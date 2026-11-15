import hmac
import json
import logging
from datetime import datetime
from typing import Optional, Any
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Header, HTTPException, status, Path
from pydantic import BaseModel

from app.config import settings
from app.database import (
    get_dashboard_summary,
    get_unprocessed_message_ids,
    save_processed_emails,
    save_daily_report,
    get_db_cursor,
    update_processed_email_state,
    get_email_by_id_or_msg_id,
    get_system_config,
    set_system_config,
    get_api_keys,
    get_api_key_by_id,
    add_api_key,
    update_api_key,
    delete_api_key,
    record_api_key_usage,
    get_latest_summary_jobs,
    get_job_batches
)
from app.cache import dashboard_cache, assistant_cache, invalidate_inbox_caches
from app.services.gmail_service import (
    fetch_recent_emails,
    get_email_full,
    update_gmail_message_state,
    batch_update_message_states,
    sync_inbox_labels_from_gmail,
    list_user_labels,
    modify_message_labels,
    send_email,
    reply_to_email
)
from app.services.llm_provider import (
    execute_with_failover,
    call_provider,
    LLMProviderError
)
from app.services.pipeline_service import execute_consolidated_pipeline

logger = logging.getLogger(__name__)


def require_admin(authorization: Optional[str] = Header(None)):
    """Gate the whole /api surface behind a single admin token.

    The dashboard is a personal Gmail workspace served from a public
    Render URL; without this, anyone on the internet could read mail,
    delete API keys, or trash Gmail messages (the API was fully open).
    - ADMIN_TOKEN unset -> 503 fail-closed (never fail open).
    - Wrong/missing token -> 401.
    The UI stores the token in sessionStorage and sends
    `Authorization: Bearer <token>` on every /api call. The external
    cron webhook (/api/cron-trigger) keeps its own CRON_SECRET check and
    lives on a separate router below, outside this dependency.
    """
    token = (settings.ADMIN_TOKEN or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin token is not configured on the server. "
                   "Set ADMIN_TOKEN in .env / Render env vars, then reload."
        )
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin token required. Enter it in the Configuration view."
        )


router = APIRouter(prefix="/api", tags=["API"], dependencies=[Depends(require_admin)])

# Cron webhook router: authenticated by CRON_SECRET only, NOT by the
# admin token (external schedulers like cron-job.org do not have it).
cron_router = APIRouter(prefix="/api", tags=["Cron"])

# ================= Request / Response Schemas =================

class ChatRequest(BaseModel):
    question: str

class MailStateRequest(BaseModel):
    is_unread: Optional[bool] = None
    is_starred: Optional[bool] = None
    is_trash: Optional[bool] = None
    is_archived: Optional[bool] = None
    is_spam: Optional[bool] = None


class BulkStateRequest(BaseModel):
    """One label change for many mails (UI ids like 1000+db id, or Gmail ids)."""
    ids: list[str]
    is_unread: Optional[bool] = None
    is_starred: Optional[bool] = None
    is_trash: Optional[bool] = None
    is_archived: Optional[bool] = None
    is_spam: Optional[bool] = None


class LabelRequest(BaseModel):
    """Apply/remove Gmail labels on one mail. Label ids are Gmail label
    ids (e.g. user-created label ids, or system ids like IMPORTANT)."""
    add: list[str] = []
    remove: list[str] = []


class ReplyRequest(BaseModel):
    body: str


class SendRequest(BaseModel):
    to: str
    subject: str
    body: str

class ConfigUpdateRequest(BaseModel):
    schedule: Optional[dict] = None
    range: Optional[dict] = None

class ApiKeyCreateRequest(BaseModel):
    provider: str
    label: str
    api_key: str
    model_name: str
    priority: int = 1
    rate_limit_rpm: int = 15

class ApiKeyUpdateRequest(BaseModel):
    label: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    rate_limit_rpm: Optional[int] = None

class SummaryTriggerRequest(BaseModel):
    range: Optional[dict] = None
    force_all: bool = False


