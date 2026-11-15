"""Phase 1 offline test suite for InboxCura / email_summarise.

Covers: keyset pagination, threading, History incremental sync,
admin auth, the coordinator reconciliation fixes, and regressions on
the pre-Phase-1 single-mail endpoints.

OFFLINE ONLY: every test runs against mocks. No TiDB, no Gmail, no
network. The DB is faked at ``app.database.get_db_cursor`` (SQL text
is asserted directly) and at the ``app.routes.api`` import bindings;
Gmail is faked at the ``app.services.gmail_service`` / history_sync
bindings. Nothing here proves live behaviour on Render.

Run from the repo root with:
    /tmp/es-test-venv/bin/python -m pytest tests/test_phase1.py -v
(requires pytest in that venv; ``pip install pytest`` if missing).
Or plain unittest discovery is NOT supported -- this file uses pytest
fixtures (``monkeypatch``); run it with pytest as above.
"""
import base64
import copy
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)  # app.main mounts StaticFiles with relative dirs

import app.database as db
import app.routes.api as api
import app.services.gmail_service as gs
import app.services.history_sync as hs
from app.cache import assistant_cache, dashboard_cache
from app.config import settings
from app.main import app
from fastapi.testclient import TestClient

TEST_TOKEN = "test-token"
MY_EMAIL = "me@example.com"

# ------------------------------------------------------------------
# Canned data
# ------------------------------------------------------------------

def make_row(id, message_id, thread_id, sender, subject, labels=None,
             is_unread=True, is_starred=False, is_archived=False,
             is_trashed=False, message_count=1, unread_count=1):
    return {
        "id": id, "message_id": message_id, "thread_id": thread_id,
        "sender": sender, "subject": subject, "snippet": f"snip {subject}",
        "labels": labels, "is_unread": is_unread, "is_starred": is_starred,
        "is_archived": is_archived, "is_trashed": is_trashed,
        "received_at": None, "created_at": None,
        "message_count": message_count, "unread_count": unread_count,
    }

def default_thread_rows():
    """One row per thread (the thread's LATEST message), id DESC order
    is the list order. Deliberately includes edge rows:
    - t3 has labels=None (NULL in TiDB) -> must still count as primary.
    - t4 is starred AND archived -> visible in Starred, not in Unread.
    - t5 is starred AND trashed -> visible in NO folder.
    """
    return [
        make_row(105, "m1b", "t1", "Alice <alice@example.com>", "Alpha launch",
                 labels="INBOX", is_unread=True, message_count=2, unread_count=1),
        make_row(104, "m2", "t2", "Bob <bob@example.com>", "Beta notes",
                 labels="INBOX,CATEGORY_SOCIAL", is_unread=False,
                 is_starred=True, message_count=1, unread_count=0),
        make_row(103, "m3", "t3", "Carol <carol@example.com>", "Alpha follow-up",
                 labels=None, is_unread=True, message_count=1, unread_count=1),
        make_row(102, "m4", "t4", "Dave <dave@example.com>", "Urgent: server down",
                 labels="INBOX", is_unread=True, is_starred=True,
                 is_archived=True, message_count=1, unread_count=1),
        make_row(101, "m5", "t5", "Eve <eve@example.com>", "Delta summary",
                 labels="INBOX", is_unread=False, is_starred=True,
                 is_trashed=True, message_count=1, unread_count=0),
        make_row(100, "m6", "t6", "Frank <frank@example.com>", "Gamma plan",
                 labels="INBOX,CATEGORY_PROMOTIONS", is_unread=True,
                 message_count=1, unread_count=1),
    ]

def default_thread_messages():
    """thread_id -> chronological messages (oldest first)."""
    m1a = make_row(101, "m1a", "t1", "Alice <alice@example.com>", "Alpha launch",
                   labels="INBOX", is_unread=False, message_count=2, unread_count=1)
    m1b = make_row(105, "m1b", "t1", "Alice <alice@example.com>", "Alpha launch",
                   labels="INBOX", is_unread=True, message_count=2, unread_count=1)
    rows = default_thread_rows()
    by_id = {r["message_id"]: r for r in rows}
    return {
        "t1": [m1a, m1b],
        "t2": [by_id["m2"]],
        "t3": [by_id["m3"]],
        "t4": [by_id["m4"]],
        "t5": [by_id["m5"]],
        "t6": [by_id["m6"]],
        # legacy single-message thread addressed by its message_id
        "m2": [by_id["m2"]],
    }

def all_single_rows():
    rows = default_thread_rows()
    m1a = make_row(101, "m1a", "t1", "Alice <alice@example.com>", "Alpha launch",
                   labels="INBOX", is_unread=False)
    return rows + [m1a]

# ------------------------------------------------------------------
# Fake DB cursor (for app.database-level tests)
# ------------------------------------------------------------------

class FakeCursor:
    """Records the SQL + params of every execute; returns canned rows.
    Also enforces the placeholder/param-count invariant."""
    def __init__(self, fetchall_rows=None, fetchone_row=None):
        self.executed = []  # list of (sql, params)
        self._fetchall_rows = fetchall_rows if fetchall_rows is not None else []
        self._fetchone_row = fetchone_row
        self.rowcount = 1
        self.lastrowid = 99

    def execute(self, sql, params=None):
        params = params if params is not None else ()
        assert sql.count("%s") == len(params), (
            f"placeholder/param mismatch: {sql.count('%s')} vs {len(params)}\n{sql}\n{params}")
        self.executed.append((sql, params))

    def executemany(self, sql, values):
        for v in values:
            self.execute(sql, v)

    def fetchall(self):
        return copy.deepcopy(self._fetchall_rows)

    def fetchone(self):
        return copy.deepcopy(self._fetchone_row) if self._fetchone_row is not None else None

    @property
    def sql(self):
        return self.executed[-1][0] if self.executed else ""

    @property
    def params(self):
        return self.executed[-1][1] if self.executed else ()

