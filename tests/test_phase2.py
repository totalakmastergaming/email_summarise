"""Phase 2 offline test suite for InboxCura / email_summarise.

Covers: label-based folders (Sent/Drafts/Archive/Trash/Spam), the
INBOX re-scope of get_inbox_counts(), get_folder_counts() agreement
with list_threads(), save_processed_emails flag derivation, server
drafts (CRUD + send), attachment metadata + download proxy, CC/BCC +
multipart FormData send/reply, forward re-attachment, thread restore,
and auth on the new endpoints.

OFFLINE ONLY: same faking strategy as tests/test_phase1.py — the DB
is faked at ``app.database.get_db_cursor`` (SQL text asserted
directly) and at the ``app.routes.api`` import bindings; Gmail is
faked at the ``app.services.gmail_service`` bindings. Nothing here
proves live behaviour on Render.

Run from the repo root with:
    /tmp/es-test-venv/bin/python -m pytest tests/test_phase2.py -v
(python-multipart must be installed in the venv — Starlette needs it
for request.form() on the multipart tests.)
"""
import ast
import copy
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)  # app.main mounts StaticFiles with relative dirs

import app.database as db
import app.routes.api as api
import app.services.gmail_service as gs
from app.cache import assistant_cache, dashboard_cache
from app.config import settings
from app.main import app
from fastapi.testclient import TestClient

TEST_TOKEN = "test-token"
MY_EMAIL = "me@example.com"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

# ------------------------------------------------------------------
# Canned data (label-based — Phase 2 folders read the labels CSV)
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

def default_phase2_rows():
    """One row per thread. Labels are the folder ground truth:
    - m201 INBOX only -> inbox (+ primary)
    - m202 INBOX+SENT -> inbox AND sent
    - m203 SENT only -> sent + archive (no INBOX/TRASH/SPAM/DRAFT)
    - m204 INBOX+TRASH -> trash only
    - m205 INBOX+SPAM -> spam only
    - m206 no labels at all -> archive only
    - m207 DRAFT -> drafts (Gmail draft)
    - m208 INBOX+STARRED -> inbox + starred
    - m209 INBOX+UNREAD -> inbox + unread
    """
    return [
        make_row(209, "m209", "t209", "Ivy <ivy@example.com>", "Unread note",
                 labels="INBOX,UNREAD", is_unread=True),
        make_row(208, "m208", "t208", "Hank <hank@example.com>", "Starred plan",
                 labels="INBOX,STARRED", is_unread=False, is_starred=True),
        make_row(207, "m207", "t207", "Me", "Draft memo",
                 labels="DRAFT", is_unread=False),
        make_row(206, "m206", "t206", "Gail <gail@example.com>", "Old archived",
                 labels="", is_unread=False, is_archived=True),
        make_row(205, "m205", "t205", "Fay <fay@example.com>", "Win a prize",
                 labels="INBOX,SPAM", is_unread=False),
        make_row(204, "m204", "t204", "Eli <eli@example.com>", "Deleted note",
                 labels="INBOX,TRASH", is_unread=False, is_trashed=True),
        make_row(203, "m203", "t203", "Me", "Sent report",
                 labels="SENT", is_unread=False, is_archived=True),
        make_row(202, "m202", "t202", "Me", "Sent reply",
                 labels="INBOX,SENT", is_unread=False),
        make_row(201, "m201", "t201", "Amy <amy@example.com>", "Hello inbox",
                 labels="INBOX", is_unread=True),
    ]

def _labels_of(r):
    return {p.strip() for p in str(r.get("labels") or "").split(",") if p.strip()}

def _folder_match(r, folder):
    """Python mirror of db._folder_predicate_sql() semantics."""
    labels = _labels_of(r)
    inbox_base = "INBOX" in labels and "TRASH" not in labels and "SPAM" not in labels
    if folder == "inbox":
        return inbox_base
    if folder == "sent":
        return "SENT" in labels
    if folder == "drafts":
        return "DRAFT" in labels
    if folder == "archive":
        return ("INBOX" not in labels and "TRASH" not in labels
                and "SPAM" not in labels and "DRAFT" not in labels)
    if folder == "trash":
        return "TRASH" in labels
    if folder == "spam":
        return "SPAM" in labels
    if folder == "starred":
        return "STARRED" in labels and "TRASH" not in labels
    if folder == "unread":
        return "UNREAD" in labels and inbox_base
    if folder == "urgent":
        return "urgent" in r["subject"].lower() and inbox_base
    if folder == "reply":
        return (r["is_unread"] and inbox_base
                and "noreply" not in r["sender"].lower())
    if folder == "all":
        return "TRASH" not in labels
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

# ------------------------------------------------------------------
# Fake DB cursor (for app.database-level tests)
# ------------------------------------------------------------------

class FakeCursor:
    """Records the SQL + params of every execute; returns canned rows.
    fetchone_row may be a single dict (returned every call) or a list
    (popped once per fetchone call, for multi-query functions)."""
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
        # HOTFIX 2026-11-16: PyMySQL-faithful check. Cursor.execute runs
        # ``query % escaped_args`` (pymysql/cursors.py mogrify), so a
        # literal '%…%' in the SQL string raises ValueError even when
        # the %s count matches — that is exactly how the Primary tab
        # and the counts broke live while 139/139 tests passed. Dummy
        # replacements stand in for the escaped args; any stray '%'
        # fails here the same way it fails on Render.
        sql % tuple("'x'" for _ in params)
        self.executed.append((sql, params))

    def executemany(self, sql, values):
        for v in values:
            self.execute(sql, v)

    def fetchall(self):
        return copy.deepcopy(self._fetchall_rows)

    def fetchone(self):
        if isinstance(self._fetchone_row, list):
            if not self._fetchone_row:
                return None
            return copy.deepcopy(self._fetchone_row.pop(0))
        if self._fetchone_row is not None:
            return copy.deepcopy(self._fetchone_row)
        return None

    @property
    def sql(self):
        return self.executed[-1][0] if self.executed else ""

    @property
    def params(self):
        return self.executed[-1][1] if self.executed else ()

    @property
    def all_sql(self):
        return "\n".join(sql for sql, _ in self.executed)

def fake_cursor_ctx(fake):
    @contextmanager
    def _ctx(commit=False):
        yield fake
    return _ctx

# ------------------------------------------------------------------
# In-memory drafts store (fakes the db drafts functions at api level)
# ------------------------------------------------------------------

