"""Hotfix round 2 regression tests — 2026-11-16.

Covers, against the REAL db/api code, the three live failures the
round-2 hotfix addresses (live: three identical drafts at
02:59/02:59/03:00, "Couldn't save draft — retry" on a successful
save, "Navneet Yadav" listed under awaiting reply):

1. Autosave idempotency, replaying the UI's exact flow
   (static/js/app.js saveDraft/reconcileDraft: POST /api/drafts,
   then PUT /api/drafts/{id} for every later autosave; a lost
   create response is recovered by GET /api/drafts + adopt the
   matching row, never by POSTing again). The DB is a stateful
   in-memory drafts table (StatefulDraftCursor below), so the real
   db.create_draft / update_draft / list_drafts / get_draft run,
   including the server-side dedup (byte-identical content within
   10 minutes returns the existing row).
2. Create-response shape: POST /api/drafts returns 200
   {"status": "success", "draft": {"id": ...}} — the keys the UI
   reads (d.draft.id) — so a success can never be misread as failure.
3. Urgent agreement + awaiting-reply own-mail exclusion: the urgent
   count (get_inbox_counts) and the urgent folder predicate share
   one SQL fragment and the same params; a self-sent unread Primary
   thread is NOT awaiting a reply in api._needs_reply, in the mail
   object the list renders, or in db._needs_reply_sql (which the
   dashboard counts use). _needs_reply_sql is also checked
   PyMySQL-faithfully (``sql % tuple(...)``), like hotfix 1.
4. UTC timestamps: draft created_at/updated_at are naive-UTC ISO
   strings with no timezone designator — the reason the UI appends
   "Z" before Date.parse in reconcileDraft.

OFFLINE ONLY: same faking strategy as tests/test_phase1.py /
tests/test_phase2.py / tests/test_hotfix.py — the DB is faked at
``app.database.get_db_cursor``. Nothing here proves live behaviour
on Render.

Run from the repo root with:
    /tmp/es-test-venv/bin/python -m pytest tests/test_hotfix2.py -v
"""
import copy
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(REPO_ROOT)  # app.main mounts StaticFiles with relative dirs

import app.database as db
import app.routes.api as api
from app.cache import assistant_cache, dashboard_cache
from app.config import settings
from app.main import app
from fastapi.testclient import TestClient
from test_phase2 import (  # noqa: F401  (shared fakes from the Phase 2 suite)
    FakeCursor, MY_EMAIL, TEST_TOKEN, fake_cursor_ctx, make_row)

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

# Naive-UTC ISO, as _utcnow_naive().isoformat() produces it — seconds,
# optional fractional part, NEVER a timezone designator. The UI's
# reconcileDraft regex accepts exactly this shape and appends "Z".
NAIVE_UTC_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")
UI_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")  # static/js/app.js


def assert_naive_utc_iso(value):
    assert isinstance(value, str), value
    assert NAIVE_UTC_ISO_RE.match(value), value
    assert "Z" not in value and "+" not in value, value
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is None, value
    assert UI_TIMESTAMP_RE.match(value), value  # the UI can parse it


# ------------------------------------------------------------------
# Stateful drafts table: the real db draft functions run against it
# ------------------------------------------------------------------