def fake_cursor_ctx(fake):
    @contextmanager
    def _ctx(commit=False):
        yield fake
    return _ctx

# ------------------------------------------------------------------
# Fake list_threads (for API-level tests): mirrors the real semantics
# over the in-memory rows, using the REAL cursor encode/decode so
# tamper / cross-query cursor rejection is exercised end to end.
# ------------------------------------------------------------------

def _folder_match(r, folder):
    if folder == "inbox":
        return not r["is_archived"] and not r["is_trashed"]
    if folder == "starred":
        return r["is_starred"] and not r["is_trashed"]
    if folder == "unread":
        return r["is_unread"] and not r["is_archived"] and not r["is_trashed"]
    if folder == "urgent":
        return ("urgent" in r["subject"].lower()
                and not r["is_archived"] and not r["is_trashed"])
    if folder == "reply":
        return (r["is_unread"] and not r["is_archived"] and not r["is_trashed"]
                and "noreply" not in r["sender"].lower())
    if folder == "all":
        return not r["is_trashed"]
    raise ValueError(f"Unknown folder: {folder}")

def _cat_match(r, cat):
    if cat is None:
        return True
    labels = r.get("labels") or ""
    if cat == "primary":
        return not any(c in labels for c in
                       ("CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
                        "CATEGORY_UPDATES", "CATEGORY_FORUMS"))
    return {"social": "CATEGORY_SOCIAL", "promotions": "CATEGORY_PROMOTIONS",
            "updates": "CATEGORY_UPDATES", "forums": "CATEGORY_FORUMS"}[cat] in labels

def _q_match(r, q):
    if not q:
        return True
    q = q.lower()
    return (q in r["subject"].lower() or q in r["sender"].lower()
            or q in r["snippet"].lower())

def fake_list_threads_factory(rows):
    def _fake(folder="inbox", cat=None, q=None, limit=30, cursor=None):
        if folder not in ("inbox", "starred", "unread", "urgent", "reply", "all"):
            raise ValueError(f"Unknown folder: {folder}")
        if cat not in (None, "primary", "social", "promotions", "updates", "forums"):
            raise ValueError(f"Unknown category: {cat}")
        limit = max(1, min(100, int(limit or 30)))
        q = (q or "").strip() or None
        last_id = db._decode_cursor(cursor, folder, cat, q) if cursor else None
        pool = [r for r in rows
                if _folder_match(r, folder) and _cat_match(r, cat) and _q_match(r, q)]
        pool.sort(key=lambda r: r["id"], reverse=True)
        if last_id is not None:
            pool = [r for r in pool if r["id"] < last_id]
        window = pool[:limit + 1]
        has_more = len(window) > limit
        page = window[:limit]
        nxt = db._encode_cursor(page[-1]["id"], folder, cat, q) if has_more and page else None
        return page, nxt, has_more
    return _fake

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture()
def thread_rows():
    return default_thread_rows()

@pytest.fixture()
def thread_messages():
    return default_thread_messages()

@pytest.fixture()
def api_client(monkeypatch, thread_rows, thread_messages):
    """TestClient with every DB/Gmail call faked. Per-test overrides:
    tests may monkeypatch.setattr(api.<name>, ...) again -- last write
    wins and everything is undone at teardown."""
    calls = {
        "batch": [], "local_state": [], "gmail_state": [],
        "reply": [], "send": [], "background_sync": [],
    }
    monkeypatch.setattr(settings, "ADMIN_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
    dashboard_cache.clear()
    assistant_cache.clear()

    singles = {r["message_id"]: r for r in all_single_rows()}
    singles.update({str(1000 + r["id"]): r for r in all_single_rows()})

    def _get_email_by_id_or_msg_id(identifier):
        return copy.deepcopy(singles.get(str(identifier)))

    def _get_email_full(message_id):
        return {"thread_id": "t1", "sender": "Alice <alice@example.com>",
                "subject": "Alpha launch", "date": "Mon, 16 Nov 2026",
                "received_at": None, "snippet": "snip",
                "body_text": "FULL BODY", "body_html": "<p>FULL BODY</p>",
                "label_ids": ["INBOX"]}

    def _batch(ids, **kw):
        calls["batch"].append((tuple(ids), dict(kw)))
        return True

    def _local_state(**kw):
        calls["local_state"].append(dict(kw))
        return True

    def _gmail_state(**kw):
        calls["gmail_state"].append(dict(kw))
        return True

    def _reply(mid, body, reply_all=False):
        calls["reply"].append((mid, body, reply_all))
        return "sent999"

    def _send(to, subject, body):
        calls["send"].append((to, subject, body))
        return "sent888"

    monkeypatch.setattr(api, "list_threads", fake_list_threads_factory(thread_rows))
    monkeypatch.setattr(api, "get_thread_messages",
                        lambda tid: copy.deepcopy(thread_messages.get(tid, [])))
    monkeypatch.setattr(api, "get_email_by_id_or_msg_id", _get_email_by_id_or_msg_id)
    monkeypatch.setattr(api, "get_email_full", _get_email_full)
    monkeypatch.setattr(api, "batch_update_message_states", _batch)
    monkeypatch.setattr(api, "update_processed_email_state", _local_state)
    monkeypatch.setattr(api, "update_gmail_message_state", _gmail_state)
    monkeypatch.setattr(api, "reply_to_email", _reply)
    monkeypatch.setattr(api, "send_email", _send)
    monkeypatch.setattr(api, "run_incremental_sync", lambda: {
        "status": "success", "mode": "incremental",
        "processed": {"added": 2, "deleted": 1, "label_changes": 3},
        "has_more": False})
    monkeypatch.setattr(api, "get_sync_checkpoint", lambda: {
        "account_id": "me", "history_id": 160,
        "last_sync_at": "2026-11-16T03:00:00", "last_mode": "incremental"})
    monkeypatch.setattr(api, "get_dashboard_summary", lambda: {
        "latest": None, "history": [], "recent_emails": [],
        "lifetime_emails": 0,
        "inbox_counts": {"unread": 3, "today": 1, "urgent": 1,
                         "needs_reply": 2, "total_threads": 5}})
    monkeypatch.setattr(api, "maybe_trigger_background_sync",
                        lambda: calls["background_sync"].append(True) or False)

    client = TestClient(app, raise_server_exceptions=False)
    client.calls = calls  # type: ignore[attr-defined]
    client.thread_messages = thread_messages  # type: ignore[attr-defined]
    yield client

@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}

