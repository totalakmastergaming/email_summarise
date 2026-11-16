import base64 as _base64
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from queue import Queue, Empty
from threading import Lock
from typing import Any, Optional
import pymysql
import pymysql.cursors
from app.config import settings
from app.crypto import decrypt_api_key, encrypt_api_key

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Timezone-naive UTC now (TiDB DATETIME columns are naive).

    ``datetime.utcnow()`` is deprecated; this is its exact equivalent.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

class TiDBConnectionPool:
    """
    Lightweight, thread-safe connection pool for TiDB Cloud Serverless.
    Reuses open TLS/SSL connections to eliminate ~200-300ms handshake overhead per query.

    The pool is BOUNDED: a semaphore caps live connections at
    ``max_connections``. Previously ``get_connection()`` created a brand-new
    connection whenever the pool queue was empty, so a burst of concurrent
    FastAPI threadpool requests could open unlimited connections and blow
    past TiDB Serverless limits. Now a caller waits up to 10s for a slot
    and then fails loudly instead of opening connection #6, #7, ...
    """
    def __init__(self, max_connections: int = 5):
        self.max_connections = max_connections
        self._pool: Queue = Queue(maxsize=max_connections)
        self._lock = Lock()
        # One slot per live connection (pooled + checked out).
        self._slots = threading.Semaphore(max_connections)

    def _create_connection(self):
        return pymysql.connect(
            host=settings.TIDB_HOST,
            port=settings.TIDB_PORT,
            user=settings.TIDB_USER,
            password=settings.TIDB_PASSWORD,
            database=settings.TIDB_DATABASE,
            ssl={"ssl_mode": "VERIFY_IDENTITY"},
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            # A cold TiDB Serverless cluster can accept the TCP connection
            # and then stall before the handshake / first query reply. Without
            # these, that stall blocks forever (no exception, no log) and the
            # app never finishes starting — Render then loops on "No open
            # ports detected". Bound every phase to 10s so a stall fails fast.
            read_timeout=10,
            write_timeout=10,
            autocommit=False
        )

    def get_connection(self):
        try:
            conn = self._pool.get_nowait()
            try:
                # Fast ping to ensure connection is healthy; auto-reconnects if stale
                conn.ping(reconnect=True)
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                # Slot stays held: this replacement reuses the dead conn's slot.
                return self._create_connection()
        except Empty:
            if not self._slots.acquire(timeout=10):
                raise TimeoutError(
                    f"TiDB connection pool exhausted: {self.max_connections} live "
                    "connections in use after 10s. Failing instead of opening "
                    "an unbounded extra connection."
                )
            try:
                return self._create_connection()
            except Exception:
                self._slots.release()
                raise

    def release_connection(self, conn):
        if not conn:
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            # Pool full, close socket safely and free its slot.
            try:
                conn.close()
            except Exception:
                pass
            self._slots.release()

# Reusable connection pool singleton
db_pool = TiDBConnectionPool(max_connections=5)

@contextmanager
def get_db_cursor(commit: bool = False):
    """
    Reusable database cursor context manager backed by connection pooling.
    Eliminates repetitive connection/cursor creation, commit, and cleanup.
    """
    conn = db_pool.get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        db_pool.release_connection(conn)

def mask_api_key(key: str) -> str:
    """Mask API key for safe frontend display and logging (e.g. AIza...4xQ9)."""
    if not key or len(key) < 10:
        return "****"
    return f"{key[:6]}...{key[-4:]}"

def init_db() -> bool:
    """Initialize and migrate required tables in TiDB.

    Returns True when the schema is verified, False when the database could
    not be reached (caller may retry). Never raises: startup must not die
    on a cold/unreachable database — see main.py lifespan.
    """
    try:
        with get_db_cursor(commit=True) as cur:
            # 1. Processed emails deduplication & inbox state table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message_id VARCHAR(255) UNIQUE NOT NULL,
                    thread_id VARCHAR(255) NULL,
                    sender VARCHAR(255),
                    subject VARCHAR(500),
                    snippet TEXT,
                    is_unread BOOLEAN DEFAULT TRUE,
                    is_starred BOOLEAN DEFAULT FALSE,
                    labels VARCHAR(500) NULL,
                    is_archived BOOLEAN DEFAULT FALSE,
                    is_trashed BOOLEAN DEFAULT FALSE,
                    received_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_msg_id (message_id),
                    INDEX idx_thread_id (thread_id)
                );
            """)

            # Safe column additions if table already existed from prior runs
            cur.execute("SHOW COLUMNS FROM processed_emails")
            existing_cols = {row['Field'] for row in cur.fetchall()}
            if "thread_id" not in existing_cols:
                # Existing rows keep NULL thread_id; everywhere else NULL is
                # treated as "own thread" via COALESCE(thread_id, message_id).
                cur.execute("ALTER TABLE processed_emails ADD COLUMN thread_id VARCHAR(255) NULL")
            cur.execute("SHOW INDEX FROM processed_emails WHERE Key_name = 'idx_thread_id'")
            if not cur.fetchall():
                cur.execute("ALTER TABLE processed_emails ADD INDEX idx_thread_id (thread_id)")
            if "is_unread" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN is_unread BOOLEAN DEFAULT TRUE")
            if "is_starred" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN is_starred BOOLEAN DEFAULT FALSE")
            if "labels" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN labels VARCHAR(500) NULL")
            if "is_archived" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN is_archived BOOLEAN DEFAULT FALSE")
            if "is_trashed" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN is_trashed BOOLEAN DEFAULT FALSE")

            # 1b. Gmail History API sync checkpoint (Phase 1 incremental sync).
            # One row per Gmail account ("me" — single-user app). history_id
            # is the last History record id the mirror has applied; the
            # checkpoint only ever advances AFTER a full history page run
            # succeeds, so a failed run reprocesses the same records safely
            # (upserts + label deltas are idempotent).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gmail_sync_state (
                    account_id VARCHAR(100) PRIMARY KEY,
                    history_id BIGINT NULL,
                    last_sync_at DATETIME NULL,
                    last_mode VARCHAR(20) NULL
                );
            """)

            # 1c. Server-side compose/reply drafts (Phase 2). Drafts live
            # in TiDB — not localStorage — so they survive refresh, work
            # across devices, and can be listed in the Drafts folder and
            # sent from the server. to/cc/bcc are comma-separated address
            # lists (TEXT: a draft may hold up to 20 recipients).
            # thread_id/message_id are set for reply drafts (the draft
            # replies in-thread when sent); NULL for fresh composes.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS drafts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    to_addrs TEXT NULL,
                    cc_addrs TEXT NULL,
                    bcc_addrs TEXT NULL,
                    subject VARCHAR(500) NULL,
                    body LONGTEXT NULL,
                    thread_id VARCHAR(255) NULL,
                    message_id VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_draft_thread_id (thread_id)
                );
            """)

            # 2. Daily executive digest reports archive
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_reports (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    report_date DATE UNIQUE NOT NULL,
                    total_emails INT DEFAULT 0,
                    urgent_count INT DEFAULT 0,
                    summary_markdown LONGTEXT,
                    action_items JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_report_date (report_date)
                );
            """)

            # 3. Dynamic Multi-Provider API Keys table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    provider VARCHAR(50) NOT NULL,
                    label VARCHAR(100) NOT NULL,
                    api_key VARCHAR(500) NOT NULL,
                    model_name VARCHAR(100) NOT NULL,
                    priority INT DEFAULT 1,
                    is_active BOOLEAN DEFAULT TRUE,
                    rate_limit_rpm INT DEFAULT 15,
                    cooldown_until DATETIME NULL,
                    total_requests INT DEFAULT 0,
                    success_requests INT DEFAULT 0,
                    failed_requests INT DEFAULT 0,
                    last_error TEXT NULL,
                    last_used_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_provider (provider),
                    INDEX idx_priority (priority)
                );
            """)

            # 4. System Configuration table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key_name VARCHAR(100) PRIMARY KEY,
                    value_text LONGTEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                );
            """)

            # 5. Persistent Summary Jobs (All-or-nothing pipeline orchestration)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS summary_jobs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    job_uuid VARCHAR(64) UNIQUE NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    date_range_info VARCHAR(255) NULL,
                    total_emails INT DEFAULT 0,
                    total_batches INT DEFAULT 0,
                    completed_batches INT DEFAULT 0,
                    failed_batches INT DEFAULT 0,
                    executive_summary LONGTEXT NULL,
                    email_markdown_report LONGTEXT NULL,
                    action_items JSON NULL,
                    urgent_count INT DEFAULT 0,
                    error_message TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_job_status (status)
                );
            """)

            # 6. Persistent Summary Batches (Intelligent dynamic batch tracking)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS summary_batches (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    job_id INT NOT NULL,
                    batch_index INT NOT NULL,
                    email_count INT NOT NULL,
                    email_ids JSON NOT NULL,
                    api_key_id INT NULL,
                    status VARCHAR(50) NOT NULL,
                    retry_count INT DEFAULT 0,
                    batch_summary LONGTEXT NULL,
                    action_items JSON NULL,
                    error_message TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_job_batch (job_id, batch_index)
                );
            """)

            # Seed default system configs if missing
            default_schedule = json.dumps({"enabled": False, "time": "18:00", "timezone": "Asia/Kolkata"})
            default_range = json.dumps({"preset": "last_1_day", "custom_start": "", "custom_end": ""})
            cur.execute("""
                INSERT IGNORE INTO system_config (key_name, value_text) VALUES 
                ('summary_schedule', %s),
                ('summary_range', %s)
            """, (default_schedule, default_range))

            # Seed primary Gemini key from .env if api_keys is empty
            # (encrypted at rest when API_KEY_ENCRYPTION_KEY is configured).
            cur.execute("SELECT COUNT(*) AS count FROM api_keys")
            cnt_row = cur.fetchone()
            if cnt_row and cnt_row.get("count", 0) == 0 and settings.GEMINI_API_KEY:
                cur.execute("""
                    INSERT INTO api_keys (provider, label, api_key, model_name, priority, is_active, rate_limit_rpm)
                    VALUES (%s, %s, %s, %s, 1, TRUE, 15)
                """, ("gemini", "Primary Gemini Flash", encrypt_api_key(settings.GEMINI_API_KEY), settings.GEMINI_MODEL))
                logger.info("Seeded primary Gemini API key into api_keys table.")

        logger.info("TiDB schema verified and tables initialized successfully.")
        return True
    except Exception as e:
        logger.warning(f"Could not connect to TiDB during init (check credentials): {e}")
        return False