class StatefulDraftCursor(FakeCursor):
    """FakeCursor that actually stores drafts rows.

    Intercepts only drafts-table SQL; anything else raises, so an
    unexpected DB touch in a drafts test is loud instead of silently
    canned. The dedup SELECT, INSERT, SELECT-by-id, UPDATE, DELETE,
    list SELECT and COUNT are emulated with dict rows (datetimes
    stored naive-UTC, like TiDB DATETIME columns).
    """

    def __init__(self):
        super().__init__(fetchall_rows=[], fetchone_row=None)
        self.drafts = {}  # id -> DB-shaped row dict
        self.next_id = 1
        self.lastrowid = 0
        self._pending_one = None
        self._pending_all = None

    def execute(self, sql, params=None):
        # PyMySQL-faithful check + recording, exactly like FakeCursor.
        super().execute(sql, params)
        params = params if params is not None else ()
        norm = " ".join(sql.split())
        if "DATE_SUB(NOW(), INTERVAL %s MINUTE)" in norm:
            # create_draft dedup lookup: byte-identical content,
            # newest first, within the window.
            (to_s, cc_s, bcc_s, subject, body, thread_id, message_id,
             window) = params
            cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                      - timedelta(minutes=int(window)))
            best = None
            for row in self.drafts.values():
                if (row["to_addrs"], row["cc_addrs"], row["bcc_addrs"],
                        row["subject"], row["body"], row["thread_id"],
                        row["message_id"]) == (
                            to_s, cc_s, bcc_s, subject, body,
                            thread_id, message_id) \
                        and row["created_at"] >= cutoff:
                    if best is None or row["id"] > best["id"]:
                        best = row
            self._pending_one = copy.deepcopy(best) if best else None
        elif norm.startswith("INSERT INTO drafts"):
            (to_s, cc_s, bcc_s, subject, body, thread_id,
             message_id) = params
            now = db._utcnow_naive()
            row = {"id": self.next_id, "to_addrs": to_s, "cc_addrs": cc_s,
                   "bcc_addrs": bcc_s, "subject": subject, "body": body,
                   "thread_id": thread_id, "message_id": message_id,
                   "created_at": now, "updated_at": now}
            self.drafts[self.next_id] = row
            self.lastrowid = self.next_id
            self.next_id += 1
            self._pending_one = None
        elif norm.startswith("SELECT * FROM drafts WHERE id = %s"):
            row = self.drafts.get(int(params[0]))
            self._pending_one = copy.deepcopy(row) if row else None
        elif norm.startswith("UPDATE drafts SET"):
            set_part = norm[len("UPDATE drafts SET "):].split(
                " WHERE id = %s")[0]
            cols = [c.split(" = ")[0].strip()
                    for c in set_part.split(", ")]
            draft_id = int(params[-1])
            row = self.drafts.get(draft_id)
            if row is None:
                self.rowcount = 0
            else:
                for col, value in zip(cols, params[:-1]):
                    row[col] = value
                row["updated_at"] = db._utcnow_naive()
                self.rowcount = 1
            self._pending_one = None
        elif norm.startswith("DELETE FROM drafts WHERE id = %s"):
            self.rowcount = (1 if self.drafts.pop(int(params[0]), None)
                             is not None else 0)
        elif norm.startswith("SELECT * FROM drafts WHERE id < %s"):
            rows = [r for r in sorted(self.drafts.values(),
                                      key=lambda r: r["id"], reverse=True)
                    if r["id"] < params[0]][:params[1]]
            self._pending_all = [copy.deepcopy(r) for r in rows]
        elif norm.startswith("SELECT * FROM drafts ORDER BY id DESC"):
            rows = sorted(self.drafts.values(),
                          key=lambda r: r["id"], reverse=True)[:params[0]]
            self._pending_all = [copy.deepcopy(r) for r in rows]
        elif norm.startswith("SELECT COUNT(*) AS n FROM drafts"):
            self._pending_one = {"n": len(self.drafts)}
        else:
            raise AssertionError(
                f"StatefulDraftCursor got unexpected SQL: {sql!r}")

    def fetchone(self):
        value, self._pending_one = self._pending_one, None
        return copy.deepcopy(value) if value is not None else None

    def fetchall(self):
        value, self._pending_all = self._pending_all, None
        return copy.deepcopy(value) if value is not None else []


@pytest.fixture()
def hotfix2_client(monkeypatch):
    """TestClient with the REAL db draft functions running against the
    stateful in-memory drafts table (api's draft bindings are NOT
    replaced, unlike the Phase 2 fixture's DraftStore)."""
    cursor = StatefulDraftCursor()
    monkeypatch.setattr(settings, "ADMIN_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
    dashboard_cache.clear()
    assistant_cache.clear()
    monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(cursor))
    monkeypatch.setattr(api, "maybe_trigger_background_sync", lambda: False)
    client = TestClient(app, raise_server_exceptions=False)
    client.draft_cursor = cursor  # type: ignore[attr-defined]
    yield client