# ================= Inbox derivation helpers =================
# Gmail's own category labels are the source of truth for Primary /
# Social / Promotions. Previously `cat` was hardcoded to "primary" for
# every mail, so the Social and Promotions tabs were empty by
# construction even when Gmail had categorised the mail.

_GMAIL_CATEGORY_TO_CAT = {
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
    "CATEGORY_PERSONAL": "primary",
}
_USER_TZ = ZoneInfo("Asia/Kolkata")  # App's configured default timezone.


def _category_from_labels(labels: Any) -> str:
    """Map Gmail labelIds (stored comma-separated) to an inbox category."""
    if isinstance(labels, str):
        label_list = [part.strip() for part in labels.split(",") if part.strip()]
    elif isinstance(labels, (list, tuple)):
        label_list = list(labels)
    else:
        label_list = []
    for label in label_list:
        if label in _GMAIL_CATEGORY_TO_CAT:
            return _GMAIL_CATEGORY_TO_CAT[label]
    return "primary"


def _is_today(received_at: Any) -> bool:
    """True when the mail arrived today in the user's timezone.

    Previously `"today": True` was hardcoded for every row. received_at
    is stored as naive UTC; compare calendar dates in Asia/Kolkata.
    """
    if not received_at:
        return False
    try:
        from datetime import timezone as _tz

        if isinstance(received_at, str):
            received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        aware = received_at.replace(tzinfo=_tz.utc) if received_at.tzinfo is None else received_at
        return aware.astimezone(_USER_TZ).date() == datetime.now(_USER_TZ).date()
    except Exception:
        return False


def _display_time(received_at: Any, created_at: Any) -> str:
    """Clock time for a row, from when the mail ARRIVED (not when synced)."""
    from datetime import timezone as _tz

    src = received_at or created_at
    if not src:
        return "Today"
    try:
        if isinstance(src, str):
            src = datetime.fromisoformat(src.replace("Z", "+00:00"))
        aware = src.replace(tzinfo=_tz.utc) if src.tzinfo is None else src
        return aware.astimezone(_USER_TZ).strftime("%I:%M %p")
    except Exception:
        return "Today"


_AUTOMATED_SENDER_TOKENS = (
    "no-reply", "noreply", "donotreply", "do-not-reply",
    "notification", "digest", "updates@", "newsletter",
)
_NO_REPLY_SUBJECT_TOKENS = (
    "newsletter", "digest", "notification only", "unsubscribe",
    "your receipt", "order confirmed", "password reset", "verify your",
)


def _needs_reply(is_unread: bool, category: str, sender: str, subject: str) -> bool:
    """Heuristic 'needs a reply' flag — NOT just `= urgent` any more.

    A mail plausibly needs a reply when it is unread, in Primary, from a
    sender that looks like a person (not a no-reply/automated address),
    and its subject does not read as automated mail. This is a heuristic:
    true reply detection (does the thread already contain a reply sent
    by the user?) needs thread history and is an open item — see NOTES.
    """
    if not is_unread or category != "primary":
        return False
    sender_lower = (sender or "").lower()
    if any(tok in sender_lower for tok in _AUTOMATED_SENDER_TOKENS):
        return False
    subject_lower = (subject or "").lower()
    if any(tok in subject_lower for tok in _NO_REPLY_SUBJECT_TOKENS):
        return False
    return True


# ================= Dashboard & Mails Endpoints =================

@router.get("/dashboard")
def get_dashboard_data():
    """
    Retrieve all data needed to render the UI dashboard.
    Cached in RAM with 60s TTL to eliminate repeated database roundtrips.
    """
    cached = dashboard_cache.get("summary")
    if cached is not None:
        cached_copy = dict(cached)
        cached_copy["cached"] = True
        return cached_copy

    try:
        data = get_dashboard_summary()
        result = {
            "status": "success",
            "data": data,
            "cached": False,
            "config": {
                "recipient_email": settings.MY_EMAIL,
                "model": settings.GEMINI_MODEL
            }
        }
        dashboard_cache.set("summary", result, ttl=60)
        return result
    except Exception as e:
        logger.error(f"Error fetching dashboard summary: {e}")
        return {
            "status": "warning",
            "message": f"Database not yet populated or error connecting: {str(e)}",
            "data": {"latest": None, "history": [], "recent_emails": [], "lifetime_emails": 0},
            "cached": False,
            "config": {
                "recipient_email": settings.MY_EMAIL,
                "model": settings.GEMINI_MODEL
            }
        }