class DraftStore:
    """Mirrors app.database draft functions over a dict; ids start at 1."""
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def _shape(self, row):
        return copy.deepcopy(row)

    def create(self, to_addrs, cc_addrs, bcc_addrs, subject, body,
               thread_id=None, message_id=None):
        row = {
            "id": self.next_id, "to": list(to_addrs), "cc": list(cc_addrs),
            "bcc": list(bcc_addrs), "subject": subject or "",
            "body": body or "", "thread_id": thread_id,
            "message_id": message_id,
            "created_at": "2026-11-16T08:00:00",
            "updated_at": "2026-11-16T08:00:00",
        }
        self.rows[self.next_id] = row
        self.next_id += 1
        return self._shape(row)

    def get(self, draft_id):
        row = self.rows.get(int(draft_id))
        return self._shape(row) if row else None

    def list(self, limit=30, cursor=None):
        limit = max(1, min(100, int(limit or 30)))
        last_id = db._decode_cursor(cursor, "drafts_list", None, None) if cursor else None
        pool = sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)
        if last_id is not None:
            pool = [r for r in pool if r["id"] < last_id]
        window = pool[:limit + 1]
        has_more = len(window) > limit
        page = [self._shape(r) for r in window[:limit]]
        nxt = (db._encode_cursor(page[-1]["id"], "drafts_list", None, None)
               if has_more and page else None)
        return page, nxt, has_more

    def update(self, draft_id, updates):
        row = self.rows.get(int(draft_id))
        if row is None:
            return None
        for key, value in (updates or {}).items():
            if key in ("to", "cc", "bcc"):
                row[key] = list(value) if isinstance(value, (list, tuple)) else [value]
            elif key in ("subject", "body", "thread_id", "message_id"):
                row[key] = value
        return self._shape(row)

    def delete(self, draft_id):
        return self.rows.pop(int(draft_id), None) is not None

    def union_row(self, draft):
        """One server draft shaped like a _list_draft_threads union row."""
        return {
            "id": draft["id"] + db._DRAFT_SORT_ID_OFFSET,
            "message_id": None, "thread_id": draft.get("thread_id"),
            "sender": "Me", "subject": draft.get("subject") or "",
            "snippet": (draft.get("body") or "")[:200],
            "is_unread": False, "is_starred": False, "labels": "DRAFT",
            "is_archived": False, "is_trashed": False,
            "received_at": None, "created_at": None, "updated_at": None,
            "message_count": 1, "unread_count": 0,
            "is_draft": 1, "draft_id": draft["id"],
            "to_addrs": ",".join(draft.get("to") or []),
            "cc_addrs": ",".join(draft.get("cc") or []),
            "bcc_addrs": ",".join(draft.get("bcc") or []),
            "body_text": draft.get("body") or "",
        }

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture()
def phase2_rows():
    return default_phase2_rows()

@pytest.fixture()
def api_client(monkeypatch, phase2_rows):
    """TestClient with every DB/Gmail call faked (Phase 2 version).

    Extra handles vs the Phase 1 fixture:
    - client.drafts: the in-memory DraftStore
    - client.calls["send"]/["reply"]: dicts incl. cc/bcc/attachments
    - attachment behaviour keyed by attachment id (att1 ok,
      att-missing 404, att-big 413, att-boom 502)
    """
    calls = {
        "batch": [], "local_state": [], "gmail_state": [],
        "reply": [], "send": [], "background_sync": [],
    }
    store = DraftStore()
    monkeypatch.setattr(settings, "ADMIN_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(settings, "MY_EMAIL", MY_EMAIL)
    dashboard_cache.clear()
    assistant_cache.clear()

    singles = {r["message_id"]: r for r in phase2_rows}
    singles.update({str(1000 + r["id"]): r for r in phase2_rows})
    thread_messages = {
        "t201": [make_row(200, "m201a", "t201", "Amy <amy@example.com>",
                          "Hello inbox", labels="INBOX", is_unread=False),
                 make_row(201, "m201", "t201", "Amy <amy@example.com>",
                          "Hello inbox", labels="INBOX", is_unread=True)],
        "t202": [dict(r) for r in phase2_rows if r["message_id"] == "m202"],
    }

    def _get_email_by_id_or_msg_id(identifier):
        return copy.deepcopy(singles.get(str(identifier)))

    def _get_thread_messages(tid):
        return copy.deepcopy(thread_messages.get(tid, []))

    def _get_email_full(message_id):
        return {"thread_id": "t201", "sender": "Amy <amy@example.com>",
                "subject": "Hello inbox", "date": "Mon, 16 Nov 2026",
                "received_at": None, "snippet": "snip",
                "body_text": "FULL BODY", "body_html": "<p>FULL BODY</p>",
                "label_ids": ["INBOX"],
                "attachments": [
                    {"filename": "report.pdf", "mimeType": "application/pdf",
                     "size": 1234, "attachmentId": "att1", "partId": "1.2"},
                    {"filename": "huge.zip", "mimeType": "application/zip",
                     "size": 11 * 1024 * 1024, "attachmentId": "att2",
                     "partId": "1.3"},
                ]}

    def _get_attachment_bytes(message_id, attachment_id):
        if attachment_id == "att1":
            return b"PDFBYTES", "report.pdf", "application/pdf"
        if attachment_id == "att-big":
            raise ValueError(
                "Attachment 'big.bin' is 11534336 bytes, over the 10 MB download cap.")
        if attachment_id == "att-boom":
            raise RuntimeError("gmail down")
        raise ValueError(
            f"Attachment {attachment_id!r} not found on message {message_id}.")

    def _get_message_attachments(message_id):
        return [
            {"filename": "report.pdf", "mimeType": "application/pdf",
             "size": 1234, "attachmentId": "att1", "partId": "1.2"},
            {"filename": "huge.zip", "mimeType": "application/zip",
             "size": 11 * 1024 * 1024, "attachmentId": "att2", "partId": "1.3"},
        ]

    def _fetch_forward_attachments(message_id, attachment_ids):
        attached, skipped = [], []
        for aid in attachment_ids:
            if aid == "att1":
                attached.append(("report.pdf", "application/pdf", b"PDFBYTES"))
            elif aid == "att2":
                skipped.append({"attachment_id": "att2", "filename": "huge.zip",
                                "reason": "Would exceed the 10 MB total forward cap."})
            else:
                skipped.append({"attachment_id": aid, "filename": "",
                                "reason": "Not found on the original message."})
        return attached, skipped

    def _build_forward_draft(message_id):
        return {
            "message_id": message_id, "thread_id": "t201", "to": "",
            "subject": "Fwd: Hello inbox",
            "body": "\n\n---------- Forwarded message ----------\nFULL BODY",
            "attachments": [
                {"attachment_id": "att1", "filename": "report.pdf",
                 "mimeType": "application/pdf", "size": 1234},
                {"attachment_id": "att2", "filename": "huge.zip",
                 "mimeType": "application/zip", "size": 11 * 1024 * 1024},
            ],
            "attached_ids": ["att1"],
            "skipped": [{"attachment_id": "att2", "filename": "huge.zip",
                         "reason": "Would exceed the 10 MB total forward cap."}],
            "total_attachment_bytes": 1234,
        }

    def _fake_list_threads(folder="inbox", cat=None, q=None, limit=30, cursor=None):
        if folder not in db._VALID_FOLDERS:
            raise ValueError(f"Unknown folder: {folder}")
        if cat not in db._VALID_CATS:
            raise ValueError(f"Unknown category: {cat}")
        limit = max(1, min(100, int(limit or 30)))
        q = (q or "").strip() or None
        last_id = db._decode_cursor(cursor, folder, cat, q) if cursor else None
        if folder == "drafts":
            pool = [r for r in phase2_rows if _folder_match(r, "drafts")]
            pool = [dict(r, is_draft=0, draft_id=None, to_addrs=None,
                         cc_addrs=None, bcc_addrs=None, body_text=None)
                    for r in pool]
            pool += [store.union_row(d) for d in store.rows.values()]
            pool.sort(key=lambda r: r["id"], reverse=True)
            if q:
                pool = [r for r in pool
                        if _q_match(r, q) or q.lower() in (r.get("body_text") or "").lower()]
        else:
            pool = [r for r in phase2_rows
                    if _folder_match(r, folder) and _cat_match(r, cat) and _q_match(r, q)]
            pool.sort(key=lambda r: r["id"], reverse=True)
        if last_id is not None:
            pool = [r for r in pool if r["id"] < last_id]
        window = pool[:limit + 1]
        has_more = len(window) > limit
        page = window[:limit]
        nxt = db._encode_cursor(page[-1]["id"], folder, cat, q) if has_more and page else None
        return page, nxt, has_more

    def _batch(ids, **kw):
        calls["batch"].append((tuple(ids), dict(kw)))
        return True

    def _local_state(**kw):
        calls["local_state"].append(dict(kw))
        return True

    def _gmail_state(**kw):
        calls["gmail_state"].append(dict(kw))
        return True

    def _reply(mid, body, reply_all=False, cc_addrs=None, bcc_addrs=None,
               attachments=None):
        calls["reply"].append({"message_id": mid, "body": body,
                               "reply_all": reply_all,
                               "cc_addrs": list(cc_addrs or []),
                               "bcc_addrs": list(bcc_addrs or []),
                               "attachments": list(attachments or [])})
        return "sent999"

    def _send(to, subject, body, cc_addrs=None, bcc_addrs=None,
              attachments=None):
        calls["send"].append({"to": to, "subject": subject, "body": body,
                              "cc_addrs": list(cc_addrs or []),
                              "bcc_addrs": list(bcc_addrs or []),
                              "attachments": list(attachments or [])})
        return "sent888"

    monkeypatch.setattr(api, "list_threads", _fake_list_threads)
    monkeypatch.setattr(api, "get_thread_messages", _get_thread_messages)
    monkeypatch.setattr(api, "get_email_by_id_or_msg_id", _get_email_by_id_or_msg_id)
    monkeypatch.setattr(api, "get_email_full", _get_email_full)
    monkeypatch.setattr(api, "get_attachment_bytes", _get_attachment_bytes)
    monkeypatch.setattr(api, "get_message_attachments", _get_message_attachments)
    monkeypatch.setattr(api, "fetch_forward_attachments", _fetch_forward_attachments)
    monkeypatch.setattr(api, "build_forward_draft", _build_forward_draft)
    monkeypatch.setattr(api, "batch_update_message_states", _batch)
    monkeypatch.setattr(api, "update_processed_email_state", _local_state)
    monkeypatch.setattr(api, "update_gmail_message_state", _gmail_state)
    monkeypatch.setattr(api, "reply_to_email", _reply)
    monkeypatch.setattr(api, "send_email", _send)
    monkeypatch.setattr(api, "create_draft", store.create)
    monkeypatch.setattr(api, "get_draft", store.get)
    monkeypatch.setattr(api, "list_drafts", store.list)
    monkeypatch.setattr(api, "update_draft", store.update)
    monkeypatch.setattr(api, "delete_draft", store.delete)
    monkeypatch.setattr(api, "get_folder_counts", lambda: {
        "inbox": 3, "sent": 2, "drafts": 1, "archive": 2, "trash": 1,
        "spam": 1, "starred": 1, "unread": 2, "urgent": 0, "reply": 1,
        "all": 8})
    monkeypatch.setattr(api, "run_incremental_sync", lambda: {
        "status": "success", "mode": "incremental",
        "processed": {"added": 0, "deleted": 0, "label_changes": 0},
        "has_more": False})
    monkeypatch.setattr(api, "get_sync_checkpoint", lambda: None)
    monkeypatch.setattr(api, "get_dashboard_summary", lambda: {
        "latest": None, "history": [], "recent_emails": [],
        "lifetime_emails": 0,
        "inbox_counts": {"unread": 2, "today": 1, "urgent": 0,
                         "needs_reply": 1, "total_threads": 3}})
    monkeypatch.setattr(api, "maybe_trigger_background_sync",
                        lambda: calls["background_sync"].append(True) or False)

    client = TestClient(app, raise_server_exceptions=False)
    client.calls = calls  # type: ignore[attr-defined]
    client.drafts = store  # type: ignore[attr-defined]
    yield client

@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}

