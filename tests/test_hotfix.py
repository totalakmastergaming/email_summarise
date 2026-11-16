"""Hotfix regression tests — 2026-11-16 live Phase 2 regressions.

Covers, against the REAL db/api code with a PyMySQL-faithful fake
cursor (``FakeCursor.execute`` now runs the ``query % args`` step, so a
literal '%…%' in SQL fails offline exactly as it fails on Render):

1. %-formatting safety: every folder/category list query, the reply
   predicate, get_inbox_counts, get_folder_counts and
   get_dashboard_summary, with seed subjects/snippets containing '%'
   (e.g. "50% off %C %s today!"). The live failure was NOT user
   content interpolated into SQL — it was literal '%CATEGORY…%'
   patterns inlined in the query text — but the seed data proves
   %-bearing mail content is safe through the same paths.
2. Counts agreement: dashboard counts, folder counts and the lists
   share the INBOX-label + latest-message ground truth.
3. Draft round-trip via TestClient: create (incl. the exact payload
   the shipped frontend sent — ``to`` as a plain string — and a
   half-typed address), list, send, delete; validation now happens at
   send time, not autosave time.
4. Search with '%' in the query string.

OFFLINE ONLY: same faking strategy as tests/test_phase1.py /
tests/test_phase2.py — the DB is faked at ``app.database.get_db_cursor``
and Gmail/drafts at the ``app.routes.api`` import bindings.

Run from the repo root with:
    /tmp/es-test-venv/bin/python -m pytest tests/test_hotfix.py -v
"""
import copy
import os
import sys
from contextlib import contextmanager
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
    DraftStore, FakeCursor, MY_EMAIL, TEST_TOKEN, default_phase2_rows,
    fake_cursor_ctx, make_row)

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

# ------------------------------------------------------------------
# Seed data: the Phase 2 rows, with %-bearing subjects/snippets
# ------------------------------------------------------------------

def percent_rows():
    rows = default_phase2_rows()
    for r in rows:
        if r["message_id"] == "m201":
            r["subject"] = "50% off %C %s today!"
            r["snippet"] = "snip 50% off %C %s today!"
        if r["message_id"] == "m209":
            r["subject"] = "100% guaranteed — act now"
            r["snippet"] = "snip 100% guaranteed"
    return rows


@pytest.fixture()
def hotfix_client(monkeypatch):
    """TestClient with the DB faked (FakeCursor) and Gmail/drafts faked.

    The FakeCursor returns the %-bearing seed rows for every fetchall
    and cycles canned fetchone values for the dashboard summary
    (latest report, lifetime total, inbox counts, folder counts,
    server-drafts count — in the order get_dashboard_summary reads
    them). The real db.list_threads / get_inbox_counts /
    get_folder_counts / get_dashboard_summary code runs.
    """
    store = DraftStore()
    calls = {"send": [], "reply": []}
    monkeypatch.setattr(settings, "ADMIN_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
    dashboard_cache.clear()
    assistant_cache.clear()

    rows = percent_rows()
    latest_row = {"report_date": "2026-11-16", "total_emails": 15,
                  "urgent_count": 1,
                  "summary_markdown": "Brief: 15 mails processed.",
                  "action_items": "[]", "created_at": None}
    inbox_row = {"unread": 2, "today": 1, "urgent": 0,
                 "needs_reply": 1, "total_threads": 3}
    folder_row = {"inbox": 3, "sent": 2, "drafts": 1, "archive": 2,
                  "trash": 1, "spam": 1, "starred": 1, "unread": 2,
                  "urgent": 0, "reply": 1, "all": 8}
    fake = FakeCursor(
        fetchall_rows=rows,
        fetchone_row=[latest_row, {"count": 57}, inbox_row, folder_row,
                      {"n": 0}])
    monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))

    def _send(to, subject, body, cc_addrs=None, bcc_addrs=None,
              attachments=None):
        calls["send"].append({"to": to, "subject": subject, "body": body,
                             "cc_addrs": list(cc_addrs or []),
                             "bcc_addrs": list(bcc_addrs or []),
                             "attachments": list(attachments or [])})
        return "sent888"

    def _reply(mid, body, reply_all=False, cc_addrs=None, bcc_addrs=None,
               attachments=None):
        calls["reply"].append({"message_id": mid, "body": body,
                              "reply_all": reply_all,
                              "cc_addrs": list(cc_addrs or []),
                              "bcc_addrs": list(bcc_addrs or []),
                              "attachments": list(attachments or [])})
        return "sent999"

    monkeypatch.setattr(api, "create_draft", store.create)
    monkeypatch.setattr(api, "get_draft", store.get)
    monkeypatch.setattr(api, "list_drafts", store.list)
    monkeypatch.setattr(api, "update_draft", store.update)
    monkeypatch.setattr(api, "delete_draft", store.delete)
    monkeypatch.setattr(api, "send_email", _send)
    monkeypatch.setattr(api, "reply_to_email", _reply)
    monkeypatch.setattr(api, "maybe_trigger_background_sync", lambda: False)

    client = TestClient(app, raise_server_exceptions=False)
    client.calls = calls  # type: ignore[attr-defined]
    client.drafts = store  # type: ignore[attr-defined]
    client.fake = fake  # type: ignore[attr-defined]
    yield client