@router.get("/mails")
def get_inbox_mails(q: Optional[str] = None):
    """
    Returns processed emails formatted for the InboxCura UI.
    Cached in RAM with 60s TTL for instant page loads.
    `q` searches subject / sender / snippet server-side (the UI search
    used to filter only the 60 already-loaded rows, so anything older
    than the latest page was unfindable). Search results bypass the
    cache so they are always fresh.
    """
    cached = None if q else dashboard_cache.get("mails")
    if cached is not None:
        cached_copy = dict(cached)
        cached_copy["cached"] = True
        return cached_copy

    try:
        with get_db_cursor() as cur:
            # Archived / trashed mail is no longer in the inbox.
            if q and q.strip():
                pattern = f"%{q.strip()}%"
                cur.execute(
                    "SELECT * FROM processed_emails "
                    "WHERE is_archived = FALSE AND is_trashed = FALSE "
                    "AND (subject LIKE %s OR sender LIKE %s OR snippet LIKE %s) "
                    "ORDER BY id DESC LIMIT 60",
                    (pattern, pattern, pattern),
                )
            else:
                cur.execute(
                    "SELECT * FROM processed_emails "
                    "WHERE is_archived = FALSE AND is_trashed = FALSE "
                    "ORDER BY id DESC LIMIT 60"
                )
            rows = cur.fetchall()

        mails = []
        for r in rows:
            received_at = r.get("received_at")
            time_str = _display_time(received_at, r.get("created_at"))
            sender_str = r.get("sender", "Unknown")
            email_part = sender_str
            name_part = sender_str
            if "<" in sender_str and ">" in sender_str:
                name_part = sender_str.split("<")[0].strip()
                email_part = sender_str.split("<")[1].split(">")[0].strip()

            snippet = r.get("snippet", "")
            subject_text = r.get("subject", "(No Subject)")
            subject_lower = subject_text.lower()
            is_urgent = any(w in subject_lower for w in ["urgent", "action required", "deadline", "immediate", "alert", "security alert"])

            # Use live values stored in database
            is_unr = bool(r.get("is_unread", True))
            is_sta = bool(r.get("is_starred", False))
            # Real Gmail category (was hardcoded "primary" for every mail).
            category = _category_from_labels(r.get("labels", ""))

            mails.append({
                "id": 1000 + r["id"],
                "message_id": r.get("message_id"),
                "sender": name_part or email_part,
                "email": email_part,
                "subject": subject_text,
                "snippet": snippet,
                "body": [snippet] if snippet else ["No further content preview."],
                "summary": snippet[:140] if snippet else "",
                "time": time_str,
                "today": _is_today(received_at),
                "unread": is_unr,
                "urgent": is_urgent,
                "starred": is_sta,
                "needsReply": _needs_reply(is_unr, category, sender_str, subject_text),
                "tag": "Alert" if is_urgent else "Inbound",
                "cat": category,
                "labels": r.get("labels", "")
            })

        result = {"status": "success", "mails": mails, "cached": False}
        if not q:
            dashboard_cache.set("mails", result, ttl=60)
        return result
    except Exception as e:
        logger.error(f"Error retrieving mails for InboxCura: {e}")
        return {"status": "error", "mails": []}

@router.get("/labels")
def list_labels():
    """User-created Gmail labels (for the label menu in the UI).

    System labels (INBOX, STARRED, ...) are not user labels and are not
    listed here; the state endpoints already manage those.
    """
    try:
        labels = list_user_labels()
    except Exception as e:
        logger.error(f"Error listing Gmail labels: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not list labels from Gmail: {str(e)}"
        )
    return {"status": "success", "labels": labels}