# ------------------------------------------------------------------
# 1. Folder predicates at the SQL layer (real _folder_predicate_sql /
#    list_threads with a FakeCursor)
# ------------------------------------------------------------------

class TestFolderPredicatesSQL:
    def test_predicate_fragments_per_folder(self):
        # (folder, must-contain fragments, must-NOT-contain fragments)
        cases = {
            "inbox": (["FIND_IN_SET('INBOX', COALESCE(p.labels, '')) > 0",
                       "FIND_IN_SET('TRASH', COALESCE(p.labels, '')) = 0",
                       "FIND_IN_SET('SPAM', COALESCE(p.labels, '')) = 0"], []),
            "sent": (["FIND_IN_SET('SENT', COALESCE(p.labels, '')) > 0"], []),
            "drafts": (["FIND_IN_SET('DRAFT', COALESCE(p.labels, '')) > 0"], []),
            "archive": (["FIND_IN_SET('INBOX', COALESCE(p.labels, '')) = 0",
                         "FIND_IN_SET('TRASH', COALESCE(p.labels, '')) = 0",
                         "FIND_IN_SET('SPAM', COALESCE(p.labels, '')) = 0",
                         "FIND_IN_SET('DRAFT', COALESCE(p.labels, '')) = 0"], []),
            "trash": (["FIND_IN_SET('TRASH', COALESCE(p.labels, '')) > 0"], []),
            "spam": (["FIND_IN_SET('SPAM', COALESCE(p.labels, '')) > 0"], []),
            "starred": (["FIND_IN_SET('STARRED', COALESCE(p.labels, '')) > 0",
                         "FIND_IN_SET('TRASH', COALESCE(p.labels, '')) = 0"], []),
            "unread": (["FIND_IN_SET('UNREAD', COALESCE(p.labels, '')) > 0",
                        "FIND_IN_SET('INBOX', COALESCE(p.labels, '')) > 0"], []),
            "all": (["FIND_IN_SET('TRASH', COALESCE(p.labels, '')) = 0"], []),
        }
        for folder, (must, must_not) in cases.items():
            sql, params = db._folder_predicate_sql(folder)
            for frag in must:
                assert frag in sql, f"{folder}: missing {frag!r} in {sql!r}"
            for frag in must_not:
                assert frag not in sql, f"{folder}: unexpected {frag!r}"
            # No stale-flag SQL anywhere in a folder predicate.
            assert "is_archived" not in sql and "is_trashed" not in sql, folder

    def test_predicate_uses_exact_token_match_not_like(self):
        # LIKE '%INBOX%' would also match a hypothetical 'XINBOX' token;
        # FIND_IN_SET matches whole tokens only.
        for folder in ("inbox", "sent", "drafts", "archive", "trash", "spam",
                       "starred", "unread", "all"):
            sql, _ = db._folder_predicate_sql(folder)
            assert "LIKE '%INBOX%'" not in sql
            assert "LIKE '%TRASH%'" not in sql
            assert "LIKE '%SPAM%'" not in sql

    def test_unknown_folder_raises(self):
        with pytest.raises(ValueError):
            db._folder_predicate_sql("nope")
        with pytest.raises(ValueError):
            db.list_threads(folder="nope")

    def _run_list(self, monkeypatch, **kw):
        fake = FakeCursor(fetchall_rows=[
            {"id": 5, "message_id": "m5", "thread_id": "t1",
             "message_count": 2, "unread_count": 1},
        ])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        rows, nxt, more = db.list_threads(**kw)
        return fake, rows, nxt, more

    def test_new_folder_sql_no_offset(self, monkeypatch):
        for folder in ("sent", "drafts", "archive", "trash", "spam"):
            fake, rows, nxt, more = self._run_list(monkeypatch, folder=folder)
            assert "OFFSET" not in fake.sql.upper(), folder
            assert fake.params[-1] == 31  # limit + 1
            assert len(rows) == 1 and more is False and nxt is None

    def test_drafts_folder_sql_is_union_with_offset_ids(self, monkeypatch):
        fake, _, _, _ = self._run_list(monkeypatch, folder="drafts")
        assert "UNION ALL" in fake.sql
        assert "FROM drafts d" in fake.sql
        assert str(db._DRAFT_SORT_ID_OFFSET) in fake.sql
        assert "is_draft" in fake.sql and "draft_id" in fake.sql
        assert "OFFSET" not in fake.sql.upper()

    def test_drafts_folder_sql_q_and_cursor(self, monkeypatch):
        fake = FakeCursor(fetchall_rows=[])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.list_threads(folder="drafts", q="hello", limit=10,
                        cursor=db._encode_cursor(2_000_000_005, "drafts", None, "hello"))
        assert "p.id < %s" in fake.sql
        assert f"(d.id + {db._DRAFT_SORT_ID_OFFSET}) < %s" in fake.sql
        assert fake.sql.count("%s") == len(fake.params)

    def test_no_sql_offset_string_in_database_module(self):
        # The word OFFSET may only appear in comments/docstrings and in
        # the _DRAFT_SORT_ID_OFFSET constant name — never inside an SQL
        # string literal (all SQL is built from string literals here).
        src = Path(db.__file__).read_text()
        tree = ast.parse(src)
        docstring_nodes = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                first = body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstring_nodes.add(id(first.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstring_nodes):
                assert "OFFSET" not in node.value.upper(), (
                    f"SQL string literal contains OFFSET: {node.value[:120]!r}")

# ------------------------------------------------------------------
# 2. Counts: get_inbox_counts re-scope + get_folder_counts agreement
# ------------------------------------------------------------------

class TestCountsSQL:
    def test_get_inbox_counts_uses_inbox_label_not_flags(self, monkeypatch):
        fake = FakeCursor(fetchone_row={"unread": 1, "today": 2, "urgent": 0,
                                        "needs_reply": 1, "total_threads": 3})
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        counts = db.get_inbox_counts()
        assert counts == {"unread": 1, "today": 2, "urgent": 0,
                          "needs_reply": 1, "total_threads": 3}
        # Re-scoped base: INBOX label present, TRASH/SPAM absent.
        assert "FIND_IN_SET('INBOX', COALESCE(p.labels, '')) > 0" in fake.sql
        assert "FIND_IN_SET('TRASH', COALESCE(p.labels, '')) = 0" in fake.sql
        assert "FIND_IN_SET('SPAM', COALESCE(p.labels, '')) = 0" in fake.sql
        # The old unfiltered base (flags, no INBOX test) is gone.
        assert "is_archived = FALSE" not in fake.sql
        assert "is_trashed = FALSE" not in fake.sql
        # HOTFIX 2026-11-16: counts run over the thread's LATEST message
        # only (same subquery as list_threads), so the sums and the
        # thread total share one ground truth.
        assert "MAX(id)" in fake.sql
        assert "GROUP BY tkey" in fake.sql
        assert "COUNT(*)" in fake.sql
        assert "COUNT(DISTINCT COALESCE(thread_id, message_id))" not in fake.sql

    def test_get_folder_counts_uses_same_predicates_as_list(self, monkeypatch):
        counts_row = {"inbox": 5, "sent": 2, "drafts": 1, "archive": 3,
                      "trash": 1, "spam": 0, "starred": 2, "unread": 4,
                      "urgent": 1, "reply": 2, "all": 12}
        fake = FakeCursor(fetchone_row=[counts_row, {"n": 2}])
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        counts = db.get_folder_counts()
        # Drafts count = Gmail-DRAFT threads (1) + server drafts (2).
        assert counts["drafts"] == 3
        assert counts["inbox"] == 5 and counts["sent"] == 2
        assert counts["trash"] == 1 and counts["spam"] == 0
        # Same helper: every folder's predicate fragment appears verbatim
        # in the aggregate query, so list and count cannot disagree.
        sql = fake.executed[0][0]
        for folder in ("inbox", "sent", "drafts", "archive", "trash",
                       "spam", "starred", "unread", "urgent", "reply", "all"):
            pred_sql, _ = db._folder_predicate_sql(folder, "p")
            assert pred_sql in sql, f"{folder} predicate missing from counts SQL"
        assert "OFFSET" not in sql.upper()
        # Second query is the server-drafts count.
        assert fake.executed[1][0].strip().startswith("SELECT COUNT(*) AS n FROM drafts")

    def test_folders_endpoint_returns_counts(self, api_client, auth_headers):
        r = api_client.get("/api/folders", headers=auth_headers)
        assert r.status_code == 200, r.text
        folders = r.json()["folders"]
        assert folders["inbox"] == 3 and folders["sent"] == 2
        assert folders["drafts"] == 1 and folders["archive"] == 2
        assert folders["trash"] == 1 and folders["spam"] == 1
        assert set(folders) == {"inbox", "sent", "drafts", "archive", "trash",
                                "spam", "starred", "unread", "urgent",
                                "reply", "all"}

# ------------------------------------------------------------------
# 3. save_processed_emails flag derivation
# ------------------------------------------------------------------

class TestSaveProcessedFlags:
    def _run(self, monkeypatch, rows):
        fake = FakeCursor()
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.save_processed_emails(rows)
        return fake

    def _row(self, mid, label_ids):
        return {"id": mid, "thread_id": "t-" + mid, "sender": "a@b.c",
                "subject": "s", "snippet": "", "is_unread": True,
                "is_starred": False, "label_ids": label_ids,
                "received_at": None}

    def test_flags_derived_on_insert(self, monkeypatch):
        fake = self._run(monkeypatch, [
            self._row("m-inbox", ["INBOX"]),
            self._row("m-sent", ["SENT"]),
            self._row("m-trash", ["INBOX", "TRASH"]),
            self._row("m-trash-only", ["TRASH"]),
            self._row("m-nolabels", []),
        ])
        # INSERT column list carries both mirror flags.
        sql = fake.executed[0][0]
        assert "is_archived" in sql and "is_trashed" in sql
        # values tuples: (..., is_archived, is_trashed, received_at)
        by_id = {v[0]: v for _, v in fake.executed}
        assert by_id["m-inbox"][8] is False and by_id["m-inbox"][9] is False
        assert by_id["m-sent"][8] is True and by_id["m-sent"][9] is False
        assert by_id["m-trash"][8] is False and by_id["m-trash"][9] is True
        assert by_id["m-trash-only"][8] is True and by_id["m-trash-only"][9] is True
        assert by_id["m-nolabels"][8] is True and by_id["m-nolabels"][9] is False

    def test_flags_derived_on_duplicate_key_update(self, monkeypatch):
        fake = self._run(monkeypatch, [self._row("m-sent", ["SENT"])])
        sql = fake.executed[0][0]
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "is_archived = VALUES(is_archived)" in sql
        assert "is_trashed = VALUES(is_trashed)" in sql
        assert "labels = VALUES(labels)" in sql

    def test_labels_csv_stored(self, monkeypatch):
        fake = self._run(monkeypatch, [self._row("m-x", ["INBOX", "SENT"])])
        by_id = {v[0]: v for _, v in fake.executed}
        assert by_id["m-x"][7] == "INBOX,SENT"

# ------------------------------------------------------------------
# 4. Drafts at the DB layer (real functions + FakeCursor)
# ------------------------------------------------------------------

def _draft_db_row(**over):
    row = {"id": 7, "to_addrs": "a@b.com", "cc_addrs": "", "bcc_addrs": "",
           "subject": "Hi", "body": "Body", "thread_id": None,
           "message_id": None, "created_at": datetime(2026, 11, 16, 8, 0, 0),
           "updated_at": datetime(2026, 11, 16, 8, 0, 0)}
    row.update(over)
    return row

class TestDraftsDB:
    def test_create_draft_sql_and_shape(self, monkeypatch):
        fake = FakeCursor(fetchone_row=_draft_db_row())
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        draft = db.create_draft(["a@b.com"], [], [], "Hi", "Body")
        assert "INSERT INTO drafts" in fake.executed[0][0]
        assert "SELECT * FROM drafts WHERE id = %s" in fake.executed[1][0]
        assert draft["id"] == 7 and draft["to"] == ["a@b.com"]
        assert draft["subject"] == "Hi" and draft["body"] == "Body"
        assert draft["created_at"] == "2026-11-16T08:00:00"

    def test_get_draft_none_when_missing(self, monkeypatch):
        fake = FakeCursor(fetchone_row=None)
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        assert db.get_draft(123) is None

    def test_list_drafts_keyset_and_own_cursor_namespace(self, monkeypatch):
        rows = [_draft_db_row(id=3), _draft_db_row(id=2), _draft_db_row(id=1)]
        fake = FakeCursor(fetchall_rows=rows)
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        page, nxt, more = db.list_drafts(limit=2)
        assert [d["id"] for d in page] == [3, 2]
        assert more is True and nxt is not None
        # The cursor belongs to the drafts_list namespace, not the
        # drafts-FOLDER namespace: a folder cursor must be rejected.
        assert db._decode_cursor(nxt, "drafts_list", None, None) == 2
        with pytest.raises(ValueError):
            db._decode_cursor(nxt, "drafts", None, None)
        with pytest.raises(ValueError):
            db.list_drafts(cursor=db._encode_cursor(2, "drafts", None, None))
        assert "OFFSET" not in fake.sql.upper()
        assert "ORDER BY id DESC" in fake.sql

    def test_update_draft_whitelists_columns(self, monkeypatch):
        fake = FakeCursor(fetchone_row=_draft_db_row(subject="New"))
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        updated = db.update_draft(7, {"subject": "New", "bogus_col": 1,
                                      "to": ["x@y.com"]})
        assert updated["subject"] == "New"
        update_sql = fake.executed[0][0]
        assert "UPDATE drafts SET" in update_sql
        assert "bogus_col" not in update_sql
        assert "to_addrs = %s" in update_sql

    def test_update_draft_no_valid_fields_just_reads(self, monkeypatch):
        fake = FakeCursor(fetchone_row=_draft_db_row())
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        assert db.update_draft(7, {"bogus": 1}) is not None
        assert len(fake.executed) == 1  # only the SELECT, no UPDATE
        assert "UPDATE" not in fake.executed[0][0]

    def test_update_draft_missing_returns_none(self, monkeypatch):
        fake = FakeCursor(fetchone_row=None)
        fake.rowcount = 0
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        assert db.update_draft(999, {"subject": "x"}) is None

    def test_delete_draft_bool(self, monkeypatch):
        fake = FakeCursor()
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        assert db.delete_draft(7) is True
        assert "DELETE FROM drafts WHERE id = %s" in fake.sql
        fake.rowcount = 0
        assert db.delete_draft(7) is False

# ------------------------------------------------------------------
# 5. Folders at the API level
# ------------------------------------------------------------------

class TestFoldersAPI:
    def test_sent_folder(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "sent", "limit": 100}).json()
        assert [m["id"] for m in d["mails"]] == [1203, 1202]

    def test_trash_folder(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "trash", "limit": 100}).json()
        assert [m["id"] for m in d["mails"]] == [1204]

    def test_spam_folder(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "spam", "limit": 100}).json()
        assert [m["id"] for m in d["mails"]] == [1205]

    def test_archive_folder_excludes_inbox_trash_spam_draft(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "archive", "limit": 100}).json()
        assert [m["id"] for m in d["mails"]] == [1206, 1203]  # no-labels + SENT-only

    def test_drafts_folder_unions_server_drafts_first(self, api_client, auth_headers):
        api_client.drafts.create(["d@e.com"], [], [], "Server draft", "hello")
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "drafts", "limit": 100}).json()
        mails = d["mails"]
        assert mails[0]["is_draft"] is True
        assert mails[0]["id"] == "draft-1"
        assert mails[0]["draft_id"] == 1
        assert mails[0]["subject"] == "Server draft"
        assert mails[0]["to"] == ["d@e.com"]
        # Gmail draft row follows, as an ordinary mail row.
        assert mails[1]["id"] == 1207 and mails[1].get("is_draft") is not True

    def test_drafts_folder_cursor_roundtrip(self, api_client, auth_headers):
        api_client.drafts.create(["a@b.com"], [], [], "D1", "b1")
        api_client.drafts.create(["c@d.com"], [], [], "D2", "b2")
        d1 = api_client.get("/api/mails", headers=auth_headers,
                            params={"folder": "drafts", "limit": 2}).json()
        assert d1["has_more"] is True and d1["next_cursor"]
        assert [m["id"] for m in d1["mails"]] == ["draft-2", "draft-1"]
        d2 = api_client.get("/api/mails", headers=auth_headers,
                            params={"folder": "drafts", "limit": 2,
                                    "cursor": d1["next_cursor"]}).json()
        assert [m["id"] for m in d2["mails"]] == [1207]
        assert d2["has_more"] is False

    def test_cursor_mismatch_400_for_new_folders(self, api_client, auth_headers):
        d1 = api_client.get("/api/mails", headers=auth_headers,
                            params={"folder": "sent", "limit": 1}).json()
        assert d1["next_cursor"], d1
        for folder in ("trash", "spam", "archive", "drafts", "inbox"):
            r = api_client.get("/api/mails", headers=auth_headers, params={
                "folder": folder, "limit": 1, "cursor": d1["next_cursor"]})
            assert r.status_code == 400, (folder, r.text)

    def test_inbox_excludes_sent_only_and_trash_and_spam(self, api_client, auth_headers):
        d = api_client.get("/api/mails", headers=auth_headers,
                           params={"folder": "inbox", "limit": 100}).json()
        ids = [m["id"] for m in d["mails"]]
        assert ids == [1209, 1208, 1202, 1201]  # unread, starred, sent+inbox, inbox
        assert 1203 not in ids and 1204 not in ids and 1205 not in ids