def _payload(**over):
    body = {"to": ["a@b.com"], "subject": "Hello", "body": "World"}
    body.update(over)
    return body


def _list_ids(client):
    r = client.get("/api/drafts", headers=AUTH)
    assert r.status_code == 200, r.text
    return [d["id"] for d in r.json()["drafts"]]


# ------------------------------------------------------------------
# 1. Autosave idempotency — the UI's exact flow
# ------------------------------------------------------------------

class TestAutosaveIdempotency:
    def test_create_then_put_autosave_keeps_one_row(self, hotfix2_client):
        # First keystroke burst: POST creates the draft (saveDraft with
        # currentDraftId == null).
        r = hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["id"] == 1
        # Every later autosave is a PUT against that id, never a POST.
        r = hotfix2_client.put("/api/drafts/1", headers=AUTH,
                               json={"body": "World, edited"})
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["id"] == 1
        assert r.json()["draft"]["body"] == "World, edited"
        assert r.json()["draft"]["subject"] == "Hello"  # untouched
        assert _list_ids(hotfix2_client) == [1]

    def test_identical_retry_post_returns_same_id(self, hotfix2_client):
        # The live bug: a create whose response was lost made the UI
        # POST the identical payload again — three copies at
        # 02:59/02:59/03:00. The server dedup must adopt the row the
        # lost create left behind.
        first = hotfix2_client.post("/api/drafts", headers=AUTH,
                                    json=_payload())
        assert first.status_code == 200, first.text
        retry = hotfix2_client.post("/api/drafts", headers=AUTH,
                                    json=_payload())
        assert retry.status_code == 200, retry.text
        assert retry.json()["draft"]["id"] == first.json()["draft"]["id"] == 1
        assert _list_ids(hotfix2_client) == [1]
        inserts = [s for s, _ in hotfix2_client.draft_cursor.executed
                   if "INSERT INTO drafts" in s]
        assert len(inserts) == 1  # the retry ran the dedup SELECT only

    def test_distinct_content_creates_second_row(self, hotfix2_client):
        r1 = hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        r2 = hotfix2_client.post("/api/drafts", headers=AUTH,
                                 json=_payload(body="Something else"))
        assert r2.json()["draft"]["id"] == 2
        r3 = hotfix2_client.post("/api/drafts", headers=AUTH,
                                 json=_payload(to=["other@b.com"]))
        assert r3.json()["draft"]["id"] == 3
        assert _list_ids(hotfix2_client) == [3, 2, 1]  # newest first
        assert r1.json()["draft"]["id"] == 1

    def test_update_in_place_changes_row_not_count(self, hotfix2_client):
        hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        r = hotfix2_client.put("/api/drafts/1", headers=AUTH, json={
            "subject": "New subject", "to": ["x@y.com"],
            "body": "World, edited"})
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["id"] == 1
        assert _list_ids(hotfix2_client) == [1]
        d = hotfix2_client.get("/api/drafts/1", headers=AUTH).json()["draft"]
        assert d["subject"] == "New subject" and d["to"] == ["x@y.com"]
        assert d["body"] == "World, edited"
        r = hotfix2_client.put("/api/drafts/999", headers=AUTH,
                               json={"subject": "Nope"})
        assert r.status_code == 404, r.text

    def test_lost_create_reconciled_by_list_then_put(self, hotfix2_client):
        # saveDraft's lost-response path: the POST's id is never
        # learned, so reconcileDraft lists drafts, adopts the row
        # whose content matches the attempted payload, and the next
        # autosave PUTs against it — still exactly one row.
        payload = _payload()
        created = hotfix2_client.post("/api/drafts", headers=AUTH,
                                      json=payload)
        assert created.status_code == 200, created.text
        # Response "lost": the UI never sees the id; reconcile by list.
        listed = hotfix2_client.get("/api/drafts?limit=10",
                                    headers=AUTH).json()["drafts"]
        assert len(listed) == 1
        adopted = [d for d in listed
                   if d["to"] == payload["to"]
                   and d["subject"] == payload["subject"]
                   and d["body"] == payload["body"]]
        assert [d["id"] for d in adopted] == [1]
        r = hotfix2_client.put("/api/drafts/1", headers=AUTH,
                               json={"body": "World, edited"})
        assert r.status_code == 200, r.text
        assert _list_ids(hotfix2_client) == [1]
        d = hotfix2_client.get("/api/drafts/1", headers=AUTH).json()["draft"]
        assert d["body"] == "World, edited"

    def test_dedup_window_only_covers_recent_rows(self, hotfix2_client):
        # The dedup window is 10 minutes on both sides (server constant
        # and the UI's reconcileDraft window) — an identical compose
        # 11 minutes later is the user's own new draft, not a retry.
        assert db._DRAFT_DEDUP_WINDOW_MINUTES == 10
        hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        cursor = hotfix2_client.draft_cursor
        cursor.drafts[1]["created_at"] = (
            db._utcnow_naive() - timedelta(minutes=11))
        r = hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["id"] == 2
        assert _list_ids(hotfix2_client) == [2, 1]

    def test_create_is_single_cursor_no_reread(self, monkeypatch):
        # The old two-acquisition shape (INSERT, then get_draft on a
        # second connection) 500d after the row committed. The whole
        # create is now one cursor: dedup SELECT, INSERT, done.
        cursor = StatefulDraftCursor()
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(cursor))
        draft = db.create_draft(["a@b.com"], [], [], "Hi", "Body")
        assert draft["id"] == 1
        assert len(cursor.executed) == 2
        assert "DATE_SUB(NOW(), INTERVAL %s MINUTE)" in cursor.executed[0][0]
        assert "INSERT INTO drafts" in cursor.executed[1][0]
        assert not [s for s, _ in cursor.executed
                    if "SELECT * FROM drafts WHERE id = %s" in s]