@router.post("/mails/{mail_id}/labels")
def update_mail_labels(mail_id: str, req: LabelRequest):
    """Apply / remove real Gmail labels on one mail.

    Previously the label button in the UI did nothing real (local-only
    toast). This calls Gmail `modify`, then mirrors the resulting label
    set in TiDB so the local list stays in sync.
    """
    add = [str(x).strip() for x in (req.add or []) if str(x).strip()]
    remove = [str(x).strip() for x in (req.remove or []) if str(x).strip()]
    if not add and not remove:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No labels to add or remove.")

    email_record = get_email_by_id_or_msg_id(mail_id)
    if not email_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")

    ok = modify_message_labels(email_record["message_id"], add_labels=add, remove_labels=remove)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gmail refused the label change."
        )

    # Mirror the new label set locally (comma-separated labelIds, same
    # format the sync stores), so category + label chips stay correct.
    stored = [p.strip() for p in str(email_record.get("labels", "")).split(",") if p.strip()]
    new_labels = sorted(set(stored) - set(remove) | set(add))
    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                "UPDATE processed_emails SET labels = %s WHERE message_id = %s",
                (",".join(new_labels), email_record["message_id"]),
            )
        dashboard_cache.delete("mails")
    except Exception as e:
        # Gmail already changed; a local mirror failure is logged, not
        # hidden — the next label sync heals it.
        logger.error(f"Error mirroring labels locally for {mail_id}: {e}")

    return {"status": "success", "labels": new_labels}


@router.get("/mails/{mail_id}")
def get_mail_detail(mail_id: str):
    """Full reading view for one mail: headers + the actual body.

    The inbox list only carries the snippet. This fetches format='full'
    from Gmail on demand (one call per open, not per sync — keeps the
    pipeline light and memory bounded) and returns plain-text + HTML.
    """
    email_record = get_email_by_id_or_msg_id(mail_id)
    if not email_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")

    try:
        full = get_email_full(email_record["message_id"])
    except Exception as e:
        logger.error(f"Error fetching full body for {mail_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch the full message from Gmail: {str(e)}"
        )

    return {
        "status": "success",
        "mail": {
            "id": 1000 + email_record["id"],
            "message_id": email_record["message_id"],
            "thread_id": full.get("thread_id"),
            "sender": full.get("sender"),
            "subject": full.get("subject"),
            "date": full.get("date"),
            "received_at": full.get("received_at"),
            "snippet": full.get("snippet"),
            "body_text": full.get("body_text", ""),
            "body_html": full.get("body_html", ""),
            "label_ids": full.get("label_ids", []),
            "cat": _category_from_labels(full.get("label_ids", [])),
            "unread": bool(email_record.get("is_unread", True)),
            "starred": bool(email_record.get("is_starred", False)),
            "archived": bool(email_record.get("is_archived", False)),
            "trashed": bool(email_record.get("is_trashed", False)),
        },
    }


@router.post("/mails/{mail_id}/reply")
def reply_to_mail(mail_id: str, req: ReplyRequest):
    """Reply in-thread via the Gmail API (not a mailto: draft)."""
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body cannot be empty.")
    if len(body) > 100000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body is too long (max 100,000 characters).")

    email_record = get_email_by_id_or_msg_id(mail_id)
    if not email_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")

    try:
        sent_id = reply_to_email(email_record["message_id"], body)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error sending reply to {mail_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the reply via Gmail: {str(e)}"
        )
    if not sent_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gmail did not return a sent message id.")
    return {"status": "success", "sent_message_id": sent_id, "in_reply_to": email_record["message_id"]}


@router.post("/mails/send")
def compose_and_send(req: SendRequest):
    """Compose + send a new email via the Gmail API."""
    to_address = (req.to or "").strip()
    subject = (req.subject or "").strip()
    body = (req.body or "").strip()
    if "@" not in to_address or "." not in to_address.split("@")[-1]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid recipient email address is required.")
    if not subject:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Subject cannot be empty.")
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body cannot be empty.")
    if len(body) > 100000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body is too long (max 100,000 characters).")

    try:
        sent_id = send_email(to_address, subject, body)
    except Exception as e:
        logger.error(f"Error sending email to {to_address}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the email via Gmail: {str(e)}"
        )
    if not sent_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gmail did not return a sent message id.")
    return {"status": "success", "sent_message_id": sent_id}