# ------------------------------------------------------------------
# 6. Drafts at the API level (CRUD + send)
# ------------------------------------------------------------------

class TestDraftsAPI:
    def test_create_and_get(self, api_client, auth_headers):
        r = api_client.post("/api/drafts", headers=auth_headers, json={
            "to": ["a@b.com"], "cc": ["c@d.com"], "subject": "Hello",
            "body": "World"})
        assert r.status_code == 200, r.text
        draft = r.json()["draft"]
        assert draft["id"] == 1 and draft["to"] == ["a@b.com"]
        assert draft["cc"] == ["c@d.com"] and draft["subject"] == "Hello"
        r = api_client.get("/api/drafts/1", headers=auth_headers)
        assert r.status_code == 200 and r.json()["draft"]["id"] == 1
        r = api_client.get("/api/drafts/999", headers=auth_headers)
        assert r.status_code == 404

    def test_create_empty_draft_400(self, api_client, auth_headers):
        r = api_client.post("/api/drafts", headers=auth_headers, json={})
        assert r.status_code == 400, r.text
        r = api_client.post("/api/drafts", headers=auth_headers, json={
            "to": [], "subject": "  ", "body": "  "})
        assert r.status_code == 400, r.text
        assert api_client.drafts.rows == {}

    def test_create_bad_address_400(self, api_client, auth_headers):
        # HOTFIX 2026-11-16: addresses are no longer validated at
        # autosave time — a draft with a half-typed address must save
        # (it failed live with "Couldn't save draft — retry").
        # Validation moved to send time instead.
        r = api_client.post("/api/drafts", headers=auth_headers, json={
            "to": ["not-an-email"], "body": "x"})
        assert r.status_code == 200, r.text
        r = api_client.post("/api/drafts/1/send", headers=auth_headers)
        assert r.status_code == 400, r.text
        # The draft survives the failed send, so it can be fixed.
        assert api_client.get("/api/drafts/1", headers=auth_headers).status_code == 200

    def test_create_reply_draft_keeps_thread_refs(self, api_client, auth_headers):
        r = api_client.post("/api/drafts", headers=auth_headers, json={
            "subject": "Re: Hello", "body": "reply",
            "thread_id": "t201", "message_id": "m201"})
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["thread_id"] == "t201"
        assert r.json()["draft"]["message_id"] == "m201"

    def test_update_partial(self, api_client, auth_headers):
        api_client.drafts.create(["a@b.com"], [], [], "Old", "body")
        r = api_client.put("/api/drafts/1", headers=auth_headers,
                           json={"subject": "New"})
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["subject"] == "New"
        assert r.json()["draft"]["to"] == ["a@b.com"]  # untouched
        r = api_client.put("/api/drafts/999", headers=auth_headers,
                           json={"subject": "New"})
        assert r.status_code == 404

    def test_delete(self, api_client, auth_headers):
        api_client.drafts.create(["a@b.com"], [], [], "Old", "body")
        r = api_client.delete("/api/drafts/1", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert api_client.get("/api/drafts/1", headers=auth_headers).status_code == 404
        r = api_client.delete("/api/drafts/1", headers=auth_headers)
        assert r.status_code == 404

    def test_list_pagination(self, api_client, auth_headers):
        for i in range(3):
            api_client.drafts.create([f"u{i}@x.com"], [], [], f"D{i}", "b")
        d1 = api_client.get("/api/drafts", headers=auth_headers,
                            params={"limit": 2}).json()
        assert [d["id"] for d in d1["drafts"]] == [3, 2]
        assert d1["has_more"] is True and d1["next_cursor"]
        d2 = api_client.get("/api/drafts", headers=auth_headers,
                            params={"limit": 2, "cursor": d1["next_cursor"]}).json()
        assert [d["id"] for d in d2["drafts"]] == [1]
        assert d2["has_more"] is False
        # A drafts-folder cursor is the wrong namespace here.
        folder_cursor = db._encode_cursor(2_000_000_001, "drafts", None, None)
        r = api_client.get("/api/drafts", headers=auth_headers,
                           params={"cursor": folder_cursor})
        assert r.status_code == 400, r.text

    def test_send_fresh_draft_deletes_only_after_confirm(self, api_client, auth_headers):
        api_client.drafts.create(["a@b.com"], ["c@d.com"], ["h@i.com"],
                                 "Hello", "World")
        r = api_client.post("/api/drafts/1/send", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["sent_message_id"] == "sent888"
        assert api_client.get("/api/drafts/1", headers=auth_headers).status_code == 404
        sent = api_client.calls["send"][0]
        assert sent["to"] == ["a@b.com"] and sent["subject"] == "Hello"
        assert sent["cc_addrs"] == ["c@d.com"] and sent["bcc_addrs"] == ["h@i.com"]

    def test_send_failed_keeps_draft_502(self, api_client, auth_headers, monkeypatch):
        api_client.drafts.create(["a@b.com"], [], [], "Hello", "World")
        monkeypatch.setattr(api, "send_email",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gmail down")))
        r = api_client.post("/api/drafts/1/send", headers=auth_headers)
        assert r.status_code == 502, r.text
        assert api_client.get("/api/drafts/1", headers=auth_headers).status_code == 200

    def test_send_reply_draft_goes_in_thread(self, api_client, auth_headers):
        api_client.drafts.create([], ["c@d.com"], [], "Re: Hello", "reply body",
                                 thread_id="t201", message_id="m201")
        r = api_client.post("/api/drafts/1/send", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["thread_id"] == "t201"
        assert r.json()["sent_message_id"] == "sent999"
        assert api_client.calls["send"] == []  # reply path, not fresh send
        reply = api_client.calls["reply"][0]
        assert reply["message_id"] == "m201" and reply["body"] == "reply body"
        assert reply["cc_addrs"] == ["c@d.com"]
        assert api_client.get("/api/drafts/1", headers=auth_headers).status_code == 404

    def test_send_draft_validation(self, api_client, auth_headers):
        # Body-only draft (no To/subject) is creatable but not sendable.
        api_client.drafts.create([], [], [], "", "body only")
        r = api_client.post("/api/drafts/1/send", headers=auth_headers)
        assert r.status_code == 400, r.text
        # Empty-body draft: creatable with a subject, not sendable.
        api_client.drafts.create(["a@b.com"], [], [], "Subject", "")
        r = api_client.post("/api/drafts/2/send", headers=auth_headers)
        assert r.status_code == 400, r.text
        r = api_client.post("/api/drafts/999/send", headers=auth_headers)
        assert r.status_code == 404

# ------------------------------------------------------------------
# 7. Attachments
# ------------------------------------------------------------------

class TestAttachments:
    def test_mail_detail_includes_attachment_metadata(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1201", headers=auth_headers)
        assert r.status_code == 200, r.text
        atts = r.json()["mail"]["attachments"]
        assert len(atts) == 2
        assert atts[0]["filename"] == "report.pdf"
        assert atts[0]["attachmentId"] == "att1"
        assert atts[0]["size"] == 1234
        assert atts[1]["filename"] == "huge.zip"
        # Metadata only — no bodies/bytes ride along.
        assert all("data" not in a and "bytes" not in a for a in atts)

    def test_extract_attachments_returns_metadata_only(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "partId": "0",
             "body": {"data": "aGVsbG8=", "size": 5}},
            {"mimeType": "application/pdf", "filename": "report.pdf",
             "partId": "1.2", "body": {"attachmentId": "att1", "size": 1234}},
            {"mimeType": "image/png", "filename": "inline.png",
             "partId": "1.3", "body": {"attachmentId": "att9", "size": 99}},
        ]}
        atts = gs.extract_attachments(payload)
        assert atts == [
            {"filename": "report.pdf", "mimeType": "application/pdf",
             "size": 1234, "attachmentId": "att1", "partId": "1.2"},
            {"filename": "inline.png", "mimeType": "image/png",
             "size": 99, "attachmentId": "att9", "partId": "1.3"},
        ]
        assert all("data" not in a for a in atts)

    def test_no_attachment_bytes_stored_in_db(self, monkeypatch):
        fake = FakeCursor()
        monkeypatch.setattr(db, "get_db_cursor", fake_cursor_ctx(fake))
        db.save_processed_emails([{"id": "m1", "thread_id": "t1",
                                   "sender": "a@b.c", "subject": "s",
                                   "snippet": "", "is_unread": True,
                                   "is_starred": False,
                                   "label_ids": ["INBOX"], "received_at": None}])
        assert "attachment" not in fake.sql.lower()

    def test_download_200(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1201/attachments/att1", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.content == b"PDFBYTES"
        assert r.headers["content-type"] == "application/pdf"
        assert 'filename="report.pdf"' in r.headers["content-disposition"]

    def test_download_unknown_mail_404(self, api_client, auth_headers):
        r = api_client.get("/api/mails/9999/attachments/att1", headers=auth_headers)
        assert r.status_code == 404

    def test_download_unknown_attachment_404(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1201/attachments/att-missing",
                           headers=auth_headers)
        assert r.status_code == 404, r.text

    def test_download_over_cap_413(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1201/attachments/att-big",
                           headers=auth_headers)
        assert r.status_code == 413, r.text
        assert "10 MB" in r.text

    def test_download_gmail_failure_502(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1201/attachments/att-boom",
                           headers=auth_headers)
        assert r.status_code == 502, r.text

# ------------------------------------------------------------------
# 8. CC/BCC + multipart FormData send/reply
# ------------------------------------------------------------------

class TestCCBCCAndFormData:
    def test_send_json_with_cc_bcc(self, api_client, auth_headers):
        r = api_client.post("/api/mails/send", headers=auth_headers, json={
            "to": "a@b.com", "subject": "s", "body": "b",
            "cc": ["c@d.com"], "bcc": ["h@i.com"]})
        assert r.status_code == 200, r.text
        sent = api_client.calls["send"][0]
        assert sent["to"] == "a@b.com"
        assert sent["cc_addrs"] == ["c@d.com"]
        assert sent["bcc_addrs"] == ["h@i.com"]
        assert sent["attachments"] == []

    def test_send_multipart_with_attachment(self, api_client, auth_headers):
        r = api_client.post("/api/mails/send", headers=auth_headers,
                            data={"to": "a@b.com", "subject": "s", "body": "b",
                                  "cc": ["c@d.com"]},
                            files={"attachments": ("note.txt", b"hello file",
                                                  "text/plain")})
        assert r.status_code == 200, r.text
        sent = api_client.calls["send"][0]
        assert sent["cc_addrs"] == ["c@d.com"]
        assert sent["attachments"] == [("note.txt", "text/plain", b"hello file")]

    def test_send_multipart_multiple_attachments(self, api_client, auth_headers):
        r = api_client.post("/api/mails/send", headers=auth_headers,
                            data={"to": "a@b.com", "subject": "s", "body": "b"},
                            files=[("attachments", ("a.txt", b"AAA", "text/plain")),
                                   ("attachments", ("b.txt", b"BBB", "text/plain"))])
        assert r.status_code == 200, r.text
        assert api_client.calls["send"][0]["attachments"] == [
            ("a.txt", "text/plain", b"AAA"), ("b.txt", "text/plain", b"BBB")]

    def test_send_multipart_over_10mb_file_413(self, api_client, auth_headers):
        big = b"x" * (MAX_ATTACHMENT_BYTES + 1)
        r = api_client.post("/api/mails/send", headers=auth_headers,
                            data={"to": "a@b.com", "subject": "s", "body": "b"},
                            files={"attachments": ("big.bin", big,
                                                  "application/octet-stream")})
        assert r.status_code == 413, r.text
        assert "10 MB" in r.text
        assert api_client.calls["send"] == []

    def test_send_multipart_total_over_10mb_413(self, api_client, auth_headers):
        chunk = b"x" * (6 * 1024 * 1024)
        r = api_client.post("/api/mails/send", headers=auth_headers,
                            data={"to": "a@b.com", "subject": "s", "body": "b"},
                            files=[("attachments", ("a.bin", chunk,
                                                  "application/octet-stream")),
                                   ("attachments", ("b.bin", chunk,
                                                  "application/octet-stream"))])
        assert r.status_code == 413, r.text
        assert "total" in r.text.lower()
        assert api_client.calls["send"] == []

    def test_reply_thread_multipart_with_attachment(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t201/reply", headers=auth_headers,
                            data={"body": "thanks", "reply_all": "true",
                                  "cc": ["c@d.com"]},
                            files={"attachments": ("note.txt", b"hi",
                                                  "text/plain")})
        assert r.status_code == 200, r.text
        reply = api_client.calls["reply"][0]
        assert reply["message_id"] == "m201"  # latest in thread
        assert reply["reply_all"] is True
        assert reply["cc_addrs"] == ["c@d.com"]
        assert reply["attachments"] == [("note.txt", "text/plain", b"hi")]

    def test_reply_mail_multipart_with_attachment(self, api_client, auth_headers):
        r = api_client.post("/api/mails/1201/reply", headers=auth_headers,
                            data={"body": "thanks"},
                            files={"attachments": ("note.txt", b"hi",
                                                  "text/plain")})
        assert r.status_code == 200, r.text
        reply = api_client.calls["reply"][0]
        assert reply["message_id"] == "m201"
        assert reply["reply_all"] is False  # mail reply has no reply_all
        assert reply["attachments"] == [("note.txt", "text/plain", b"hi")]

    def test_reply_multipart_over_cap_413(self, api_client, auth_headers):
        big = b"x" * (MAX_ATTACHMENT_BYTES + 1)
        r = api_client.post("/api/mails/1201/reply", headers=auth_headers,
                            data={"body": "thanks"},
                            files={"attachments": ("big.bin", big,
                                                  "application/octet-stream")})
        assert r.status_code == 413, r.text
        assert api_client.calls["reply"] == []

    def test_bad_address_400(self, api_client, auth_headers):
        for payload in (
                {"to": "a@b.com", "subject": "s", "body": "b",
                 "cc": ["not-an-email"]},
                {"to": "a@b.com", "subject": "s", "body": "b",
                 "bcc": ["@nodomain"]},
                {"to": "a@b.com", "subject": "s", "body": "b",
                 "cc": ["plainstring"]}):
            r = api_client.post("/api/mails/send", headers=auth_headers, json=payload)
            assert r.status_code == 400, (payload, r.text)
        r = api_client.post("/api/threads/t201/reply", headers=auth_headers,
                            json={"body": "hi", "cc": ["bad"]})
        assert r.status_code == 400, r.text
        assert api_client.calls["send"] == [] and api_client.calls["reply"] == []

    def test_more_than_20_recipients_400(self, api_client, auth_headers):
        cc = [f"u{i}@x.com" for i in range(20)]
        r = api_client.post("/api/mails/send", headers=auth_headers, json={
            "to": "a@b.com", "subject": "s", "body": "b", "cc": cc})
        assert r.status_code == 400, r.text  # 1 + 20 = 21
        r = api_client.post("/api/mails/send", headers=auth_headers, json={
            "to": "a@b.com", "subject": "s", "body": "b",
            "cc": cc[:19]})
        assert r.status_code == 200, r.text  # 1 + 19 = 20, at the cap
        # HOTFIX 2026-11-16: drafts do NOT enforce the cap at save time
        # — autosave must never fail while the user is still typing
        # recipients. The cap is enforced at send time instead.
        r = api_client.post("/api/drafts", headers=auth_headers, json={
            "to": cc, "cc": ["extra@x.com"], "subject": "s", "body": "b"})
        assert r.status_code == 200, r.text  # 20 + 1 = 21: savable, not sendable
        r = api_client.post("/api/drafts/1/send", headers=auth_headers)
        assert r.status_code == 400, r.text  # 20 + 1 = 21 over the cap
        r = api_client.post("/api/drafts", headers=auth_headers, json={
            "to": cc, "subject": "s", "body": "b"})
        assert r.status_code == 200, r.text  # exactly 20 is allowed
        r = api_client.post("/api/drafts/2/send", headers=auth_headers)
        assert r.status_code == 200, r.text  # exactly 20 sends fine
        assert len(api_client.drafts.rows) == 1

# ------------------------------------------------------------------
# 9. Forward
# ------------------------------------------------------------------

class TestForward:
    def test_get_forward_draft_shape(self, api_client, auth_headers):
        r = api_client.get("/api/mails/1201/forward", headers=auth_headers)
        assert r.status_code == 200, r.text
        d = r.json()["draft"]
        assert d["message_id"] == "m201"
        assert d["subject"] == "Fwd: Hello inbox"
        assert "Forwarded message" in d["body"]
        assert [a["attachment_id"] for a in d["attachments"]] == ["att1", "att2"]
        assert d["attached_ids"] == ["att1"]
        assert d["skipped"] == [{"attachment_id": "att2", "filename": "huge.zip",
                                 "reason": "Would exceed the 10 MB total forward cap."}]
        assert d["total_attachment_bytes"] == 1234
        r = api_client.get("/api/mails/9999/forward", headers=auth_headers)
        assert r.status_code == 404

    def test_send_with_forward_reports_attached_and_skipped(self, api_client, auth_headers):
        r = api_client.post("/api/mails/send", headers=auth_headers, json={
            "to": "a@b.com", "subject": "Fwd: Hello inbox", "body": "see below",
            "forward_message_id": "m201",
            "forward_attachment_ids": ["att1", "att2"]})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["forwarded_attachments"] == ["report.pdf"]
        assert d["skipped_forward_attachments"] == [
            {"attachment_id": "att2", "filename": "huge.zip",
             "reason": "Would exceed the 10 MB total forward cap."}]
        # Alias the UI reads must carry the same list.
        assert d["skipped_attachments"] == d["skipped_forward_attachments"]
        sent = api_client.calls["send"][0]
        assert sent["attachments"] == [("report.pdf", "application/pdf", b"PDFBYTES")]

    def test_send_forward_all_attachments_when_ids_empty(self, api_client, auth_headers):
        r = api_client.post("/api/mails/send", headers=auth_headers, json={
            "to": "a@b.com", "subject": "Fwd: Hello inbox", "body": "see below",
            "forward_message_id": "m201"})
        assert r.status_code == 200, r.text
        # Empty forward_attachment_ids means "all of the original's".
        assert r.json()["forwarded_attachments"] == ["report.pdf"]

    def test_send_forward_ids_without_message_400(self, api_client, auth_headers):
        r = api_client.post("/api/mails/send", headers=auth_headers, json={
            "to": "a@b.com", "subject": "s", "body": "b",
            "forward_attachment_ids": ["att1"]})
        assert r.status_code == 400, r.text

    def test_send_forward_with_own_upload_reports_only_forwarded(self, api_client, auth_headers):
        """A file the user uploads with a forward is NOT a forwarded
        attachment: forwarded_attachments must list only the original's
        re-attached files. (Fails today for the same UploadFile reason
        as the multipart tests — the upload never reaches send_email —
        and stays failing after that is fixed until the reporting list
        is built from the forwarded files only.)"""
        r = api_client.post("/api/mails/send", headers=auth_headers,
                            data={"to": "a@b.com", "subject": "Fwd: Hello inbox",
                                  "body": "see below",
                                  "forward_message_id": "m201",
                                  "forward_attachment_ids": ["att1"]},
                            files={"attachments": ("mine.txt", b"mine",
                                                  "text/plain")})
        assert r.status_code == 200, r.text
        sent = api_client.calls["send"][0]
        assert ("mine.txt", "text/plain", b"mine") in sent["attachments"]
        assert ("report.pdf", "application/pdf", b"PDFBYTES") in sent["attachments"]
        assert r.json()["forwarded_attachments"] == ["report.pdf"]

    def test_fetch_forward_attachments_gs_level(self, monkeypatch):
        metas = [
            {"filename": "a.pdf", "mimeType": "application/pdf",
             "size": 100, "attachmentId": "a1", "partId": "1"},
            {"filename": "b.zip", "mimeType": "application/zip",
             "size": 9 * 1024 * 1024, "attachmentId": "b2", "partId": "2"},
            {"filename": "c.zip", "mimeType": "application/zip",
             "size": 5 * 1024 * 1024, "attachmentId": "c3", "partId": "3"},
        ]
        monkeypatch.setattr(gs, "get_message_attachments", lambda mid: metas)
        monkeypatch.setattr(gs, "get_attachment_bytes",
                            lambda mid, aid: (b"x" * 100, "a.pdf", "application/pdf")
                            if aid == "a1" else
                            (b"y" * (9 * 1024 * 1024), "b.zip", "application/zip"))
        attached, skipped = gs.fetch_forward_attachments(
            "m1", ["a1", "b2", "c3", "ghost"])
        assert [f for f, _, _ in attached] == ["a.pdf", "b.zip"]
        assert [s["attachment_id"] for s in skipped] == ["c3", "ghost"]
        assert "10 MB" in skipped[0]["reason"]
        assert "Not found" in skipped[1]["reason"]

    def test_build_forward_draft_gs_level(self, monkeypatch):
        monkeypatch.setattr(gs, "get_email_full", lambda mid: {
            "thread_id": "t1", "sender": "Amy <amy@example.com>",
            "subject": "Hello", "date": "Mon", "body_text": "ORIG",
            "attachments": [
                {"filename": "a.pdf", "mimeType": "application/pdf",
                 "size": 100, "attachmentId": "a1", "partId": "1"}]})
        monkeypatch.setattr(gs, "fetch_forward_attachments",
                            lambda mid, aids: ([("a.pdf", "application/pdf", b"X")],
                                               [{"attachment_id": "z9", "filename": "",
                                                 "reason": "Not found on the original message."}]))
        d = gs.build_forward_draft("m1")
        assert d["subject"] == "Fwd: Hello"
        assert "ORIG" in d["body"] and "Forwarded message" in d["body"]
        assert d["attached_ids"] == ["a1"]
        assert d["skipped"][0]["attachment_id"] == "z9"
        assert d["total_attachment_bytes"] == 1

# ------------------------------------------------------------------
# 10. Restore via thread state
# ------------------------------------------------------------------

class TestRestore:
    def test_restore_from_trash(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t201/state", headers=auth_headers,
                            json={"is_trash": False})
        assert r.status_code == 200, r.text
        ids, kw = api_client.calls["batch"][0]
        assert ids == ("m201a", "m201") and kw["is_trash"] is False

    def test_restore_from_spam(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t201/state", headers=auth_headers,
                            json={"is_spam": False})
        assert r.status_code == 200, r.text
        ids, kw = api_client.calls["batch"][0]
        assert kw["is_spam"] is False

    def test_restore_from_archive(self, api_client, auth_headers):
        r = api_client.post("/api/threads/t201/state", headers=auth_headers,
                            json={"is_archived": False})
        assert r.status_code == 200, r.text
        ids, kw = api_client.calls["batch"][0]
        assert kw["is_archived"] is False
        # Local mirror must also clear the archived flag.
        assert any(c.get("is_archived") is False
                   for c in api_client.calls["local_state"])

# ------------------------------------------------------------------
# 11. Auth on the new endpoints
# ------------------------------------------------------------------

class TestAuthPhase2:
    @pytest.mark.parametrize("method,path,kwargs", [
        ("GET", "/api/folders", {}),
        ("GET", "/api/drafts", {}),
        ("POST", "/api/drafts", {"json": {"subject": "x", "body": "y"}}),
        ("GET", "/api/drafts/1", {}),
        ("PUT", "/api/drafts/1", {"json": {"subject": "x"}}),
        ("DELETE", "/api/drafts/1", {}),
        ("POST", "/api/drafts/1/send", {}),
        ("GET", "/api/mails/1201/attachments/att1", {}),
        ("GET", "/api/mails/1201/forward", {}),
        ("POST", "/api/threads/t201/state", {"json": {"is_trash": False}}),
    ])
    def test_no_token_401(self, api_client, method, path, kwargs):
        r = api_client.request(method, path, **kwargs)
        assert r.status_code == 401, (method, path, r.text)

    def test_wrong_token_401_on_drafts(self, api_client):
        r = api_client.get("/api/drafts",
                           headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

# ------------------------------------------------------------------
# 12. Route table (Phase 2)
# ------------------------------------------------------------------

class TestRouteTablePhase2:
    def test_new_phase2_routes_present(self):
        routes = {(tuple(sorted(getattr(r, "methods", None) or ())), r.path)
                  for r in app.routes if hasattr(r, "methods")}
        new_routes = [
            (("GET",), "/api/folders"),
            (("GET",), "/api/mails/{mail_id}/attachments/{attachment_id}"),
            (("GET",), "/api/mails/{mail_id}/forward"),
            (("POST",), "/api/drafts"),
            (("GET",), "/api/drafts"),
            (("GET",), "/api/drafts/{draft_id}"),
            (("PUT",), "/api/drafts/{draft_id}"),
            (("DELETE",), "/api/drafts/{draft_id}"),
            (("POST",), "/api/drafts/{draft_id}/send"),
        ]
        for methods, path in new_routes:
            assert (methods, path) in routes, f"missing new route {methods} {path}"

    def test_send_and_reply_still_registered(self):
        routes = {(tuple(sorted(getattr(r, "methods", None) or ())), r.path)
                  for r in app.routes if hasattr(r, "methods")}
        for methods, path in [
                (("POST",), "/api/mails/send"),
                (("POST",), "/api/mails/{mail_id}/reply"),
                (("POST",), "/api/threads/{thread_id}/reply"),
                (("POST",), "/api/threads/{thread_id}/state")]:
            assert (methods, path) in routes, f"missing route {methods} {path}"