# ------------------------------------------------------------------
# 1. Pagination (API level, via fake list_threads + real cursor codec)
# ------------------------------------------------------------------

class TestPagination:
    def test_page1_then_page2_no_duplicates(self, api_client, auth_headers):
        r1 = api_client.get("/api/mails", headers=auth_headers, params={"limit": 2})
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1["status"] == "success" and d1["has_more"] is True
        assert d1["next_cursor"]
        assert [m["id"] for m in d1["mails"]] == [1105, 1104]  # inbox, id DESC
        r2 = api_client.get("/api/mails", headers=auth_headers,
                            params={"limit": 2, "cursor": d1["next_cursor"]})
        assert r2.status_code == 200, r2.text
        d2 = r2.json()
        assert d2["has_more"] is False and d2["next_cursor"] is None
        assert [m["id"] for m in d2["mails"]] == [1103, 1100]
        assert not ({m["id"] for m in d1["mails"]} & {m["id"] for m in d2["mails"]})

    def test_tampered_cursor_400(self, api_client, auth_headers):
        r = api_client.get("/api/mails", headers=auth_headers,
                           params={"cursor": "!!!not-base64!!!"})
        assert r.status_code == 400, r.text

    def test_cursor_from_another_folder_400(self, api_client, auth_headers):
        d1 = api_client.get("/api/mails", headers=auth_headers,
                            params={"limit": 2}).json()
        r = api_client.get("/api/mails", headers=auth_headers, params={
            "folder": "starred", "limit": 2, "cursor": d1["next_cursor"]})
        assert r.status_code == 400, r.text

    def test_cursor_from_another_cat_400(self, api_client, auth_headers):
        d1 = api_client.get("/api/mails", headers=auth_headers,
                            params={"cat": "primary", "limit": 1}).json()
        assert d1["next_cursor"], d1
        r = api_client.get("/api/mails", headers=auth_headers, params={
            "cat": "social", "limit": 1, "cursor": d1["next_cursor"]})
        assert r.status_code == 400, r.text

    def test_cursor_from_another_search_400(self, api_client, auth_headers):
        d1 = api_client.get("/api/mails", headers=auth_headers,
                            params={"q": "alpha", "limit": 1}).json()
        assert d1["next_cursor"], d1
        r = api_client.get("/api/mails", headers=auth_headers, params={
            "q": "beta", "limit": 1, "cursor": d1["next_cursor"]})
        assert r.status_code == 400, r.text

    def test_limit_clamps(self, api_client, auth_headers):
        r = api_client.get("/api/mails", headers=auth_headers, params={"limit": 500})
        assert r.status_code == 200 and r.json()["limit"] == 100, r.text
        r = api_client.get("/api/mails", headers=auth_headers, params={"limit": -3})
        assert r.status_code == 200 and r.json()["limit"] == 1, r.text
        assert len(r.json()["mails"]) == 1
        r = api_client.get("/api/mails", headers=auth_headers, params={"limit": 2})
        assert r.json()["limit"] == 2

    def test_folder_change_resets_to_different_first_page(self, api_client, auth_headers):
        inbox = api_client.get("/api/mails", headers=auth_headers,
                               params={"limit": 2}).json()
        starred = api_client.get("/api/mails", headers=auth_headers,
                                 params={"folder": "starred", "limit": 2}).json()
        assert [m["id"] for m in starred["mails"]] == [1104, 1102]
        assert [m["id"] for m in starred["mails"]] != [m["id"] for m in inbox["mails"]]

    def test_unknown_folder_400(self, api_client, auth_headers):
        r = api_client.get("/api/mails", headers=auth_headers, params={"folder": "nope"})
        assert r.status_code == 400, r.text

    def test_search_paginates(self, api_client, auth_headers):
        d1 = api_client.get("/api/mails", headers=auth_headers,
                            params={"q": "alpha", "limit": 1}).json()
        assert [m["id"] for m in d1["mails"]] == [1105] and d1["has_more"] is True
        d2 = api_client.get("/api/mails", headers=auth_headers,
                            params={"q": "alpha", "limit": 1,
                                    "cursor": d1["next_cursor"]}).json()
        assert [m["id"] for m in d2["mails"]] == [1103] and d2["has_more"] is False

    def test_emails_alias(self, api_client, auth_headers):
        r = api_client.get("/api/emails", headers=auth_headers, params={"limit": 2})
        assert r.status_code == 200 and r.json()["status"] == "success", r.text

    def test_mails_fires_background_sync_trigger(self, api_client, auth_headers):
        api_client.get("/api/mails", headers=auth_headers)
        assert len(api_client.calls["background_sync"]) == 1