# ================= Inbox Synchronization & Emails =================

def get_unprocessed_message_ids(candidate_ids: list[str]) -> set[str]:
    """Return the set of message_ids that have NOT yet been processed."""
    if not candidate_ids:
        return set()
    
    with get_db_cursor() as cur:
        format_strings = ','.join(['%s'] * len(candidate_ids))
        cur.execute(
            f"SELECT message_id FROM processed_emails WHERE message_id IN ({format_strings})",
            tuple(candidate_ids)
        )
        rows = cur.fetchall()
        existing_ids = {row['message_id'] for row in rows}
        return set(candidate_ids) - existing_ids

def save_processed_emails(emails_data: list[dict]):
    """Insert or update processed emails into TiDB with live read/starred states.

    The stored ``labels`` CSV is the source of truth for folder state,
    so the ``is_archived`` / ``is_trashed`` mirror flags are derived
    from it here — on INSERT and on duplicate-key UPDATE — exactly as
    ``apply_label_delta`` / the label sync derive them (archived =
    INBOX absent, trashed = TRASH present). Previously the INSERT
    omitted both flags (defaulting FALSE) and the UPDATE never touched
    them, so any row that arrived without INBOX (e.g. Sent mail, which
    the date-range Gmail fetch pulls in) was stored as "in inbox" and
    counted in the dashboard while belonging to no inbox tab.
    """
    if not emails_data:
        return
    with get_db_cursor(commit=True) as cur:
        sql = """
            INSERT INTO processed_emails (message_id, thread_id, sender, subject, snippet, is_unread, is_starred, labels, is_archived, is_trashed, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                thread_id = VALUES(thread_id),
                sender = VALUES(sender),
                subject = VALUES(subject),
                snippet = VALUES(snippet),
                is_unread = VALUES(is_unread),
                is_starred = VALUES(is_starred),
                labels = VALUES(labels),
                is_archived = VALUES(is_archived),
                is_trashed = VALUES(is_trashed)
        """
        values = []
        for e in emails_data:
            rec_at = e.get("received_at")
            if isinstance(rec_at, str):
                try:
                    rec_at = datetime.fromisoformat(rec_at.replace("Z", "+00:00"))
                except Exception:
                    rec_at = None
            labels_str = ",".join(e.get("label_ids", [])) if isinstance(e.get("label_ids"), list) else e.get("labels", "")
            label_set = {p.strip() for p in str(labels_str or "").split(",") if p.strip()}
            values.append((
                e["id"],
                e.get("thread_id"),
                e.get("sender", "Unknown"),
                e.get("subject", "(No Subject)"),
                e.get("snippet", ""),
                bool(e.get("is_unread", True)),
                bool(e.get("is_starred", False)),
                labels_str,
                "INBOX" not in label_set,
                "TRASH" in label_set,
                rec_at
            ))
        cur.executemany(sql, values)