# ================= Bidirectional Gmail Sync Endpoints =================

@router.post("/mails/{mail_id}/state")
def update_mail_state(mail_id: str, req: MailStateRequest):
    """
    Synchronizes read, starred, or trash changes:
    1. Updates local state in TiDB immediately.
    2. Syncs change directly back to Gmail via Gmail API.
    """
    try:
        # Find email in database
        email_record = get_email_by_id_or_msg_id(mail_id)
        if not email_record:
            # Previously this returned {"status": "success"} for a mail
            # that does not exist, so the UI showed a success toast for a
            # no-op. A missing mail is a 404.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")

        msg_id = email_record["message_id"]

        # Update TiDB (archive/trash now persist locally too — previously
        # trash only went to Gmail, so a trashed mail kept showing in the
        # inbox list until the next full re-sync). Spam is stored as
        # archived locally: Gmail removes SPAM mail from the inbox, and
        # the label sync derives is_archived from the INBOX label, so the
        # two stay in agreement.
        update_processed_email_state(
            message_id_or_pk=msg_id,
            is_unread=req.is_unread,
            is_starred=req.is_starred,
            is_archived=req.is_archived if req.is_archived is not None else (True if req.is_spam else None),
            is_trashed=req.is_trash
        )

        # Clear mail cache so UI immediately gets fresh state
        dashboard_cache.delete("mails")

        # Sync back to Gmail
        gmail_ok = update_gmail_message_state(
            message_id=msg_id,
            is_unread=req.is_unread,
            is_starred=req.is_starred,
            is_trash=req.is_trash,
            is_archived=req.is_archived,
            is_spam=req.is_spam
        )

        return {"status": "success", "synced_gmail": gmail_ok, "message_id": msg_id}
    except HTTPException:
        raise  # 404 above must stay a 404, not become a 500.
    except Exception as e:
        logger.error(f"Error syncing mail state: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"State synchronization error: {str(e)}"
        )


@router.post("/mails/bulk-state")
def bulk_update_mail_state(req: BulkStateRequest):
    """Apply one state change to many mails — one Gmail call, not N.

    Previously bulk actions (mark-all-read, archive-all, spam-all) had no
    backend at all: the UI could only flip local rows, so Gmail itself
    never changed. This resolves ids, fires one Gmail batchModify, then
    persists the same state locally. Ids that do not resolve are skipped
    and reported, not silently counted as done.
    """
    if not req.ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No mail ids provided.")
    if len(req.ids) > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At most 100 mails per bulk request.")
    if all(v is None for v in (req.is_unread, req.is_starred, req.is_trash, req.is_archived, req.is_spam)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No state change provided.")

    resolved: list[str] = []
    skipped: list[str] = []
    for raw_id in req.ids:
        record = get_email_by_id_or_msg_id(str(raw_id))
        if record:
            resolved.append(record["message_id"])
        else:
            skipped.append(str(raw_id))

    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="None of the provided mail ids were found.")

    gmail_ok = batch_update_message_states(
        resolved,
        is_unread=req.is_unread,
        is_starred=req.is_starred,
        is_trash=req.is_trash,
        is_archived=req.is_archived,
        is_spam=req.is_spam,
    )
    # Local state mirrors Gmail only when Gmail accepted the change; if
    # Gmail failed, local rows stay as they were (next label sync heals
    # any drift instead of the UI lying about it).
    if gmail_ok:
        for msg_id in resolved:
            update_processed_email_state(
                message_id_or_pk=msg_id,
                is_unread=req.is_unread,
                is_starred=req.is_starred,
                is_archived=req.is_archived if req.is_archived is not None else (True if req.is_spam else None),
                is_trashed=req.is_trash,
            )
        dashboard_cache.delete("mails")

    return {
        "status": "success",
        "synced_gmail": gmail_ok,
        "updated_count": len(resolved) if gmail_ok else 0,
        "skipped_ids": skipped,
    }