# ------------------------------------------------------------------
# 2. Pagination / threading at the SQL layer (real list_threads)
# ------------------------------------------------------------------

class TestListThreadsSQL:
    def _run(self, monkeypatch, **kw):
        fake = FakeCursor(fetchall_rows=[
            {"id": 5, "message_id": "m5", "thread_id": "t1",
             "message_count": 2, "unread_count": 1},
            {"id": 3, "message_id": "m3", "thread_id": None,
             "message_count": 1, "unread_count": 0},
        ])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        rows, nxt, more = db.list_threads(**kw)
        return fake, rows, nxt, more

    def test_placeholder_counts_all_folders_and_cats(self, monkeypatch):
        for folder in ("inbox", "starred", "unread", "urgent", "reply", "all"):
            for cat in (None, "primary", "social", "promotions", "updates", "forums"):
                fake, rows, nxt, more = self._run(
                    monkeypatch, folder=folder, cat=cat, q="test", limit=30)
                assert len(rows) == 2 and more is False and nxt is None
                assert "COALESCE(thread_id, message_id)" in fake.sql
                assert "OFFSET" not in fake.sql.upper()
                assert fake.params[-1] == 31  # limit + 1

    def test_one_row_per_thread_sql_shape(self, monkeypatch):
        fake, rows, _, _ = self._run(monkeypatch, folder="inbox")
        assert "GROUP BY tkey" in fake.sql
        assert "MAX(id) AS max_id" in fake.sql
        assert "message_count" in fake.sql and "unread_count" in fake.sql

    def test_cursor_sql_and_limit_clamp(self, monkeypatch):
        fake = FakeCursor(fetchall_rows=[])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.list_threads(folder="inbox", limit=1,
                        cursor=db._encode_cursor(5, "inbox", None, None))
        assert "p.id < %s" in fake.sql and fake.params[-2] == 5
        db.list_threads(folder="inbox", limit=500)
        assert fake.params[-1] == 101  # clamped to 100, +1
        db.list_threads(folder="inbox", limit=-5)
        assert fake.params[-1] == 2  # clamped to 1, +1

    def test_cursor_codec(self):
        c = db._encode_cursor(123, "inbox", None, None)
        assert db._decode_cursor(c, "inbox", None, None) == 123
        c2 = db._encode_cursor(77, "starred", "social", "hello")
        assert db._decode_cursor(c2, "starred", "social", "hello") == 77
        for bad in [("unread", "social", "hello"), ("starred", None, "hello"),
                    ("starred", "social", None), ("starred", "social", "hell")]:
            with pytest.raises(ValueError):
                db._decode_cursor(c2, *bad)
        with pytest.raises(ValueError):
            db._decode_cursor("!!!not-base64!!!", "inbox", None, None)
        with pytest.raises(ValueError):
            db._decode_cursor(base64.urlsafe_b64encode(b'{"v":2}').decode(),
                              "inbox", None, None)

    def test_starred_excludes_trashed_sql(self, monkeypatch):
        fake, _, _, _ = self._run(monkeypatch, folder="starred")
        assert "p.is_starred = TRUE AND p.is_trashed = FALSE" in fake.sql

    def test_unread_excludes_archived_and_trashed_sql(self, monkeypatch):
        fake, _, _, _ = self._run(monkeypatch, folder="unread")
        assert ("p.is_unread = TRUE AND p.is_archived = FALSE "
                "AND p.is_trashed = FALSE") in fake.sql

    def test_primary_uses_coalesce_for_null_labels(self, monkeypatch):
        fake, _, _, _ = self._run(monkeypatch, folder="inbox", cat="primary")
        assert "COALESCE(p.labels, '') NOT LIKE '%CATEGORY_SOCIAL%'" in fake.sql
        assert "COALESCE(p.labels, '') NOT LIKE '%CATEGORY_PROMOTIONS%'" in fake.sql
        assert "COALESCE(p.labels, '') NOT LIKE '%CATEGORY_UPDATES%'" in fake.sql
        assert "COALESCE(p.labels, '') NOT LIKE '%CATEGORY_FORUMS%'" in fake.sql
        # NULL-semantics proof: in MySQL, NULL NOT LIKE '...' is NULL
        # (row dropped); COALESCE(NULL,'') NOT LIKE '...' is TRUE (row kept).
        # The SQL above is exactly the COALESCE form, so a NULL-labels row
        # is a primary row -- exercised at API level too (t3 in primary).
        assert "p.labels NOT LIKE '%CATEGORY_SOCIAL%'" not in fake.sql.replace(
            "COALESCE(p.labels, '') NOT LIKE", "")

    def test_get_thread_messages_chronological_sql(self, monkeypatch):
        fake = FakeCursor(fetchall_rows=[])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.get_thread_messages("t1")
        assert "ORDER BY (received_at IS NULL), received_at ASC, id ASC" in fake.sql
        assert "thread_id = %s OR message_id = %s" in fake.sql

    def test_get_inbox_counts_sql_and_mapping(self, monkeypatch):
        fake = FakeCursor(fetchone_row={"unread": 1, "today": 2, "urgent": 0,
                                        "needs_reply": 1, "total_threads": 9})
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        counts = db.get_inbox_counts()
        assert counts == {"unread": 1, "today": 2, "urgent": 0,
                          "needs_reply": 1, "total_threads": 9}
        assert "CONVERT_TZ" in fake.sql
        assert "COUNT(DISTINCT COALESCE(thread_id, message_id))" in fake.sql