def update_processed_email_state(message_id_or_pk: str, is_unread: Optional[bool] = None, is_starred: Optional[bool] = None, is_archived: Optional[bool] = None, is_trashed: Optional[bool] = None) -> bool:
    """Update local email read/starred/archive/trash status in TiDB."""
    updates = []
    params = []
    if is_unread is not None:
        updates.append("is_unread = %s")
        params.append(bool(is_unread))
    if is_starred is not None:
        updates.append("is_starred = %s")
        params.append(bool(is_starred))
    if is_archived is not None:
        updates.append("is_archived = %s")
        params.append(bool(is_archived))
    if is_trashed is not None:
        updates.append("is_trashed = %s")
        params.append(bool(is_trashed))
    if not updates:
        return False

    with get_db_cursor(commit=True) as cur:
        # Check if argument is integer primary key or Gmail string message_id
        if str(message_id_or_pk).isdigit() and int(message_id_or_pk) > 1000:
            db_id = int(message_id_or_pk) - 1000
            params.append(db_id)
            cur.execute(f"UPDATE processed_emails SET {', '.join(updates)} WHERE id = %s", tuple(params))
        elif str(message_id_or_pk).isdigit():
            params.append(int(message_id_or_pk))
            cur.execute(f"UPDATE processed_emails SET {', '.join(updates)} WHERE id = %s", tuple(params))
        else:
            params.append(str(message_id_or_pk))
            cur.execute(f"UPDATE processed_emails SET {', '.join(updates)} WHERE message_id = %s", tuple(params))
        return cur.rowcount > 0

def get_email_by_id_or_msg_id(identifier: Any) -> Optional[dict]:
    """Retrieve an email record by its integer id or Gmail message_id."""
    with get_db_cursor() as cur:
        if str(identifier).isdigit():
            val = int(identifier)
            if val > 1000:
                val = val - 1000
            cur.execute("SELECT * FROM processed_emails WHERE id = %s", (val,))
        else:
            cur.execute("SELECT * FROM processed_emails WHERE message_id = %s", (str(identifier),))
        return cur.fetchone()


# ================= Threading, counts & keyset pagination (Phase 1) ====

# Urgent-subject keywords, kept identical to the ones used by the API
# layer (api.py) so dashboard counts and folder filtering agree.
_URGENT_SUBJECT_LIKES = ["%urgent%", "%action required%", "%deadline%", "%immediate%", "%alert%"]

# SQL fragment for the "needs a reply" heuristic, mirroring api.py's
# _needs_reply() in SQL. APPROXIMATION — it cannot know whether the user
# already replied in the thread (that needs thread history); it only
# checks unread + Primary category + a non-automated-looking sender and
# subject. The Python helper remains the source of truth for a single
# row; this fragment exists so folders/counts can be computed in SQL.
#
# HOTFIX 2026-11-16: the patterns MUST be bound params, never inlined
# into the SQL string. PyMySQL executes queries as ``query % params``,
# so a literal '%…%' in the SQL is read as a %-format specifier and
# raises ValueError ("unsupported format character 'C' (0x43) at index
# 855" — live, Primary tab + dashboard/folder counts). The old constant
# _NEEDS_REPLY_SQL inlined all 20 patterns and broke every query that
# used it (get_inbox_counts, get_folder_counts, folder=reply).
_NEEDS_REPLY_PATTERNS = [
    ("labels", "%CATEGORY_SOCIAL%"),
    ("labels", "%CATEGORY_PROMOTIONS%"),
    ("labels", "%CATEGORY_UPDATES%"),
    ("labels", "%CATEGORY_FORUMS%"),
    ("sender", "%noreply%"),
    ("sender", "%no-reply%"),
    ("sender", "%donotreply%"),
    ("sender", "%do-not-reply%"),
    ("sender", "%notification%"),
    ("sender", "%digest%"),
    ("sender", "%updates@%"),
    ("sender", "%newsletter%"),
    ("subject", "%newsletter%"),
    ("subject", "%digest%"),
    ("subject", "%notification only%"),
    ("subject", "%unsubscribe%"),
    ("subject", "%your receipt%"),
    ("subject", "%order confirmed%"),
    ("subject", "%password reset%"),
    ("subject", "%verify your%"),
]


def _needs_reply_sql(alias: str = "p") -> tuple[str, list[str]]:
    """(SQL, params) for the needs-reply heuristic on one row.

    Columns are qualified with ``alias`` (the latest-message alias in
    every caller); patterns are bound params, never inlined.
    """
    a = f"{alias}." if alias else ""
    parts = [f"{a}is_unread = TRUE"]
    parts.extend(
        f"COALESCE({a}{col}, '') NOT LIKE %s" for col, _ in _NEEDS_REPLY_PATTERNS
    )
    return "(" + " AND ".join(parts) + ")", [pat for _, pat in _NEEDS_REPLY_PATTERNS]


def _urgent_subject_sql(alias: str = "") -> str:
    """`subject LIKE ...` OR-chain for the urgent keywords (alias-aware)."""
    col = f"{alias}.subject" if alias else "subject"
    return "(" + " OR ".join(f"{col} LIKE %s" for _ in _URGENT_SUBJECT_LIKES) + ")"


def get_thread_messages(thread_id: str) -> list[dict]:
    """All messages of one thread, chronological.

    ``thread_id`` may be a Gmail threadId or a single message_id: rows
    stored before threading (thread_id NULL) are their own thread, so
    the lookup also matches message_id. NULL received_at sorts first
    in MySQL, so undated rows are pushed last.
    """
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT * FROM processed_emails "
            "WHERE thread_id = %s OR message_id = %s "
            "ORDER BY (received_at IS NULL), received_at ASC, id ASC",
            (thread_id, thread_id),
        )
        return cur.fetchall()


def mark_messages_trashed(message_ids: list[str]) -> int:
    """Soft-trash rows by Gmail message_id (rows are never deleted).

    Used by the History sync for messagesDeleted records. Returns the
    number of local rows actually marked.
    """
    if not message_ids:
        return 0
    with get_db_cursor(commit=True) as cur:
        fmt = ",".join(["%s"] * len(message_ids))
        cur.execute(
            f"UPDATE processed_emails SET is_trashed = TRUE WHERE message_id IN ({fmt})",
            tuple(message_ids),
        )
        return cur.rowcount


def apply_label_delta(message_id: str, add_labels: Optional[list[str]] = None,
                      remove_labels: Optional[list[str]] = None) -> bool:
    """Apply a Gmail label delta to one local row, no Gmail calls.

    Used by the History sync for labelsAdded/labelsRemoved records.
    The labels CSV is the source of truth; the derived flags are
    recomputed from it (same derivation as sync_inbox_labels_from_gmail:
    unread = UNREAD present, starred = STARRED present, archived = INBOX
    absent, trashed = TRASH present). Returns False when the message is
    not in TiDB (the record is skipped, not an error).
    """
    with get_db_cursor(commit=True) as cur:
        cur.execute("SELECT labels FROM processed_emails WHERE message_id = %s", (message_id,))
        row = cur.fetchone()
        if not row:
            return False
        labels = {p.strip() for p in str(row.get("labels") or "").split(",") if p.strip()}
        labels |= {str(x).strip() for x in (add_labels or []) if str(x).strip()}
        labels -= {str(x).strip() for x in (remove_labels or []) if str(x).strip()}
        cur.execute(
            "UPDATE processed_emails SET labels = %s, is_unread = %s, "
            "is_starred = %s, is_archived = %s, is_trashed = %s "
            "WHERE message_id = %s",
            (
                ",".join(sorted(labels)),
                "UNREAD" in labels,
                "STARRED" in labels,
                "INBOX" not in labels,
                "TRASH" in labels,
                message_id,
            ),
        )
        return True