# ------------------------------------------------------------------
# 1. %-formatting safety (DB level, real SQL, PyMySQL-faithful fake)
# ------------------------------------------------------------------

class TestPercentFormatting:
    def test_reply_predicate_is_parameterised(self):
        sql, params = db._folder_predicate_sql("reply", "p")
        assert sql.count("%s") == len(params) == 20
        assert "NOT LIKE '%" not in sql
        assert "%CATEGORY" not in sql
        assert "%noreply%" in params and "%verify your%" in params

    def test_list_threads_all_folder_cat_combos(self, monkeypatch):
        fake = FakeCursor(fetchall_rows=percent_rows())
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        for folder in db._VALID_FOLDERS:
            for cat in db._VALID_CATS:
                rows, _, _ = db.list_threads(folder=folder, cat=cat, limit=5)
                assert isinstance(rows, list)
        # The live failure, byte-identical before the fix:
        # list_threads(folder="inbox", cat="primary") raised
        # ValueError: unsupported format character 'C' (0x43) at index 855.
        primary_sql = [s for s, _ in fake.executed
                       if "CATEGORY_SOCIAL" in s or "%CATEGORY" in s]
        assert not primary_sql, "literal %CATEGORY patterns in executed SQL"
        for sql, _ in fake.executed:
            assert "NOT LIKE '%" not in sql

    def test_list_threads_percent_search_query(self, monkeypatch):
        fake = FakeCursor(fetchall_rows=percent_rows())
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        rows, _, _ = db.list_threads(folder="inbox", cat="primary",
                                     q="100% off %C", limit=5)
        assert isinstance(rows, list)

    def test_counts_format_cleanly(self, monkeypatch):
        fake = FakeCursor(
            fetchone_row=[{"unread": 2, "today": 1, "urgent": 0,
                           "needs_reply": 1, "total_threads": 3},
                          {"inbox": 3, "sent": 2, "drafts": 1, "archive": 2,
                           "trash": 1, "spam": 1, "starred": 1, "unread": 2,
                           "urgent": 0, "reply": 1, "all": 8},
                          {"n": 0}])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        counts = db.get_inbox_counts()
        assert counts["total_threads"] == 3
        folders = db.get_folder_counts()
        assert folders["inbox"] == 3 and folders["reply"] == 1

    def test_dashboard_summary_counts_not_none(self, monkeypatch):
        fake = FakeCursor(
            fetchall_rows=percent_rows(),
            fetchone_row=[{"report_date": "2026-11-16", "total_emails": 15,
                           "urgent_count": 1, "summary_markdown": "B",
                           "action_items": "[]", "created_at": None},
                          {"count": 57},
                          {"unread": 2, "today": 1, "urgent": 0,
                           "needs_reply": 1, "total_threads": 3},
                          {"inbox": 3, "sent": 2, "drafts": 1, "archive": 2,
                           "trash": 1, "spam": 1, "starred": 1, "unread": 2,
                           "urgent": 0, "reply": 1, "all": 8},
                          {"n": 0}])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        data = db.get_dashboard_summary()
        # Before the fix both counts calls raised (swallowed to None),
        # which is exactly how the live dashboard showed all zeros.
        assert data["inbox_counts"] is not None
        assert data["folder_counts"] is not None
        assert data["inbox_counts"]["total_threads"] == 3
        assert data["folder_counts"]["inbox"] == 3

    def test_save_percent_subject(self, monkeypatch):
        fake = FakeCursor(fetchall_rows=[])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        row = {"id": "m900", "thread_id": "t900",
               "sender": "Sale <sale@example.com>",
               "subject": "50% off %C %s today!",
               "snippet": "snip 50% off", "is_unread": True,
               "is_starred": False, "label_ids": ["INBOX"],
               "received_at": None}
        db.save_processed_emails([row])  # must not raise


# ------------------------------------------------------------------
# 2. Counts agreement (dashboard vs folders vs lists)
# ------------------------------------------------------------------