# ------------------------------------------------------------------
# 3. Threading (API level)
# ------------------------------------------------------------------

class TestThreading:
    def test_list_returns_one_entry_per_thread(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"limit": 100}).json()
        t1_rows = [m for m in d["mails"] if m["thread_id"] == "t1"]
        assert len(t1_rows) == 1
        assert t1_rows[0]["message_count"] == 2
        assert t1_rows[0]["unread_count"] == 1
        assert t1_rows[0]["id"] == 1000 + 105  # row id = 1000 + latest db id

    def test_thread_view_chronological_latest_body_only(self, api_client, auth_headers):
        r = api_client.get("/api/threads/t1", headers=auth_headers)
        assert r.status_code == 200, r.text
        msgs = r.json()["thread"]["messages"]
        assert [m["id"] for m in msgs] == [1101, 1105]  # oldest -> latest
        assert msgs[0]["has_body"] is False
        assert msgs[1]["has_body"] is True
        assert msgs[1]["body_text"] == "FULL BODY"
        assert r.json()["thread"]["thread_id"] == "t1"

    def test_thread_view_unknown_404(self, api_client, auth_headers):
        assert api_client.get("/api/threads/nope", headers=auth_headers).status_code == 404

    def test_reply_calls_reply_to_email_on_latest_with_reply_all(
            self, api_client, auth_headers):
        r = api_client.post("/api/threads/t1/reply", headers=auth_headers,
                            json={"body": "thanks", "reply_all": True})
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "success", "sent_message_id": "sent999",
                            "thread_id": "t1"}
        assert api_client.calls["reply"] == [("m1b", "thanks", True)]

    def test_reply_defaults_reply_all_false(self, api_client, auth_headers):
        api_client.post("/api/threads/t1/reply", headers=auth_headers,
                        json={"body": "thanks"})
        assert api_client.calls["reply"] == [("m1b", "thanks", False)]

    def test_reply_unknown_thread_404(self, api_client, auth_headers):
        r = api_client.post("/api/threads/nope/reply", headers=auth_headers,
                            json={"body": "hi"})
        assert r.status_code == 404

    def test_reply_empty_body_400(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t1/reply", headers=auth_headers,
                            json={"body": "   "})
        assert r.status_code == 400, r.text
        assert api_client.calls["reply"] == []

    def test_archive_calls_batch_once_with_all_ids(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t1/state", headers=auth_headers,
                            json={"is_archived": True})
        assert r.status_code == 200, r.text
        assert len(api_client.calls["batch"]) == 1
        ids, kw = api_client.calls["batch"][0]
        assert ids == ("m1a", "m1b")  # ALL messages, ONE batchModify
        assert kw.get("is_archived") is True

    def test_star_calls_batch_with_latest_only(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t1/state", headers=auth_headers,
                            json={"is_starred": True})
        assert r.status_code == 200, r.text
        assert api_client.calls["batch"] == [(("m1b",), {
            "is_unread": None, "is_starred": True, "is_trash": None,
            "is_archived": None, "is_spam": None})] or \
            api_client.calls["batch"][0][0] == ("m1b",)
        assert api_client.calls["batch"][0][1]["is_starred"] is True

    def test_unstar_calls_batch_with_starred_only(self, api_client, auth_headers):
        api_client.thread_messages["t1"][0]["is_starred"] = True
        api_client.thread_messages["t1"][1]["is_starred"] = True
        r = api_client.post("/api/threads/t1/state", headers=auth_headers,
                            json={"is_starred": False})
        assert r.status_code == 200, r.text
        assert len(api_client.calls["batch"]) == 1
        ids, kw = api_client.calls["batch"][0]
        assert ids == ("m1a", "m1b") and kw.get("is_starred") is False

    def test_thread_state_unknown_404_and_empty_400(self, api_client, auth_headers):
        r = api_client.post("/api/threads/nope/state", headers=auth_headers,
                            json={"is_unread": False})
        assert r.status_code == 404
        r = api_client.post("/api/threads/t1/state", headers=auth_headers, json={})
        assert r.status_code == 400

# ------------------------------------------------------------------
# 4. Incremental sync (history_sync module, faked DB + Gmail)
# ------------------------------------------------------------------

def _http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError
    resp = httplib2.Response({"status": str(status)})
    resp.reason = "Gone" if status == 410 else "Not Found"
    return HttpError(resp, b"history expired")