# ---- Gmail History sync checkpoint ----

_SYNC_ACCOUNT_ID = "me"  # Single-user app: the Gmail userId is always "me".


def get_sync_checkpoint() -> Optional[dict]:
    """Current History sync checkpoint, or None if never synced."""
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM gmail_sync_state WHERE account_id = %s", (_SYNC_ACCOUNT_ID,))
        row = cur.fetchone()
    if not row:
        return None
    return {
        "account_id": row["account_id"],
        "history_id": int(row["history_id"]) if row.get("history_id") is not None else None,
        "last_sync_at": row["last_sync_at"].isoformat() if row.get("last_sync_at") else None,
        "last_mode": row.get("last_mode"),
    }


def set_sync_checkpoint(history_id: int, mode: str) -> None:
    """Advance the History checkpoint. Only call AFTER a sync run has
    fully succeeded — a failed run must leave the old checkpoint in
    place so its records are reprocessed (safe: upserts + label deltas
    are idempotent)."""
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO gmail_sync_state (account_id, history_id, last_sync_at, last_mode)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                history_id = VALUES(history_id),
                last_sync_at = VALUES(last_sync_at),
                last_mode = VALUES(last_mode)
            """,
            (_SYNC_ACCOUNT_ID, int(history_id), _utcnow_naive(), mode),
        )


# ---- Folder predicates (Phase 2) ----
# Single source of truth: the stored Gmail ``labels`` CSV on the
# thread's LATEST message — NOT the is_archived / is_trashed flags,
# which the pipeline insert path never set (see save_processed_emails
# fix above). FIND_IN_SET matches whole label tokens exactly
# (COALESCE guards NULL labels); LIKE '%INBOX%' would also match a
# hypothetical 'XINBOX' token and is not used for folder tests.
# Every predicate is written against alias ``p`` (the latest message
# of a thread); list_threads() and get_folder_counts() share this one
# helper so a folder list and its count can never disagree.

def _label_present(label: str, alias: str = "p") -> str:
    return f"FIND_IN_SET('{label}', COALESCE({alias}.labels, '')) > 0"


def _label_absent(label: str, alias: str = "p") -> str:
    return f"FIND_IN_SET('{label}', COALESCE({alias}.labels, '')) = 0"


def _folder_predicate_sql(folder: str, alias: str = "p") -> tuple[str, list[Any]]:
    """(SQL, params) for one folder, tested on the thread's latest message.

    - inbox: INBOX present, not TRASH/SPAM.
    - sent: SENT label. - drafts: DRAFT label (Gmail-side drafts; the
      server drafts table is unioned in list_threads / added in
      get_folder_counts — see there).
    - archive: no INBOX, no TRASH/SPAM, not a draft.
    - trash: TRASH. - spam: SPAM.
    - starred: STARRED label, not trashed (keeps archived visible, like Gmail).
    - unread: UNREAD label, in the inbox (INBOX present, not TRASH/SPAM).
    - urgent: urgent subject, in the inbox.
    - reply: needs-reply heuristic, in the inbox.
    - all: everything except trashed.
    """
    a = alias
    inbox_base = f"{_label_present('INBOX', a)} AND {_label_absent('TRASH', a)} AND {_label_absent('SPAM', a)}"
    if folder == "inbox":
        return inbox_base, []
    if folder == "sent":
        return _label_present("SENT", a), []
    if folder == "drafts":
        return _label_present("DRAFT", a), []
    if folder == "archive":
        return (
            f"{_label_absent('INBOX', a)} AND {_label_absent('TRASH', a)} "
            f"AND {_label_absent('SPAM', a)} AND {_label_absent('DRAFT', a)}",
            [],
        )
    if folder == "trash":
        return _label_present("TRASH", a), []
    if folder == "spam":
        return _label_present("SPAM", a), []
    if folder == "starred":
        return f"{_label_present('STARRED', a)} AND {_label_absent('TRASH', a)}", []
    if folder == "unread":
        return f"{_label_present('UNREAD', a)} AND {inbox_base}", []
    if folder == "urgent":
        return f"{_urgent_subject_sql(a)} AND {inbox_base}", list(_URGENT_SUBJECT_LIKES)
    if folder == "reply":
        needs_sql, needs_params = _needs_reply_sql(a)
        return f"{needs_sql} AND {inbox_base}", needs_params
    if folder == "all":
        return _label_absent("TRASH", a), []
    raise ValueError(f"Unknown folder: {folder}")


def get_inbox_counts() -> dict:
    """SQL counts for the dashboard, over INBOX threads only.

    Phase 2 re-scope (diagnostic 2026-11-16): the old base was
    ``is_archived = FALSE AND is_trashed = FALSE`` with no INBOX-label
    test and no category test, so Sent threads and CATEGORY_UPDATES /
    CATEGORY_FORUMS threads — invisible in every inbox tab — were
    counted, and total_threads (30) disagreed with the visible tabs
    (15). The base is now the INBOX label (present, not TRASH/SPAM),
    so total_threads equals the number of threads across the five
    inbox category tabs. Message-level sums (unread/today/urgent/
    needs_reply) use the same INBOX base; total_threads counts
    distinct threads.

    HOTFIX 2026-11-16: all five numbers are now computed over the
    thread's LATEST message only (same latest-message subquery as
    list_threads/get_folder_counts). Previously the four sums ran over
    every message row while total_threads counted threads, so e.g.
    unread could exceed total_threads and disagree with the bold rows
    the inbox actually shows (a thread's row carries its latest
    message's flags). Now unread = threads whose latest message is
    unread, today/urgent/needs_reply likewise — one ground truth.
    """
    from zoneinfo import ZoneInfo

    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    needs_sql, needs_params = _needs_reply_sql("p")
    with get_db_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN p.is_unread THEN 1 ELSE 0 END), 0) AS unread,
                COALESCE(SUM(CASE WHEN DATE(CONVERT_TZ(p.received_at, '+00:00', '+05:30')) = %s THEN 1 ELSE 0 END), 0) AS today,
                COALESCE(SUM(CASE WHEN {_urgent_subject_sql("p")} THEN 1 ELSE 0 END), 0) AS urgent,
                COALESCE(SUM(CASE WHEN {needs_sql} THEN 1 ELSE 0 END), 0) AS needs_reply,
                COUNT(*) AS total_threads
            FROM processed_emails p
            JOIN (SELECT COALESCE(thread_id, message_id) AS tkey, MAX(id) AS max_id
                  FROM processed_emails GROUP BY tkey) latest ON p.id = latest.max_id
            WHERE {_label_present('INBOX')} AND {_label_absent('TRASH')} AND {_label_absent('SPAM')}
            """,
            (today_ist, *_URGENT_SUBJECT_LIKES, *needs_params),
        )
        row = cur.fetchone() or {}
    return {
        "unread": int(row.get("unread") or 0),
        "today": int(row.get("today") or 0),
        "urgent": int(row.get("urgent") or 0),
        "needs_reply": int(row.get("needs_reply") or 0),
        "total_threads": int(row.get("total_threads") or 0),
    }