# ------------------------------------------------------------------
# 2. Create-response shape — what the UI reads
# ------------------------------------------------------------------

class TestCreateResponseShape:
    def test_post_returns_status_success_with_draft_id(self, hotfix2_client):
        r = hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        # Exactly the envelope saveDraft reads: d.draft.id.
        assert set(body) == {"status", "draft"}
        assert body["status"] == "success"
        draft = body["draft"]
        assert draft["id"] is not None and isinstance(draft["id"], int)
        assert set(draft) == {"id", "to", "cc", "bcc", "subject", "body",
                              "thread_id", "message_id", "created_at",
                              "updated_at"}
        assert draft["to"] == ["a@b.com"]
        assert draft["subject"] == "Hello" and draft["body"] == "World"

    def test_put_returns_same_envelope(self, hotfix2_client):
        hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        r = hotfix2_client.put("/api/drafts/1", headers=AUTH,
                               json={"subject": "Edited"})
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"status", "draft"}
        assert r.json()["status"] == "success"
        assert r.json()["draft"]["id"] == 1
        assert r.json()["draft"]["subject"] == "Edited"


# ------------------------------------------------------------------
# 3. Urgent agreement + awaiting-reply own-mail exclusion
# ------------------------------------------------------------------

class TestUrgentAgreement:
    def test_urgent_count_and_folder_share_one_fragment(self, monkeypatch):
        monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
        folder_sql, folder_params = db._folder_predicate_sql("urgent")
        assert db._urgent_subject_sql("p") in folder_sql
        assert folder_params == list(db._URGENT_SUBJECT_LIKES)
        # The dashboard count runs the identical fragment with the
        # identical params (same basis, so the count and the folder
        # list can never disagree about what "urgent" means).
        fake = FakeCursor(fetchone_row={"unread": 1, "today": 1, "urgent": 2,
                                        "needs_reply": 0, "total_threads": 3})
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        counts = db.get_inbox_counts()
        assert counts["urgent"] == 2
        assert db._urgent_subject_sql("p") in fake.sql
        assert (tuple(fake.params[1:1 + len(db._URGENT_SUBJECT_LIKES)])
                == tuple(db._URGENT_SUBJECT_LIKES))
        # get_folder_counts embeds the same predicate verbatim.
        counts_row = {"inbox": 5, "sent": 2, "drafts": 1, "archive": 3,
                      "trash": 1, "spam": 0, "starred": 2, "unread": 4,
                      "urgent": 2, "reply": 2, "all": 12}
        fake = FakeCursor(fetchone_row=[counts_row, {"n": 0}])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        folders = db.get_folder_counts()
        assert folders["urgent"] == 2
        assert db._urgent_subject_sql("p") in fake.executed[0][0]

    def test_row_urgent_flag_uses_same_keywords_as_sql(self):
        # The mail object flags urgent from a keyword list; every SQL
        # LIKE core must be covered by it (and vice versa), so a
        # thread the row calls urgent is one the SQL counts.
        api_words = ["urgent", "action required", "deadline",
                     "immediate", "alert", "security alert"]
        for like in db._URGENT_SUBJECT_LIKES:
            core = like.strip("%")
            assert any(core in w or w in core for w in api_words), like
        for subject, expected in [
                ("URGENT: server down", True),
                ("Action required: approve", True),
                ("Deadline Friday", True),
                ("Immediate attention", True),
                ("Security alert: login", True),
                ("Just saying hello", False)]:
            row = make_row(201, "m201", "t201", "Amy <amy@example.com>",
                           subject, labels="INBOX", is_unread=True)
            assert api._mail_obj_from_row(row)["urgent"] is expected, subject