@pytest.fixture()
def sync_harness(monkeypatch):
    """Patch history_sync's DB/Gmail bindings with recording fakes."""
    state = {"saved": [], "trashed_calls": [], "label_deltas": [],
             "checkpoints": [], "checkpoint": {"history_id": 100,
                                               "last_sync_at": None,
                                               "last_mode": "incremental"}}
    monkeypatch.setattr(hs, "save_processed_emails",
                        lambda items: state["saved"].extend(items))
    monkeypatch.setattr(hs, "mark_messages_trashed",
                        lambda ids: state["trashed_calls"].append(list(ids)) or len(ids))
    monkeypatch.setattr(
        hs, "apply_label_delta",
        lambda mid, add_labels=None, remove_labels=None:
            state["label_deltas"].append((mid, add_labels, remove_labels)) or True)
    monkeypatch.setattr(hs, "set_sync_checkpoint",
                        lambda hid, mode: state["checkpoints"].append((hid, mode)))
    monkeypatch.setattr(hs, "get_sync_checkpoint", lambda: state["checkpoint"])
    monkeypatch.setattr(hs, "get_message_metadata",
                        lambda mid: {"id": mid, "thread_id": "t-" + mid,
                                     "sender": "a@b.c", "subject": "s",
                                     "snippet": "", "label_ids": ["INBOX"],
                                     "is_unread": True, "is_starred": False})
    return state

def _two_page_history(monkeypatch):
    def _list(start_id, page_token=None):
        assert start_id == 100
        if page_token is None:
            return {"historyId": "150", "history": [
                {"id": "101", "messagesAdded": [
                    {"message": {"id": "m1", "threadId": "t1"}, "labelIds": ["INBOX"]}]},
                {"id": "102", "messagesDeleted": [
                    {"message": {"id": "mOld", "threadId": "tOld"}}]},
                {"id": "103",
                 "labelsAdded": [{"message": {"id": "m1"}, "labelIds": ["STARRED"]}],
                 "labelsRemoved": [{"message": {"id": "m1"}, "labelIds": ["UNREAD"]}]},
            ], "nextPageToken": "tok1"}
        return {"historyId": "160", "history": [
            {"id": "104", "messagesAdded": [
                {"message": {"id": "m2", "threadId": "t2"}, "labelIds": ["INBOX"]}]},
        ]}
    monkeypatch.setattr(hs, "list_history", _list)