def get_folder_counts() -> dict:
    """Per-folder THREAD counts, over each thread's latest message.

    Uses the exact same _folder_predicate_sql() as list_threads(), so
    a folder's count always equals the number of rows its list can
    return (same base, same predicates, same latest-message rule).
    The drafts count adds the server drafts table rows to the
    Gmail-DRAFT threads, matching the drafts-folder union in
    list_threads().
    """
    folders = ("inbox", "sent", "drafts", "archive", "trash", "spam",
               "starred", "unread", "urgent", "reply", "all")
    select_parts: list[str] = []
    params: list[Any] = []
    for folder in folders:
        pred_sql, pred_params = _folder_predicate_sql(folder, "p")
        select_parts.append(
            "COUNT(DISTINCT CASE WHEN "
            f"{pred_sql} THEN COALESCE(p.thread_id, p.message_id) END) AS `{folder}`"
        )
        params.extend(pred_params)
    with get_db_cursor() as cur:
        cur.execute(
            "SELECT " + ", ".join(select_parts) + " "
            "FROM processed_emails p "
            "JOIN (SELECT COALESCE(thread_id, message_id) AS tkey, MAX(id) AS max_id "
            "FROM processed_emails GROUP BY tkey) latest ON p.id = latest.max_id",
            tuple(params),
        )
        row = cur.fetchone() or {}
        try:
            cur.execute("SELECT COUNT(*) AS n FROM drafts")
            server_drafts = int((cur.fetchone() or {}).get("n") or 0)
        except Exception:
            # Drafts table predates this deploy (init_db runs in a
            # background thread); count mail drafts only until it exists.
            server_drafts = 0
    counts = {f: int(row.get(f) or 0) for f in folders}
    counts["drafts"] = counts["drafts"] + server_drafts
    return counts


# ---- Keyset pagination over threads ----

_VALID_FOLDERS = ("inbox", "starred", "unread", "urgent", "reply", "all",
                  "sent", "drafts", "archive", "trash", "spam")
# Folders whose rows come from the drafts-table union (see
# list_threads): server drafts sort before Gmail drafts.
_DRAFT_SORT_ID_OFFSET = 2_000_000_000
# A thread's latest message can never have a processed_emails id this
# large in practice (150 mails per sync run); the offset keeps the two
# id spaces in one keyset ordering without a second cursor format.
_VALID_CATS = (None, "primary", "social", "promotions", "updates", "forums")
_CAT_LABEL = {
    "social": "CATEGORY_SOCIAL",
    "promotions": "CATEGORY_PROMOTIONS",
    "updates": "CATEGORY_UPDATES",
    "forums": "CATEGORY_FORUMS",
}