@router.post("/sync-inbox-state")
def sync_inbox_from_gmail():
    """
    Checks Gmail for the current read/unread and starred labels of recent messages,
    updating TiDB so the web inbox is 100% identical to Gmail.
    """
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT message_id FROM processed_emails ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall()

        msg_ids = [r["message_id"] for r in rows]
        if not msg_ids:
            return {"status": "success", "updated_count": 0}

        updated_states = sync_inbox_labels_from_gmail(msg_ids)
        for state_item in updated_states:
            update_processed_email_state(
                message_id_or_pk=state_item["message_id"],
                is_unread=state_item["is_unread"],
                is_starred=state_item["is_starred"],
                is_archived=state_item.get("is_archived"),
                is_trashed=state_item.get("is_trash"),
            )

        dashboard_cache.delete("mails")
        return {"status": "success", "updated_count": len(updated_states)}
    except Exception as e:
        logger.error(f"Error syncing labels from Gmail: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inbox sync error: {str(e)}"
        )

# ================= Configuration Endpoints =================

@router.get("/config")
def get_config():
    """Retrieve current summary schedule and date range settings."""
    schedule = get_system_config("summary_schedule", {
        "enabled": False,
        "time": "18:00",
        "timezone": "Asia/Kolkata"
    })
    date_range = get_system_config("summary_range", {
        "preset": "last_1_day",
        "custom_start": "",
        "custom_end": ""
    })
    return {
        "status": "success",
        "schedule": schedule,
        "range": date_range,
        "cron_webhook_url": "/api/cron-trigger"
    }

@router.post("/config")
def update_config(req: ConfigUpdateRequest):
    """Save new summary schedule and date range settings."""
    if req.schedule is not None:
        set_system_config("summary_schedule", req.schedule)
    if req.range is not None:
        set_system_config("summary_range", req.range)
    return {"status": "success", "message": "Configuration saved successfully."}

# ================= API Key Management Endpoints =================

@router.get("/keys")
def list_api_keys():
    """Retrieve all configured AI provider keys with masked secrets and health stats."""
    keys = get_api_keys(include_secret=False)
    return {"status": "success", "keys": keys}

@router.post("/keys")
def create_api_key(req: ApiKeyCreateRequest):
    """Add a new API key."""
    if not req.api_key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="API Key cannot be empty.")
    
    new_id = add_api_key(
        provider=req.provider,
        label=req.label,
        api_key=req.api_key,
        model_name=req.model_name,
        priority=req.priority,
        rate_limit_rpm=req.rate_limit_rpm
    )
    return {"status": "success", "id": new_id, "message": "API Key added successfully."}

@router.put("/keys/{key_id}")
def edit_api_key(key_id: int, req: ApiKeyUpdateRequest):
    """Edit API key attributes or toggle active/cooldown status."""
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided to update.")
    
    ok = update_api_key(key_id, updates)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Key not found.")
    return {"status": "success", "message": "API Key updated successfully."}