class TestIncrementalSync:
    def test_happy_path_advances_checkpoint(self, sync_harness, monkeypatch):
        _two_page_history(monkeypatch)
        res = hs.run_incremental_sync()
        assert res["status"] == "success" and res["mode"] == "incremental", res
        assert res["processed"] == {"added": 2, "deleted": 1, "label_changes": 2}, res
        assert res["has_more"] is False
        assert sync_harness["checkpoints"] == [(160, "incremental")]
        assert [s["id"] for s in sync_harness["saved"]] == ["m1", "m2"]
        assert sync_harness["trashed_calls"] == [["mOld"]]

    def test_fetch_cap_early_stop_leaves_checkpoint(self, sync_harness, monkeypatch):
        many = [{"id": str(200 + i), "messagesAdded": [
                    {"message": {"id": f"mx{i}"}, "labelIds": ["INBOX"]}]}
                for i in range(150)]
        monkeypatch.setattr(hs, "list_history",
                            lambda s, page_token=None: {"historyId": "999", "history": many})
        res = hs.run_incremental_sync()
        assert res["has_more"] is True and res["processed"]["added"] == 100, res
        assert sync_harness["checkpoints"] == []

    def test_page_cap_early_stop_leaves_checkpoint(self, sync_harness, monkeypatch):
        # 5 pages consumed, Gmail still has a nextPageToken -> has_more.
        monkeypatch.setattr(
            hs, "list_history",
            lambda s, page_token=None: {"historyId": "200", "history": [],
                                        "nextPageToken": "more"})
        res = hs.run_incremental_sync()
        assert res["has_more"] is True, res
        assert sync_harness["checkpoints"] == []

    def test_failed_run_leaves_checkpoint(self, sync_harness, monkeypatch):
        monkeypatch.setattr(hs, "list_history",
                            lambda s, page_token=None: (_ for _ in ()).throw(
                                RuntimeError("gmail down")))
        res = hs.run_incremental_sync()
        assert res["status"] == "error", res
        assert res["has_more"] is True
        assert sync_harness["checkpoints"] == []
        assert sync_harness["saved"] == []

    def test_idempotent_reprocessing(self, sync_harness, monkeypatch):
        _two_page_history(monkeypatch)
        res1 = hs.run_incremental_sync()
        first_saved = [s["id"] for s in sync_harness["saved"]]
        first_checkpoints = list(sync_harness["checkpoints"])
        assert res1["status"] == "success"
        # Replay the SAME history from the SAME checkpoint (as a failed
        # run's retry would): same ids upserted, no error, same checkpoint.
        sync_harness["saved"].clear()
        sync_harness["checkpoints"].clear()
        res2 = hs.run_incremental_sync()
        assert res2["status"] == "success", res2
        assert [s["id"] for s in sync_harness["saved"]] == first_saved
        assert sync_harness["checkpoints"] == first_checkpoints
        assert res2["processed"] == res1["processed"]

    @pytest.mark.parametrize("status", [404, 410])
    def test_expired_history_id_reconciles_without_deleting(
            self, sync_harness, monkeypatch, status):
        def _raising(start_id, page_token=None):
            raise _http_error(status)
        monkeypatch.setattr(hs, "list_history", _raising)
        monkeypatch.setattr(hs, "get_profile_history_id", lambda: 500)
        monkeypatch.setattr(hs, "fetch_recent_emails",
                            lambda range_config=None, max_results=100:
                                [{"id": "r1", "thread_id": "tr1"}])
        res = hs.run_incremental_sync()
        assert res["status"] == "success" and res["mode"] == "reconciled", res
        assert "warning" in res
        assert sync_harness["checkpoints"] == [(500, "reconciled")]
        assert [s["id"] for s in sync_harness["saved"]] == ["r1"]
        assert sync_harness["trashed_calls"] == []  # local rows never deleted

    def test_busy_guard_when_lock_held(self, sync_harness):
        assert hs._sync_lock.acquire(blocking=False)
        try:
            res = hs.run_incremental_sync()
            assert res["status"] == "busy", res
        finally:
            hs._sync_lock.release()

    def test_initial_sync_no_checkpoint(self, sync_harness, monkeypatch):
        sync_harness["checkpoint"] = None
        monkeypatch.setattr(hs, "get_profile_history_id", lambda: 700)
        monkeypatch.setattr(hs, "fetch_recent_emails",
                            lambda range_config=None, max_results=100:
                                [{"id": "i1"}, {"id": "i2"}])
        res = hs.run_incremental_sync()
        assert res == {"status": "success", "mode": "initial",
                       "added": 2, "has_more": False}, res
        assert sync_harness["checkpoints"] == [(700, "initial")]

    def test_sync_endpoints(self, api_client, auth_headers, monkeypatch):
        r = api_client.post("/api/sync/incremental", headers=auth_headers)
        assert r.status_code == 200 and r.json()["mode"] == "incremental", r.text
        r = api_client.get("/api/sync/status", headers=auth_headers)
        assert r.status_code == 200 and r.json()["checkpoint"]["history_id"] == 160, r.text
        r = api_client.post("/api/sync-inbox-state", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["updated_count"] == 6, r.json()  # 2+1+3
        assert r.json()["mode"] == "incremental"

    def test_sync_busy_via_api(self, api_client, auth_headers, monkeypatch):
        monkeypatch.setattr(api, "run_incremental_sync",
                            lambda: {"status": "busy",
                                     "message": "An incremental sync is already in progress."})
        r = api_client.post("/api/sync/incremental", headers=auth_headers)
        assert r.status_code == 200 and r.json()["status"] == "busy", r.text
        r = api_client.post("/api/sync-inbox-state", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == {"status": "busy", "updated_count": 0}, r.json()

# ------------------------------------------------------------------
# 5. Auth
# ------------------------------------------------------------------

class TestAuth:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/mails"),
        ("GET", "/api/threads/t1"),
        ("POST", "/api/sync/incremental"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/sync/status"),
    ])
    def test_no_token_401(self, api_client, method, path):
        r = api_client.request(method, path)
        assert r.status_code == 401, (method, path, r.text)

    def test_wrong_token_401(self, api_client):
        r = api_client.get("/api/mails", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_admin_token_unset_503(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
        r = api_client.get("/api/mails", headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 503, r.text
        r = api_client.get("/api/mails")  # no header either: still 503, not 401
        assert r.status_code == 503, r.text

# ------------------------------------------------------------------
# 6. Coordinator fixes (API level)
# ------------------------------------------------------------------

class TestCoordinatorFixes:
    def test_starred_folder_excludes_trashed(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "starred", "limit": 100}).json()
        ids = [m["id"] for m in d["mails"]]
        assert 1101 not in ids  # t5: starred but trashed
        assert ids == [1104, 1102]  # t2 starred; t4 starred+archived stays

    def test_unread_folder_excludes_archived_and_trashed(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "unread", "limit": 100}).json()
        ids = [m["id"] for m in d["mails"]]
        assert 1102 not in ids  # t4: unread but archived
        assert 1101 not in ids  # t5: trashed
        assert ids == [1105, 1103, 1100]

    def test_primary_cat_includes_null_labels_row(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"cat": "primary", "limit": 100}).json()
        assert [m["thread_id"] for m in d["mails"]] == ["t1", "t3"]
        # t3's labels are NULL in TiDB and it still shows up as primary.

    def test_reply_all_addressing_excludes_me_and_dedupes(self, monkeypatch):
        from email import policy
        from email.parser import BytesParser

        orig = {"threadId": "thr9", "payload": {"headers": [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "To", "value": "Me <me@example.com>, Bob <bob@example.com>, "
                                    "bob@example.com, Alice <alice@example.com>"},
            {"name": "Cc", "value": "Carol <carol@example.com>, me@example.com, "
                                    "carol@example.com"},
            {"name": "Subject", "value": "Hello"},
            {"name": "Message-ID", "value": "<mid1@x>"},
            {"name": "References", "value": "<ref0@x>"},
        ]}}

        class FakeMessages:
            def __init__(self):
                self.last_sent = None
            def get(self, userId, id, format, metadataHeaders=None):
                class Req:
                    def execute(_self):
                        return orig
                return Req()
            def send(self, userId, body):
                outer = self
                class Req:
                    def execute(_self):
                        outer.last_sent = body
                        return {"id": "sent123"}
                return Req()

        msgs = FakeMessages()

        class FakeService:
            def users(self):
                return self
            def messages(self):
                return msgs

        monkeypatch.setattr(gs, "get_gmail_client", lambda: FakeService())
        monkeypatch.setattr(settings, "MY_EMAIL", "me@example.com")

        sent = gs.reply_to_email("mOrig", "hi", reply_all=True)
        assert sent == "sent123"
        msg = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(msgs.last_sent["raw"]))
        assert msg["To"] == "alice@example.com, bob@example.com", msg["To"]
        assert msg["Cc"] == "carol@example.com", msg["Cc"]
        assert "me@example.com" not in msg["To"] and "me@example.com" not in (msg["Cc"] or "")
        assert msg["To"].count("bob@example.com") == 1
        assert msg["Cc"].count("carol@example.com") == 1

        sent2 = gs.reply_to_email("mOrig", "hi")  # reply_all=False
        msg2 = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(msgs.last_sent["raw"]))
        assert msg2["To"] == "alice@example.com" and msg2.get("Cc") is None