def _encode_cursor(last_id: int, folder: str, cat: Optional[str], q: Optional[str]) -> str:
    payload = json.dumps({"v": 1, "last_id": int(last_id), "folder": folder, "cat": cat, "q": q or None})
    return _base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str, folder: str, cat: Optional[str], q: Optional[str]) -> int:
    """Decode a list cursor and validate it belongs to this exact query.

    A cursor from another folder/category/search would silently skip or
    repeat rows, so a mismatch is a 400, not a best-effort decode.
    Raises ValueError on any invalid or mismatched cursor.
    """
    try:
        payload = json.loads(_base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except Exception:
        raise ValueError("Invalid pagination cursor.")
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("Invalid pagination cursor.")
    if payload.get("folder") != folder or payload.get("cat") != cat or (payload.get("q") or None) != (q or None):
        raise ValueError("Pagination cursor does not match this folder/category/search.")
    last_id = payload.get("last_id")
    if not isinstance(last_id, int) or last_id <= 0:
        raise ValueError("Invalid pagination cursor.")
    return last_id


def list_threads(folder: str = "inbox", cat: Optional[str] = None,
                 q: Optional[str] = None, limit: int = 30,
                 cursor: Optional[str] = None) -> tuple[list[dict], Optional[str], bool]:
    """One row per THREAD: the latest message of each thread, id DESC.

    Keyset pagination (NEVER SQL OFFSET): the cursor is an opaque token
    holding the last seen row id plus the query it belongs to; each page
    fetches `limit + 1` rows with `id < last_id`. Returns
    (rows, next_cursor, has_more). Each row carries the full
    processed_emails columns plus message_count / unread_count for its
    thread. Folder/cat/q filters apply to the thread's LATEST message.
    """
    folder = (folder or "inbox").lower()
    if folder not in _VALID_FOLDERS:
        raise ValueError(f"Unknown folder: {folder}")
    if cat not in _VALID_CATS:
        raise ValueError(f"Unknown category: {cat}")
    limit = max(1, min(100, int(limit or 30)))
    q = (q or "").strip() or None

    last_id = _decode_cursor(cursor, folder, cat, q) if cursor else None

    if folder == "drafts":
        return _list_draft_threads(cat=cat, q=q, limit=limit, last_id=last_id)

    # Folder predicate on the latest message of the thread, from the
    # stored Gmail labels CSV (see _folder_predicate_sql). Starred
    # keeps archived mail visible, like Gmail's Starred view; every
    # other mail folder excludes trash, and unread/urgent/reply are
    # inbox-scoped.
    folder_sql, folder_params = _folder_predicate_sql(folder, "p")
    params: list[Any] = list(folder_params)

    cat_sql = ""
    if cat in _CAT_LABEL:
        cat_sql = " AND p.labels LIKE %s"
        params.append(f"%{_CAT_LABEL[cat]}%")
    elif cat == "primary":
        # Primary = no CATEGORY_* label at all (CATEGORY_PERSONAL also
        # counts as primary, matching _category_from_labels).
        # COALESCE: a NULL labels value must count as "no category",
        # not silently drop the row (NULL NOT LIKE is NULL in MySQL).
        # HOTFIX 2026-11-16: the patterns are bound params, never
        # inlined — PyMySQL executes queries as ``query % params``, so
        # the old literal '%CATEGORY_SOCIAL%' raised ValueError
        # ("unsupported format character 'C' (0x43) at index 855")
        # and broke the Primary tab on every fetch. The other
        # categories already used bound params (see above).
        cat_sql = " AND " + " AND ".join(
            "COALESCE(p.labels, '') NOT LIKE %s" for _ in _CAT_LABEL
        )
        params.extend(f"%{label}%" for label in _CAT_LABEL.values())

    q_sql = ""
    if q:
        pattern = f"%{q}%"
        q_sql = " AND (p.subject LIKE %s OR p.sender LIKE %s OR p.snippet LIKE %s)"
        params.extend([pattern, pattern, pattern])

    cursor_sql = ""
    if last_id is not None:
        cursor_sql = " AND p.id < %s"
        params.append(last_id)

    params.append(limit + 1)
    with get_db_cursor() as cur:
        cur.execute(
            f"""
            SELECT p.*,
                (SELECT COUNT(*) FROM processed_emails t
                 WHERE COALESCE(t.thread_id, t.message_id) = COALESCE(p.thread_id, p.message_id)) AS message_count,
                (SELECT COUNT(*) FROM processed_emails t
                 WHERE COALESCE(t.thread_id, t.message_id) = COALESCE(p.thread_id, p.message_id)
                   AND t.is_unread = TRUE) AS unread_count
            FROM processed_emails p
            JOIN (
                SELECT COALESCE(thread_id, message_id) AS tkey, MAX(id) AS max_id
                FROM processed_emails GROUP BY tkey
            ) latest ON p.id = latest.max_id
            WHERE {folder_sql}{cat_sql}{q_sql}{cursor_sql}
            ORDER BY p.id DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = _encode_cursor(page_rows[-1]["id"], folder, cat, q) if has_more and page_rows else None
    return page_rows, next_cursor, has_more


def _list_draft_threads(cat: Optional[str], q: Optional[str], limit: int,
                        last_id: Optional[int]) -> tuple[list[dict], Optional[str], bool]:
    """Drafts folder: UNION of Gmail drafts + server drafts, one keyset.

    - Gmail drafts: threads whose latest message carries the DRAFT
      label (same latest-message rule as every other folder).
    - Server drafts: rows of the ``drafts`` table (compose/reply
      autosaves), shaped into the same thread-like columns with
      ``is_draft=1`` and ``draft_id`` set, so the API layer can tell
      them apart from mail rows.
    - One keyset, no OFFSET: server-draft sort ids are
      ``draft id + _DRAFT_SORT_ID_OFFSET`` (2e9 — a real mail id can
      never reach it), so both parts share the single integer ``id``
      ordering and the v1 cursor format is unchanged. Server drafts
      sort before Gmail drafts. ``cat`` is ignored here (drafts have
      no inbox category); ``q`` matches subject/body/recipients.
    """
    mail_pred, _ = _folder_predicate_sql("drafts", "p")
    mail_params: list[Any] = []
    q_sql_mail = ""
    if q:
        pattern = f"%{q}%"
        q_sql_mail = " AND (p.subject LIKE %s OR p.sender LIKE %s OR p.snippet LIKE %s)"
        mail_params.extend([pattern, pattern, pattern])
    mail_cursor_sql = ""
    if last_id is not None:
        mail_cursor_sql = " AND p.id < %s"
        mail_params.append(last_id)

    draft_params: list[Any] = []
    q_sql_draft = ""
    if q:
        pattern = f"%{q}%"
        q_sql_draft = " WHERE (d.subject LIKE %s OR d.body LIKE %s OR d.to_addrs LIKE %s)"
        draft_params.extend([pattern, pattern, pattern])
    draft_cursor_sql = ""
    if last_id is not None:
        draft_cursor_sql = (
            (" AND " if q_sql_draft else " WHERE ")
            + f"(d.id + {_DRAFT_SORT_ID_OFFSET}) < %s"
        )
        draft_params.append(last_id)

    with get_db_cursor() as cur:
        cur.execute(
            f"""
            SELECT p.id AS id, p.message_id AS message_id, p.thread_id AS thread_id,
                p.sender AS sender, p.subject AS subject, p.snippet AS snippet,
                p.is_unread AS is_unread, p.is_starred AS is_starred,
                p.labels AS labels, p.is_archived AS is_archived,
                p.is_trashed AS is_trashed, p.received_at AS received_at,
                p.created_at AS created_at, p.created_at AS updated_at,
                (SELECT COUNT(*) FROM processed_emails t
                 WHERE COALESCE(t.thread_id, t.message_id) = COALESCE(p.thread_id, p.message_id)) AS message_count,
                (SELECT COUNT(*) FROM processed_emails t
                 WHERE COALESCE(t.thread_id, t.message_id) = COALESCE(p.thread_id, p.message_id)
                   AND t.is_unread = TRUE) AS unread_count,
                0 AS is_draft, NULL AS draft_id,
                NULL AS to_addrs, NULL AS cc_addrs, NULL AS bcc_addrs,
                NULL AS body_text
            FROM processed_emails p
            JOIN (
                SELECT COALESCE(thread_id, message_id) AS tkey, MAX(id) AS max_id
                FROM processed_emails GROUP BY tkey
            ) latest ON p.id = latest.max_id
            WHERE {mail_pred}{q_sql_mail}{mail_cursor_sql}
            UNION ALL
            SELECT (d.id + {_DRAFT_SORT_ID_OFFSET}) AS id, NULL AS message_id,
                d.thread_id AS thread_id, 'Me' AS sender,
                d.subject AS subject, LEFT(COALESCE(d.body, ''), 200) AS snippet,
                0 AS is_unread, 0 AS is_starred, 'DRAFT' AS labels,
                0 AS is_archived, 0 AS is_trashed,
                d.updated_at AS received_at, d.created_at AS created_at,
                d.updated_at AS updated_at,
                1 AS message_count, 0 AS unread_count,
                1 AS is_draft, d.id AS draft_id,
                d.to_addrs AS to_addrs, d.cc_addrs AS cc_addrs,
                d.bcc_addrs AS bcc_addrs, d.body AS body_text
            FROM drafts d
            {q_sql_draft}{draft_cursor_sql}
            ORDER BY id DESC
            LIMIT %s
            """,
            tuple(mail_params + draft_params + [limit + 1]),
        )
        rows = cur.fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = _encode_cursor(page_rows[-1]["id"], "drafts", cat, q) if has_more and page_rows else None
    return page_rows, next_cursor, has_more


# ================= Drafts (server-side, Phase 2) =================

def _draft_row_to_dict(row: Optional[dict]) -> Optional[dict]:
    """One drafts-table row -> plain dict (datetimes ISO, addrs as lists)."""
    if not row:
        return None

    def _split(raw: Any) -> list[str]:
        return [p.strip() for p in str(raw or "").split(",") if p.strip()]

    return {
        "id": row["id"],
        "to": _split(row.get("to_addrs")),
        "cc": _split(row.get("cc_addrs")),
        "bcc": _split(row.get("bcc_addrs")),
        "subject": row.get("subject") or "",
        "body": row.get("body") or "",
        "thread_id": row.get("thread_id"),
        "message_id": row.get("message_id"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def create_draft(to_addrs: list[str], cc_addrs: list[str], bcc_addrs: list[str],
                 subject: str, body: str,
                 thread_id: Optional[str] = None,
                 message_id: Optional[str] = None) -> dict:
    """Insert a server draft; returns the created draft dict."""
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO drafts (to_addrs, cc_addrs, bcc_addrs, subject, body, thread_id, message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (",".join(to_addrs), ",".join(cc_addrs), ",".join(bcc_addrs),
             subject or "", body or "", thread_id, message_id),
        )
        draft_id = cur.lastrowid
    draft = get_draft(draft_id)
    assert draft is not None
    return draft


def get_draft(draft_id: int) -> Optional[dict]:
    """One draft by id, or None."""
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM drafts WHERE id = %s", (int(draft_id),))
        return _draft_row_to_dict(cur.fetchone())


def list_drafts(limit: int = 30, cursor: Optional[str] = None) -> tuple[list[dict], Optional[str], bool]:
    """Server drafts only, newest first, keyset on drafts.id DESC.

    Its own cursor namespace (folder="drafts_list") so a drafts-table
    cursor can never be mistaken for a drafts-FOLDER cursor (the
    folder union sorts on offset ids — see _list_draft_threads).
    """
    limit = max(1, min(100, int(limit or 30)))
    last_id = _decode_cursor(cursor, "drafts_list", None, None) if cursor else None
    with get_db_cursor() as cur:
        if last_id is not None:
            cur.execute(
                "SELECT * FROM drafts WHERE id < %s ORDER BY id DESC LIMIT %s",
                (last_id, limit + 1),
            )
        else:
            cur.execute(
                "SELECT * FROM drafts ORDER BY id DESC LIMIT %s",
                (limit + 1,),
            )
        rows = [_draft_row_to_dict(r) for r in cur.fetchall()]
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = _encode_cursor(page_rows[-1]["id"], "drafts_list", None, None) if has_more and page_rows else None
    return page_rows, next_cursor, has_more


def update_draft(draft_id: int, updates: dict) -> Optional[dict]:
    """Partial update of a draft; returns the updated draft, or None
    when the id does not exist. Only whitelisted columns are written;
    ``to``/``cc``/``bcc`` accept address lists (stored comma-joined)."""
    allowed = {"to_addrs", "cc_addrs", "bcc_addrs", "subject", "body", "thread_id", "message_id"}
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in (updates or {}).items():
        if key in ("to", "cc", "bcc"):
            key = f"{key}_addrs"
            value = ",".join(value) if isinstance(value, (list, tuple)) else str(value or "")
        if key in allowed:
            clauses.append(f"{key} = %s")
            params.append(value)
    if not clauses:
        return get_draft(draft_id)
    params.append(int(draft_id))
    with get_db_cursor(commit=True) as cur:
        cur.execute(f"UPDATE drafts SET {', '.join(clauses)} WHERE id = %s", tuple(params))
        if cur.rowcount == 0:
            return None
    return get_draft(draft_id)


def delete_draft(draft_id: int) -> bool:
    """Delete a draft; True when a row was removed."""
    with get_db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM drafts WHERE id = %s", (int(draft_id),))
        return cur.rowcount > 0

# ================= Configuration Management =================

def get_system_config(key_name: str, default: Any = None) -> Any:
    """Retrieve system configuration value parsed from JSON."""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT value_text FROM system_config WHERE key_name = %s", (key_name,))
            row = cur.fetchone()
            if row and row.get("value_text"):
                return json.loads(row["value_text"])
    except Exception as e:
        logger.error(f"Error fetching system_config '{key_name}': {e}")
    return default

def set_system_config(key_name: str, value: Any) -> bool:
    """Persist system configuration value serialized as JSON."""
    try:
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with get_db_cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO system_config (key_name, value_text)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE value_text = VALUES(value_text)
            """, (key_name, val_str))
        return True
    except Exception as e:
        logger.error(f"Error setting system_config '{key_name}': {e}")
        return False

# ================= API Key Management =================

def get_api_keys(include_secret: bool = False) -> list[dict]:
    """Retrieve all API keys. Secrets are masked by default."""
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM api_keys ORDER BY priority ASC, id ASC")
        rows = cur.fetchall()

    result = []
    now = _utcnow_naive()
    for r in rows:
        key_raw = r.get("api_key", "")
        cooldown = r.get("cooldown_until")
        is_cooldown = bool(cooldown and cooldown > now)
        item = {
            "id": r["id"],
            "provider": r["provider"],
            "label": r["label"],
            "model_name": r["model_name"],
            "priority": r["priority"],
            "is_active": bool(r["is_active"]),
            "rate_limit_rpm": r["rate_limit_rpm"],
            "is_in_cooldown": is_cooldown,
            "cooldown_until": cooldown.isoformat() if cooldown else None,
            "total_requests": r["total_requests"],
            "success_requests": r["success_requests"],
            "failed_requests": r["failed_requests"],
            "last_error": r.get("last_error"),
            "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "api_key_masked": mask_api_key(key_raw)
        }
        if include_secret:
            item["api_key"] = decrypt_api_key(key_raw)
        result.append(item)
    return result


def get_api_key_by_id(key_id: int) -> Optional[dict]:
    """Retrieve one API key row with the secret decrypted (internal use only).

    Callers must never return ``api_key`` to the frontend. Use this instead
    of a raw ``SELECT * FROM api_keys`` so encrypted rows actually work.
    """
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM api_keys WHERE id = %s", (key_id,))
        row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    row["api_key"] = decrypt_api_key(row.get("api_key", ""))
    return row

def get_active_api_keys(provider: Optional[str] = None) -> list[dict]:
    """Retrieve all active keys not currently in cooldown, sorted by priority.

    Returned rows carry the DECRYPTED ``api_key`` — this is internal-only
    (LLM callers); never serialize these rows to an API response.
    """
    with get_db_cursor() as cur:
        now = _utcnow_naive()
        if provider:
            cur.execute("""
                SELECT * FROM api_keys 
                WHERE is_active = TRUE AND provider = %s 
                  AND (cooldown_until IS NULL OR cooldown_until <= %s)
                ORDER BY priority ASC, success_requests ASC, id ASC
            """, (provider, now))
        else:
            cur.execute("""
                SELECT * FROM api_keys 
                WHERE is_active = TRUE 
                  AND (cooldown_until IS NULL OR cooldown_until <= %s)
                ORDER BY priority ASC, success_requests ASC, id ASC
            """, (now,))
        rows = cur.fetchall()
    for r in rows:
        r["api_key"] = decrypt_api_key(r.get("api_key", ""))
    return rows

def add_api_key(provider: str, label: str, api_key: str, model_name: str, priority: int = 1, rate_limit_rpm: int = 15) -> int:
    """Add a new AI provider API key (encrypted at rest when configured)."""
    with get_db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO api_keys (provider, label, api_key, model_name, priority, is_active, rate_limit_rpm)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s)
        """, (provider.strip().lower(), label.strip(), encrypt_api_key(api_key.strip()), model_name.strip(), priority, rate_limit_rpm))
        return cur.lastrowid

def update_api_key(key_id: int, updates: dict) -> bool:
    """Update fields for a specific API key."""
    allowed = ["label", "api_key", "model_name", "priority", "is_active", "rate_limit_rpm"]
    clauses = []
    params = []
    for k, v in updates.items():
        if k in allowed:
            clauses.append(f"{k} = %s")
            # Re-encrypt a rotated key value instead of storing it raw.
            params.append(encrypt_api_key(str(v).strip()) if k == "api_key" else v)
    if not clauses:
        return False
    params.append(key_id)
    with get_db_cursor(commit=True) as cur:
        cur.execute(f"UPDATE api_keys SET {', '.join(clauses)} WHERE id = %s", tuple(params))
        return cur.rowcount > 0

def delete_api_key(key_id: int) -> bool:
    """Delete an API key by id."""
    with get_db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
        return cur.rowcount > 0

def record_api_key_usage(key_id: int, success: bool, error_msg: Optional[str] = None):
    """Record request outcome for usage tracking and health monitoring."""
    with get_db_cursor(commit=True) as cur:
        if success:
            cur.execute("""
                UPDATE api_keys SET
                    total_requests = total_requests + 1,
                    success_requests = success_requests + 1,
                    last_used_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (key_id,))
        else:
            cur.execute("""
                UPDATE api_keys SET
                    total_requests = total_requests + 1,
                    failed_requests = failed_requests + 1,
                    last_error = %s,
                    last_used_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (error_msg[:500] if error_msg else "Unknown error", key_id))

def set_api_key_cooldown(key_id: int, seconds: int = 60, error_msg: Optional[str] = None):
    """Place an API key on temporary cooldown after rate limits or 429 quota exhaustion."""
    cooldown_time = _utcnow_naive() + timedelta(seconds=seconds)
    with get_db_cursor(commit=True) as cur:
        cur.execute("""
            UPDATE api_keys SET
                cooldown_until = %s,
                last_error = %s,
                last_used_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (cooldown_time, error_msg[:500] if error_msg else "Rate limited / cooldown", key_id))