class TestCountsAgreement:
    def test_get_inbox_counts_latest_message_ground_truth(self, monkeypatch):
        fake = FakeCursor(fetchone_row={"unread": 2, "today": 1, "urgent": 0,
                                        "needs_reply": 1, "total_threads": 3})
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.get_inbox_counts()
        sql = fake.sql
        assert "MAX(id)" in sql and "GROUP BY tkey" in sql
        assert "COUNT(*)" in sql
        assert "COUNT(DISTINCT" not in sql
        assert "FIND_IN_SET('INBOX', COALESCE(p.labels, '')) > 0" in sql

    def test_dashboard_and_folder_totals_agree(self, hotfix_client):
        r = hotfix_client.get("/api/dashboard", headers=AUTH)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["inbox_counts"] is not None
        assert data["folder_counts"] is not None
        assert (data["inbox_counts"]["total_threads"]
                == data["folder_counts"]["inbox"] == 3)
        # Thread-based counts can never exceed the thread total.
        assert data["inbox_counts"]["unread"] <= 3
        assert data["inbox_counts"]["today"] <= 3

    def test_primary_tab_loads_with_percent_mail(self, hotfix_client):
        r = hotfix_client.get("/api/mails", headers=AUTH,
                              params={"folder": "inbox", "cat": "primary"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success"
        subjects = [m["subject"] for m in r.json()["mails"]]
        assert "50% off %C %s today!" in subjects

    def test_search_with_percent_query(self, hotfix_client):
        r = hotfix_client.get("/api/mails", headers=AUTH,
                              params={"folder": "inbox", "q": "100% off %C"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success"

    def test_reply_folder_loads(self, hotfix_client):
        r = hotfix_client.get("/api/mails", headers=AUTH,
                              params={"folder": "reply"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success"

    def test_label_change_invalidates_dashboard_cache(self, hotfix_client,
                                                      monkeypatch):
        # The five mutation sites deleted cache key "mails", which was
        # never set (the dashboard is cached under "summary"), so a
        # stale all-zeros dashboard survived mutations for up to 60s.
        dashboard_cache.set("summary", {"sentinel": True}, ttl=60)
        row = make_row(201, "m201", "t201", "Amy <amy@example.com>",
                       "Hello inbox", labels="INBOX", is_unread=True)
        monkeypatch.setattr(api, "get_email_by_id_or_msg_id",
                            lambda ident: copy.deepcopy(row))
        monkeypatch.setattr(api, "modify_message_labels",
                            lambda *a, **k: True)
        # update_mail_labels uses api's own get_db_cursor binding.
        monkeypatch.setattr(api, "get_db_cursor",
                            fake_cursor_ctx(hotfix_client.fake))
        r = hotfix_client.post("/api/mails/1201/labels", headers=AUTH,
                               json={"add": ["STARRED"], "remove": []})
        assert r.status_code == 200, r.text
        assert dashboard_cache.get("summary") is None


# ------------------------------------------------------------------
# 3. Draft round-trip (API level)
# ------------------------------------------------------------------

class TestDraftRoundTrip:
    def test_create_with_string_to_old_frontend_payload(self, hotfix_client):
        # The shipped compose UI sent `to` as a plain string; the API
        # model wants a list, so Pydantic 422'd every autosave.
        r = hotfix_client.post("/api/drafts", headers=AUTH, json={
            "to": "a@b.com, b@c.com", "subject": "Hello", "body": "x"})
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["to"] == ["a@b.com", "b@c.com"]

    def test_create_with_half_typed_address_succeeds(self, hotfix_client):
        # Autosave fires while the user is still typing; a half-typed
        # address must not fail the save (live: 400 on every keystroke).
        r = hotfix_client.post("/api/drafts", headers=AUTH, json={
            "to": ["ankit"], "subject": "Typing…", "body": "y"})
        assert r.status_code == 200, r.text

    def test_update_with_string_to(self, hotfix_client):
        hotfix_client.drafts.create(["a@b.com"], [], [], "Old", "body")
        r = hotfix_client.put("/api/drafts/1", headers=AUTH,
                              json={"to": "c@d.com"})
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["to"] == ["c@d.com"]

    def test_full_roundtrip_create_list_send_delete(self, hotfix_client):
        r = hotfix_client.post("/api/drafts", headers=AUTH, json={
            "to": ["a@b.com"], "cc": ["c@d.com"], "subject": "Hello",
            "body": "World"})
        assert r.status_code == 200, r.text
        draft_id = r.json()["draft"]["id"]
        d = hotfix_client.get("/api/drafts", headers=AUTH).json()
        assert [x["id"] for x in d["drafts"]] == [draft_id]
        r = hotfix_client.post(f"/api/drafts/{draft_id}/send", headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["sent_message_id"] == "sent888"
        assert hotfix_client.calls["send"][0]["to"] == ["a@b.com"]
        assert hotfix_client.calls["send"][0]["cc_addrs"] == ["c@d.com"]
        assert hotfix_client.get(f"/api/drafts/{draft_id}",
                                 headers=AUTH).status_code == 404
        hotfix_client.drafts.create(["z@z.com"], [], [], "Second", "b")
        r = hotfix_client.delete("/api/drafts/2", headers=AUTH)
        assert r.status_code == 200, r.text
        assert hotfix_client.get("/api/drafts/2",
                                 headers=AUTH).status_code == 404

    def test_send_with_bad_address_fails_and_keeps_draft(self, hotfix_client):
        hotfix_client.post("/api/drafts", headers=AUTH, json={
            "to": ["not-an-email"], "body": "x"})
        r = hotfix_client.post("/api/drafts/1/send", headers=AUTH)
        assert r.status_code == 400, r.text
        assert hotfix_client.get("/api/drafts/1",
                                 headers=AUTH).status_code == 200
        assert hotfix_client.calls["send"] == []

    def test_drafts_folder_lists_server_draft(self, hotfix_client):
        hotfix_client.post("/api/drafts", headers=AUTH, json={
            "to": ["a@b.com"], "subject": "Hello", "body": "World"})
        r = hotfix_client.get("/api/mails", headers=AUTH,
                              params={"folder": "drafts"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success"