@router.delete("/keys/{key_id}")
def remove_api_key(key_id: int):
    """Delete an API key."""
    ok = delete_api_key(key_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Key not found.")
    return {"status": "success", "message": "API Key deleted successfully."}

@router.post("/keys/{key_id}/test")
def test_single_api_key(key_id: int):
    """Test connection and model response for a specific API key."""
    # Decrypting read: the old raw SELECT returned the stored (possibly
    # encrypted) value straight into the provider call, so testing an
    # encrypted key always failed with an auth error.
    key = get_api_key_by_id(key_id)

    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API Key not found.")

    try:
        test_prompt = "Respond with a short JSON status: {\"status\": \"ok\", \"service\": \"InboxCura\"}"
        res = call_provider(key, test_prompt, is_json=True)
        record_api_key_usage(key_id, success=True)
        return {"status": "success", "message": "Connection verified successfully!", "response": res.strip()}
    except Exception as e:
        record_api_key_usage(key_id, success=False, error_msg=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Key test failed: {str(e)}")

# ================= AI Assistant & Summary Pipeline =================

@router.post("/chat-assistant")
def chat_assistant(req: ChatRequest):
    """
    Answers user queries in InboxCura AI Assistant view with automatic failover.
    Cached with 15-minute TTL to provide instant responses on repeated questions.
    """
    normalized_q = req.question.strip().lower()
    cache_key = f"chat:{normalized_q}"

    # Return cached response if available (0ms latency, 0 tokens)
    cached_answer = assistant_cache.get(cache_key)
    if cached_answer is not None:
        return {"status": "success", "answer": cached_answer, "cached": True}

    try:
        data = get_dashboard_summary()
        latest = data.get("latest", {}) or {}
        recent = data.get("recent_emails", []) or []
        summary_text = latest.get("summary_markdown", "")

        prompt = f"""
You are the InboxCura personal email executive assistant.
Answer the user's question concisely and accurately based on their current email inbox and today's summary.

User Question: {req.question}

Latest Daily Briefing Summary:
{summary_text or 'No summary generated yet.'}

Recent Inbound Emails in Inbox:
{json.dumps(recent[:20], indent=2, default=str)}

Guidelines:
- Format your response in clean HTML (using <b>, <br>, <i>, <ul>, <li>) suitable for inline chat bubbles.
- Be direct, executive, and actionable.
- If asked to draft a reply, provide a polished, ready-to-send draft.
"""
        answer, key_used = execute_with_failover(prompt, is_json=False)

        # Store in cache for 15 minutes
        assistant_cache.set(cache_key, answer, ttl=900)
        return {
            "status": "success",
            "answer": answer,
            "cached": False,
            "provider_used": key_used.get("provider"),
            "model_used": key_used.get("model_name")
        }
    except Exception as e:
        logger.error(f"AI Assistant query failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Assistant failed: {str(e)}"
        )

@router.post("/summary/trigger")
def trigger_summary_job(req: SummaryTriggerRequest):
    """
    Manually triggers the end-to-end Map-Reduce summarization pipeline.
    Respects selected date range and executes all batches with failover.
    """
    try:
        result = execute_consolidated_pipeline(range_config=req.range, force_all=req.force_all)
        return result
    except Exception as e:
        logger.exception("Manual summary execution failed:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Summary execution failed: {str(e)}"
        )

@router.get("/summary/status")
def get_summary_status():
    """Retrieve recent summary job records and execution statuses."""
    jobs = get_latest_summary_jobs(limit=10)
    for j in jobs:
        j["batches"] = get_job_batches(j["id"])
    return {"status": "success", "jobs": jobs}

@router.post("/sync-now")
def manual_sync():
    """Triggered directly by 'Sync Live Mail' button in the dashboard UI."""
    try:
        result = execute_consolidated_pipeline(force_all=False)
        return result
    except Exception as e:
        logger.exception("Manual sync failed:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync execution failed: {str(e)}"
        )

@cron_router.post("/cron-trigger")
def cron_trigger(authorization: str = Header(None)):
    """Target webhook endpoint for cron-job.org or external cloud schedulers."""
    # Fail CLOSED when no secret is configured: previously an empty
    # CRON_SECRET made the expected header "Bearer ", and the plain `!=`
    # comparison was also not timing-safe (string comparison short-circuits
    # on the first differing byte, leaking the secret byte-by-byte).
    if not settings.CRON_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cron trigger is not configured (CRON_SECRET is unset)"
        )
    expected_header = f"Bearer {settings.CRON_SECRET}"
    if not authorization or not hmac.compare_digest(authorization, expected_header):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Cron Authorization Header"
        )

    try:
        result = execute_consolidated_pipeline(force_all=False)
        return result
    except Exception as e:
        logger.exception("Cron sync failed:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cron pipeline error: {str(e)}"
        )