# ================= Persistent Summary Jobs & Batches =================

def create_summary_job(job_uuid: str, date_range_info: str, total_emails: int, total_batches: int) -> int:
    """Create a new summary job tracker in TiDB."""
    with get_db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO summary_jobs (job_uuid, status, date_range_info, total_emails, total_batches, completed_batches, failed_batches)
            VALUES (%s, 'PROCESSING', %s, %s, %s, 0, 0)
        """, (job_uuid, date_range_info, total_emails, total_batches))
        return cur.lastrowid

def update_summary_job(job_id: int, updates: dict) -> bool:
    """Update status, outputs, and counts for a summary job."""
    allowed = [
        "status", "completed_batches", "failed_batches", "executive_summary",
        "email_markdown_report", "action_items", "urgent_count", "error_message"
    ]
    clauses = []
    params = []
    for k, v in updates.items():
        if k in allowed:
            clauses.append(f"{k} = %s")
            params.append(json.dumps(v) if k == "action_items" and isinstance(v, (list, dict)) else v)
    if not clauses:
        return False
    params.append(job_id)
    with get_db_cursor(commit=True) as cur:
        cur.execute(f"UPDATE summary_jobs SET {', '.join(clauses)} WHERE id = %s", tuple(params))
        return cur.rowcount > 0

def get_summary_job(job_id: int) -> Optional[dict]:
    """Retrieve summary job by ID."""
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM summary_jobs WHERE id = %s", (job_id,))
        return cur.fetchone()

def get_latest_summary_jobs(limit: int = 10) -> list[dict]:
    """Retrieve recent summary jobs."""
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM summary_jobs ORDER BY id DESC LIMIT %s", (limit,))
        return cur.fetchall()

def create_summary_batch(job_id: int, batch_index: int, email_count: int, email_ids: list[str], api_key_id: Optional[int] = None) -> int:
    """Record an individual batch slice in TiDB for fault-tolerant execution."""
    with get_db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO summary_batches (job_id, batch_index, email_count, email_ids, api_key_id, status)
            VALUES (%s, %s, %s, %s, %s, 'PENDING')
        """, (job_id, batch_index, email_count, json.dumps(email_ids), api_key_id))
        return cur.lastrowid