class TestAwaitingReplyOwnMail:
    def test_needs_reply_api_excludes_own_mail(self, monkeypatch):
        monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
        # Self-sent unread Primary thread: the live brief listed
        # "Navneet Yadav" three times under awaiting reply.
        assert api._needs_reply(True, "primary",
                                "Navneet Yadav <me@example.com>",
                                "Hello inbox", "me@example.com") is False
        # Address comparison is case-insensitive.
        assert api._needs_reply(True, "primary", "Me <ME@EXAMPLE.COM>",
                                "Hello inbox", "ME@EXAMPLE.COM") is False
        # Another person, same shape: still awaiting a reply.
        assert api._needs_reply(True, "primary", "Amy <amy@example.com>",
                                "Hello inbox", "amy@example.com") is True
        assert api._needs_reply(True, "primary", "amy@example.com",
                                "Hello inbox", "amy@example.com") is True
        # Unread + Primary are still required for anyone else.
        assert api._needs_reply(False, "primary", "Amy <amy@example.com>",
                                "Hello", "amy@example.com") is False
        assert api._needs_reply(True, "social", "Amy <amy@example.com>",
                                "Hello", "amy@example.com") is False

    def test_needs_reply_false_in_mail_object_for_own_mail(self, monkeypatch):
        monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
        own = make_row(201, "m201", "t201",
                       "Navneet Yadav <me@example.com>", "Hello inbox",
                       labels="INBOX", is_unread=True)
        assert api._mail_obj_from_row(own)["needsReply"] is False
        other = make_row(201, "m201", "t201", "Amy <amy@example.com>",
                         "Hello inbox", labels="INBOX", is_unread=True)
        assert api._mail_obj_from_row(other)["needsReply"] is True

    def test_needs_reply_sql_excludes_own_mail(self, monkeypatch):
        monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
        sql, params = db._needs_reply_sql("p")
        # The sender is stored as "Name <email>": the account address
        # appearing anywhere in it means own mail.
        assert "COALESCE(p.sender, '') NOT LIKE %s" in sql
        assert params[-1] == f"%{MY_EMAIL}%"
        # PyMySQL-faithful: no inlined literal '%…' patterns — the
        # hotfix-1 failure class (ValueError on Render, invisible
        # offline until the ``sql % args`` step runs).
        sql % tuple("'x'" for _ in params)  # must not raise
        assert "NOT LIKE '%" not in sql
        assert "%CATEGORY" not in sql
        assert "%" not in sql.replace("%s", "")
        # The dashboard count uses this same SQL, so its needs_reply
        # number excludes own mail too.
        fake = FakeCursor(fetchone_row={"unread": 1, "today": 1, "urgent": 0,
                                        "needs_reply": 0, "total_threads": 1})
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.get_inbox_counts()
        assert "COALESCE(p.sender, '') NOT LIKE %s" in fake.sql
        assert f"%{MY_EMAIL}%" in fake.params

    def test_needs_reply_sql_exclusion_is_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
        # The base patterns already contain sender NOT LIKE clauses
        # (noreply etc.); the own-mail exclusion adds exactly one more.
        sender_clauses = sum(
            1 for col, _ in db._NEEDS_REPLY_PATTERNS if col == "sender")
        clause = "COALESCE(p.sender, '') NOT LIKE %s"
        # Explicit opt-out: no extra sender clause beyond the base ones.
        sql, params = db._needs_reply_sql("p", exclude_sender_email="")
        assert sql.count(clause) == sender_clauses
        assert len(params) == len(db._NEEDS_REPLY_PATTERNS)
        # Explicit address wins over settings and adds one clause.
        sql, params = db._needs_reply_sql(
            "p", exclude_sender_email="boss@example.com")
        assert sql.count(clause) == sender_clauses + 1
        assert params[-1] == "%boss@example.com%"
        # LIKE metacharacters in the address are escaped, so a "%" in
        # an address cannot turn the exclusion into a wildcard.
        sql, params = db._needs_reply_sql(
            "p", exclude_sender_email="me%test@example.com")
        assert params[-1] == r"%me\%test@example.com%"
        sql % tuple("'x'" for _ in params)  # must not raise