# ------------------------------------------------------------------
# 7. Regression: pre-Phase-1 single-mail endpoints + dashboard
# ------------------------------------------------------------------

class TestRegression:
    def test_get_single_mail_full_body(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1105", headers=auth_headers)
        assert r.status_code == 200, r.text
        mail = r.json()["mail"]
        assert mail["body_text"] == "FULL BODY"
        assert mail["message_id"] == "m1b"
        r = api_client.get("/api/mails/9999", headers=auth_headers)
        assert r.status_code == 404

    def test_post_single_mail_state(self, api_client, auth_headers):
        r = api_client.post("/api/mails/1105/state", headers=auth_headers,
                            json={"is_unread": False})
        assert r.status_code == 200, r.text
        assert r.json()["synced_gmail"] is True
        assert r.json()["message_id"] == "m1b"
        assert api_client.calls["gmail_state"], "Gmail sync not called"
        r = api_client.post("/api/mails/9999/state", headers=auth_headers,
                            json={"is_unread": False})
        assert r.status_code == 404

    def test_send_validation(self, api_client, auth_headers):
        for payload, why in [
                ({"to": "not-an-email", "subject": "s", "body": "b"}, "bad to"),
                ({"to": "a@b.com", "subject": "  ", "body": "b"}, "empty subject"),
                ({"to": "a@b.com", "subject": "s", "body": "  "}, "empty body")]:
            r = api_client.post("/api/mails/send", headers=auth_headers, json=payload)
            assert r.status_code == 400, (why, r.text)
        assert api_client.calls["send"] == []
        r = api_client.post("/api/mails/send", headers=auth_headers,
                            json={"to": "a@b.com", "subject": "s", "body": "b"})
        assert r.status_code == 200, r.text
        assert r.json()["sent_message_id"] == "sent888"
        assert api_client.calls["send"] == [("a@b.com", "s", "b")]

    def test_dashboard_returns_inbox_counts(self, api_client, auth_headers):
        dashboard_cache.clear()
        r = api_client.get("/api/dashboard", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["inbox_counts"]["total_threads"] == 5, r.json()

# ------------------------------------------------------------------
# 8. Route table
# ------------------------------------------------------------------

class TestRouteTable:
    def test_previous_routes_still_registered(self):
        routes = {(tuple(sorted(getattr(r, "methods", None) or ())) , r.path)
                  for r in app.routes if hasattr(r, "methods")}
        expected = [
            (("GET",), "/api/dashboard"), (("GET",), "/api/mails"),
            (("GET",), "/api/emails"), (("GET",), "/api/labels"),
            (("GET",), "/api/mails/{mail_id}"),
            (("POST",), "/api/mails/{mail_id}/state"),
            (("POST",), "/api/mails/{mail_id}/labels"),
            (("POST",), "/api/mails/{mail_id}/reply"),
            (("POST",), "/api/mails/send"),
            (("POST",), "/api/mails/bulk-state"),
            (("GET",), "/api/config"), (("POST",), "/api/config"),
            (("GET",), "/api/keys"), (("POST",), "/api/keys"),
            (("PUT",), "/api/keys/{key_id}"),
            (("DELETE",), "/api/keys/{key_id}"),
            (("POST",), "/api/keys/{key_id}/test"),
            (("POST",), "/api/chat-assistant"),
            (("POST",), "/api/summary/trigger"),
            (("GET",), "/api/summary/status"),
            (("POST",), "/api/sync-now"),
            (("POST",), "/api/sync-inbox-state"),
            (("POST",), "/api/cron-trigger"),
            (("GET",), "/health"), (("GET",), "/"),
        ]
        for methods, path in expected:
            assert (methods, path) in routes, f"missing route {methods} {path}"

    def test_six_new_phase1_routes_present(self):
        routes = {(tuple(sorted(getattr(r, "methods", None) or ())), r.path)
                  for r in app.routes if hasattr(r, "methods")}
        new_routes = [
            (("GET",), "/api/emails"),
            (("GET",), "/api/threads/{thread_id}"),
            (("POST",), "/api/threads/{thread_id}/state"),
            (("POST",), "/api/threads/{thread_id}/reply"),
            (("POST",), "/api/sync/incremental"),
            (("GET",), "/api/sync/status"),
        ]
        for methods, path in new_routes:
            assert (methods, path) in routes, f"missing new route {methods} {path}"
