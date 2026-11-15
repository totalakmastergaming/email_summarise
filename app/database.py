import json
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from queue import Queue, Empty
from threading import Lock
from typing import Any, Optional
import pymysql
import pymysql.cursors
from app.config import settings

logger = logging.getLogger(__name__)

class TiDBConnectionPool:
    """
    Lightweight, thread-safe connection pool for TiDB Cloud Serverless.
    Reuses open TLS/SSL connections to eliminate ~200-300ms handshake overhead per query.
    """
    def __init__(self, max_connections: int = 5):
        self.max_connections = max_connections
        self._pool: Queue = Queue(maxsize=max_connections)
        self._lock = Lock()

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
                return self._create_connection()
        except Empty:
            return self._create_connection()

    def release_connection(self, conn):
        if not conn:
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            # Pool full, close socket safely
            try:
                conn.close()
            except Exception:
                pass

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

def init_db():
    """Initialize and migrate required tables in TiDB."""
    try:
        with get_db_cursor(commit=True) as cur:
            # 1. Processed emails deduplication & inbox state table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message_id VARCHAR(255) UNIQUE NOT NULL,
                    sender VARCHAR(255),
                    subject VARCHAR(500),
                    snippet TEXT,
                    is_unread BOOLEAN DEFAULT TRUE,
                    is_starred BOOLEAN DEFAULT FALSE,
                    labels VARCHAR(500) NULL,
                    received_at DATETIME NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_msg_id (message_id)
                );
            """)

            # Safe column additions if table already existed from prior runs
            cur.execute("SHOW COLUMNS FROM processed_emails")
            existing_cols = {row['Field'] for row in cur.fetchall()}
            if "is_unread" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN is_unread BOOLEAN DEFAULT TRUE")
            if "is_starred" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN is_starred BOOLEAN DEFAULT FALSE")
            if "labels" not in existing_cols:
                cur.execute("ALTER TABLE processed_emails ADD COLUMN labels VARCHAR(500) NULL")

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
            cur.execute("SELECT COUNT(*) AS count FROM api_keys")
            cnt_row = cur.fetchone()
            if cnt_row and cnt_row.get("count", 0) == 0 and settings.GEMINI_API_KEY:
                cur.execute("""
                    INSERT INTO api_keys (provider, label, api_key, model_name, priority, is_active, rate_limit_rpm)
                    VALUES (%s, %s, %s, %s, 1, TRUE, 15)
                """, ("gemini", "Primary Gemini Flash", settings.GEMINI_API_KEY, settings.GEMINI_MODEL))
                logger.info("Seeded primary Gemini API key into api_keys table.")

        logger.info("TiDB schema verified and tables initialized successfully.")
    except Exception as e:
        logger.warning(f"Could not connect to TiDB during init (check credentials): {e}")

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
    """Insert or update processed emails into TiDB with live read/starred states."""
    if not emails_data:
        return
    with get_db_cursor(commit=True) as cur:
        sql = """
            INSERT INTO processed_emails (message_id, sender, subject, snippet, is_unread, is_starred, labels, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                sender = VALUES(sender),
                subject = VALUES(subject),
                snippet = VALUES(snippet),
                is_unread = VALUES(is_unread),
                is_starred = VALUES(is_starred),
                labels = VALUES(labels)
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
            values.append((
                e["id"],
                e.get("sender", "Unknown"),
                e.get("subject", "(No Subject)"),
                e.get("snippet", ""),
                bool(e.get("is_unread", True)),
                bool(e.get("is_starred", False)),
                labels_str,
                rec_at
            ))
        cur.executemany(sql, values)

def update_processed_email_state(message_id_or_pk: str, is_unread: Optional[bool] = None, is_starred: Optional[bool] = None) -> bool:
    """Update local email read/starred status in TiDB."""
    updates = []
    params = []
    if is_unread is not None:
        updates.append("is_unread = %s")
        params.append(bool(is_unread))
    if is_starred is not None:
        updates.append("is_starred = %s")
        params.append(bool(is_starred))
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
    now = datetime.utcnow()
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
            item["api_key"] = key_raw
        result.append(item)
    return result

def get_active_api_keys(provider: Optional[str] = None) -> list[dict]:
    """Retrieve all active keys not currently in cooldown, sorted by priority."""
    with get_db_cursor() as cur:
        now = datetime.utcnow()
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
        return cur.fetchall()

def add_api_key(provider: str, label: str, api_key: str, model_name: str, priority: int = 1, rate_limit_rpm: int = 15) -> int:
    """Add a new AI provider API key."""
    with get_db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO api_keys (provider, label, api_key, model_name, priority, is_active, rate_limit_rpm)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s)
        """, (provider.strip().lower(), label.strip(), api_key.strip(), model_name.strip(), priority, rate_limit_rpm))
        return cur.lastrowid

def update_api_key(key_id: int, updates: dict) -> bool:
    """Update fields for a specific API key."""
    allowed = ["label", "api_key", "model_name", "priority", "is_active", "rate_limit_rpm"]
    clauses = []
    params = []
    for k, v in updates.items():
        if k in allowed:
            clauses.append(f"{k} = %s")
            params.append(v)
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
    cooldown_time = datetime.utcnow() + timedelta(seconds=seconds)
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

        return {
            "latest": latest,
            "history": history,
            "recent_emails": recent_emails,
            "lifetime_emails": lifetime_emails.get("count", 0) if lifetime_emails else 0
        }