# ------------------------------------------------------------------
# 4. UTC timestamps — why the UI appends "Z"
# ------------------------------------------------------------------

class TestUtcTimestamps:
    def test_utcnow_is_naive(self):
        assert db._utcnow_naive().tzinfo is None

    def test_create_returns_naive_utc_iso_timestamps(self, hotfix2_client):
        r = hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        assert r.status_code == 200, r.text
        draft = r.json()["draft"]
        assert_naive_utc_iso(draft["created_at"])
        assert_naive_utc_iso(draft["updated_at"])

    def test_get_and_list_return_naive_utc_iso_timestamps(self,
                                                          hotfix2_client):
        hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        one = hotfix2_client.get("/api/drafts/1", headers=AUTH).json()["draft"]
        assert_naive_utc_iso(one["created_at"])
        assert_naive_utc_iso(one["updated_at"])
        listed = hotfix2_client.get("/api/drafts", headers=AUTH
                                    ).json()["drafts"][0]
        assert_naive_utc_iso(listed["created_at"])
        assert_naive_utc_iso(listed["updated_at"])

    def test_update_returns_naive_utc_iso_updated_at(self, hotfix2_client):
        hotfix2_client.post("/api/drafts", headers=AUTH, json=_payload())
        r = hotfix2_client.put("/api/drafts/1", headers=AUTH,
                               json={"subject": "Edited"})
        assert r.status_code == 200, r.text
        assert_naive_utc_iso(r.json()["draft"]["updated_at"])

    def test_db_level_create_timestamps_are_naive_utc_iso(self, monkeypatch):
        cursor = StatefulDraftCursor()
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(cursor))
        draft = db.create_draft(["a@b.com"], [], [], "Hi", "Body")
        assert_naive_utc_iso(draft["created_at"])
        assert_naive_utc_iso(draft["updated_at"])
        # The re-read row (get_draft) carries the same shape: the
        # stored DATETIME is naive, so .isoformat() stays designator-free.
        again = db.get_draft(draft["id"])
        assert_naive_utc_iso(again["created_at"])
        assert_naive_utc_iso(again["updated_at"])