def update_summary_batch(batch_id: int, status: str, batch_summary: Optional[str] = None, action_items: Optional[list] = None, error_message: Optional[str] = None, retry_count: Optional[int] = None):
    """Update batch processing result and status."""
    with get_db_cursor(commit=True) as cur:
        clauses = ["status = %s"]
        params = [status]
        if batch_summary is not None:
            clauses.append("batch_summary = %s")
            params.append(batch_summary)
        if action_items is not None:
            clauses.append("action_items = %s")
            params.append(json.dumps(action_items))
        if error_message is not None:
            clauses.append("error_message = %s")
            params.append(error_message[:1000])
        if retry_count is not None:
            clauses.append("retry_count = %s")
            params.append(retry_count)

        params.append(batch_id)
        cur.execute(f"UPDATE summary_batches SET {', '.join(clauses)} WHERE id = %s", tuple(params))

def get_job_batches(job_id: int) -> list[dict]:
    """Retrieve all batch slices for a specific job."""
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM summary_batches WHERE job_id = %s ORDER BY batch_index ASC", (job_id,))
        return cur.fetchall()

# ================= Dashboard & Reports =================

def save_daily_report(report_date, total_emails: int, urgent_count: int, summary_markdown: str, action_items: list):
    """Save or update today's daily report in TiDB."""
    with get_db_cursor(commit=True) as cur:
        sql = """
            INSERT INTO daily_reports (report_date, total_emails, urgent_count, summary_markdown, action_items)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_emails = VALUES(total_emails),
                urgent_count = VALUES(urgent_count),
                summary_markdown = VALUES(summary_markdown),
                action_items = VALUES(action_items),
                created_at = CURRENT_TIMESTAMP
        """
        cur.execute(sql, (
            report_date,
            total_emails,
            urgent_count,
            summary_markdown,
            json.dumps(action_items)
        ))

def get_dashboard_summary():
    """Retrieve the latest report, 7-day history, and recent processed emails."""
    with get_db_cursor() as cur:
        # Latest Report
        cur.execute("SELECT * FROM daily_reports ORDER BY report_date DESC LIMIT 1")
        latest = cur.fetchone()

        # Past 7 Days History
        cur.execute("SELECT report_date, total_emails, urgent_count, created_at FROM daily_reports ORDER BY report_date DESC LIMIT 7")
        history = cur.fetchall()

        # Recent Processed Emails
        cur.execute("SELECT id, message_id, sender, subject, is_unread, is_starred, created_at FROM processed_emails ORDER BY id DESC LIMIT 20")
        recent_emails = cur.fetchall()

        # Lifetime total emails processed
        cur.execute("SELECT COUNT(*) AS count FROM processed_emails")
        lifetime_emails = cur.fetchone()

        # Live inbox counts (SQL aggregates over the mirror; the dashboard
        # stat boxes read these instead of deriving counts client-side).
        try:
            inbox_counts = get_inbox_counts()
        except Exception as e:
            logger.warning(f"Could not compute inbox counts: {e}")
            inbox_counts = None

        # Per-folder thread counts (same predicates as the folder lists,
        # so sidebar badges agree with what each folder shows).
        try:
            folder_counts = get_folder_counts()
        except Exception as e:
            logger.warning(f"Could not compute folder counts: {e}")
            folder_counts = None

        return {
            "latest": latest,
            "history": history,
            "recent_emails": recent_emails,
            "lifetime_emails": lifetime_emails.get("count", 0) if lifetime_emails else 0,
            "inbox_counts": inbox_counts,
            "folder_counts": folder_counts
        }
