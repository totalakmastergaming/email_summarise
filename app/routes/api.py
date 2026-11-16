import hmac
import json
import logging
from datetime import datetime
from typing import Optional, Any
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status, Path
from starlette.datastructures import UploadFile
from pydantic import BaseModel, field_validator

from app.config import settings
from app.database import (
    get_dashboard_summary,
    get_unprocessed_message_ids,
    save_processed_emails,
    save_daily_report,
    get_db_cursor,
    update_processed_email_state,
    get_email_by_id_or_msg_id,
    get_thread_messages,
    list_threads,
    get_sync_checkpoint,
    get_folder_counts,
    create_draft,
    get_draft,
    list_drafts,
    update_draft,
    delete_draft,
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
from app.services.history_sync import run_incremental_sync, maybe_trigger_background_sync
from app.cache import dashboard_cache, assistant_cache, invalidate_inbox_caches
from app.services.gmail_service import (
    MAX_ATTACHMENT_BYTES,
    fetch_recent_emails,
    get_email_full,
    get_message_attachments,
    get_attachment_bytes,
    build_forward_draft,
    fetch_forward_attachments,
    update_gmail_message_state,
    batch_update_message_states,
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
    cc: list[str] = []
    bcc: list[str] = []


class ThreadReplyRequest(BaseModel):
    body: str
    reply_all: bool = False
    cc: list[str] = []
    bcc: list[str] = []


class SendRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: list[str] = []
    bcc: list[str] = []
    # Forward: re-attach originals from this message. When
    # forward_attachment_ids is empty, ALL of the original's
    # attachments are re-attached (within the 10 MB total cap).
    forward_message_id: Optional[str] = None
    forward_attachment_ids: list[str] = []


class DraftCreateRequest(BaseModel):
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    subject: str = ""
    body: str = ""
    thread_id: Optional[str] = None
    message_id: Optional[str] = None

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _coerce_address_list(cls, v: Any) -> Any:
        # HOTFIX 2026-11-16: the compose UI historically sent `to` as a
        # plain string ("a@b.com, b@c.com"); accept that shape instead
        # of 422ing. A string is split on commas, like parseAddressList.
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v


class DraftUpdateRequest(BaseModel):
    to: Optional[list[str]] = None
    cc: Optional[list[str]] = None
    bcc: Optional[list[str]] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    thread_id: Optional[str] = None
    message_id: Optional[str] = None

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _coerce_address_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

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


# ================= Recipient validation (Phase 2: CC/BCC) =================
# Loose on purpose: non-empty strings containing "@" with a non-empty
# local part and domain. Gmail itself rejects truly undeliverable
# addresses at send time (502), so this only catches typos early.
MAX_RECIPIENTS_TOTAL = 20


def _validate_addresses(values: Any, field: str) -> list[str]:
    """Clean + loosely validate one address list; 400 on a bad entry."""
    out: list[str] = []
    for raw in (values or []):
        addr = str(raw).strip()
        if not addr:
            continue
        local, sep, domain = addr.partition("@")
        if not sep or not local or not domain:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {field} address: {addr!r} (expected name@example.com).",
            )
        out.append(addr)
    return out


def _check_recipient_cap(to_count: int, cc: list[str], bcc: list[str]) -> None:
    """Total recipients across To+Cc+Bcc must stay within the cap."""
    if to_count + len(cc) + len(bcc) > MAX_RECIPIENTS_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_RECIPIENTS_TOTAL} recipients total across To, Cc and Bcc.",
        )


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

def _mail_obj_from_row(r: dict) -> dict:
    """One processed_emails row -> the mail object the UI consumes.

    Shared by the paginated inbox list and the thread view so both
    agree on every field. Extra keys the list did not used to carry
    (date, received_at, archived, trashed) are additive — the UI
    ignores unknown fields.
    """
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

    received_iso = None
    if received_at:
        try:
            received_iso = received_at.isoformat() if hasattr(received_at, "isoformat") else str(received_at)
        except Exception:
            received_iso = None

    return {
        "id": 1000 + r["id"],
        "message_id": r.get("message_id"),
        "thread_id": r.get("thread_id") or r.get("message_id"),
        "sender": name_part or email_part,
        "email": email_part,
        "subject": subject_text,
        "snippet": snippet,
        "body": [snippet] if snippet else ["No further content preview."],
        "summary": snippet[:140] if snippet else "",
        "time": time_str,
        "date": received_iso,
        "received_at": received_iso,
        "today": _is_today(received_at),
        "unread": is_unr,
        "urgent": is_urgent,
        "starred": is_sta,
        "archived": bool(r.get("is_archived", False)),
        "trashed": bool(r.get("is_trashed", False)),
        "needsReply": _needs_reply(is_unr, category, sender_str, subject_text),
        "tag": "Alert" if is_urgent else "Inbound",
        "cat": category,
        "labels": r.get("labels", "")
    }


def _draft_obj_from_row(r: dict) -> dict:
    """One drafts-folder row (server draft) -> the mail-like object the
    UI consumes. Draft rows carry ``is_draft`` + ``draft_id`` from the
    list_threads() union; the UI id is the string ``draft-<id>`` so it
    can never collide with a mail id (1000 + db id)."""
    updated_at = r.get("updated_at") or r.get("created_at")
    body_text = r.get("body_text") or ""
    received_iso = None
    if updated_at:
        try:
            received_iso = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
        except Exception:
            received_iso = None

    def _split(raw: Any) -> list[str]:
        return [p.strip() for p in str(raw or "").split(",") if p.strip()]

    return {
        "id": f"draft-{r['draft_id']}",
        "draft_id": int(r["draft_id"]),
        "is_draft": True,
        "message_id": None,
        "thread_id": r.get("thread_id") or f"draft-{r['draft_id']}",
        "sender": "Me",
        "email": settings.MY_EMAIL or "",
        "to": _split(r.get("to_addrs")),
        "cc": _split(r.get("cc_addrs")),
        "bcc": _split(r.get("bcc_addrs")),
        "subject": r.get("subject") or "(No subject)",
        "snippet": r.get("snippet") or "",
        "body": [body_text] if body_text else [""],
        "summary": body_text[:140],
        "time": _display_time(updated_at, r.get("created_at")),
        "date": received_iso,
        "received_at": received_iso,
        "today": _is_today(updated_at),
        "unread": False,
        "urgent": False,
        "starred": False,
        "archived": False,
        "trashed": False,
        "needsReply": False,
        "tag": "Draft",
        "cat": "primary",
        "labels": "DRAFT",
        "message_count": 1,
        "unread_count": 0,
    }


@router.get("/mails")
@router.get("/emails")
def get_inbox_mails(q: Optional[str] = None, folder: str = "inbox",
                    cat: Optional[str] = None, limit: int = 30,
                    cursor: Optional[str] = None):
    """
    Threaded, keyset-paginated inbox list, served from the TiDB mirror.

    WHY THE MIRROR, NOT LIVE GMAIL: listing a page live from Gmail
    would cost 30+ Gmail calls per page (one list + one metadata fetch
    per message) and still could not group messages into threads
    correctly, because Gmail's list returns flat messages. The History
    sync (see app/services/history_sync.py) keeps the mirror current,
    so the list reads one cheap SQL query instead. The cursor is an
    opaque, validated keyset token (last seen row id + the query it
    belongs to) — the same pattern as Gmail's own pageToken, with NO
    SQL OFFSET anywhere. A background incremental sync is kicked off
    (throttled, never blocking) so browsing keeps the mirror fresh.
    """
    # Fire-and-forget freshness: throttled to once per 120s, never
    # blocks this response.
    maybe_trigger_background_sync()
    try:
        rows, next_cursor, has_more = list_threads(
            folder=folder, cat=cat, q=q, limit=limit, cursor=cursor
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error retrieving mails for InboxCura: {e}")
        return {"status": "error", "mails": [], "next_cursor": None, "has_more": False,
                "folder": folder, "limit": limit}

    mails = []
    for r in rows:
        if r.get("is_draft"):
            # Server-draft rows from the drafts-folder union already
            # carry message_count/unread_count (1/0); mail rows get
            # theirs from the thread aggregates below.
            mails.append(_draft_obj_from_row(r))
            continue
        mail = _mail_obj_from_row(r)
        mail["message_count"] = int(r.get("message_count") or 1)
        mail["unread_count"] = int(r.get("unread_count") or 0)
        mails.append(mail)

    return {
        "status": "success",
        "mails": mails,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "folder": folder,
        "limit": max(1, min(100, int(limit or 30))),
    }


@router.get("/folders")
def list_folders():
    """Per-folder thread counts (sidebar badges).

    Computed with the exact same label predicates as the folder lists,
    so a badge always equals the number of rows its folder shows.
    Not cached — one cheap aggregate query per call.
    """
    try:
        counts = get_folder_counts()
    except Exception as e:
        logger.error(f"Error computing folder counts: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not compute folder counts: {str(e)}"
        )
    return {"status": "success", "folders": counts}

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
        dashboard_cache.delete("summary")
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
            "attachments": full.get("attachments", []),
            "cat": _category_from_labels(full.get("label_ids", [])),
            "unread": bool(email_record.get("is_unread", True)),
            "starred": bool(email_record.get("is_starred", False)),
            "archived": bool(email_record.get("is_archived", False)),
            "trashed": bool(email_record.get("is_trashed", False)),
        },
    }


@router.get("/mails/{mail_id}/attachments/{attachment_id}")
def download_attachment(mail_id: str, attachment_id: str):
    """Download one attachment's bytes, proxied from Gmail.

    No local storage: bytes stream straight from Gmail to the client.
    Refuses with 413 when the attachment exceeds the 10 MB cap, 404
    when the mail or the attachment id is unknown. Content-Disposition
    makes the browser save it under the original filename.
    """
    email_record = get_email_by_id_or_msg_id(mail_id)
    if not email_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")
    try:
        data, filename, mime_type = get_attachment_bytes(
            email_record["message_id"], attachment_id
        )
    except ValueError as e:
        detail = str(e)
        if "over the 10 MB" in detail:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=detail,
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    except Exception as e:
        logger.error(f"Error downloading attachment {attachment_id} for {mail_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not download the attachment from Gmail: {str(e)}"
        )
    safe_filename = (filename or "attachment").replace('"', "'").replace("\r", "").replace("\n", "")
    return Response(
        content=data,
        media_type=mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


@router.get("/mails/{mail_id}/forward")
def get_forward_draft(mail_id: str):
    """Server-side forward draft: quoted original + attachments.

    The old forward was purely client-side prefill from the cached
    plain-text body, so HTML mail and attachments were lost. This
    returns the prefill (subject/body) plus the original's attachment
    list, which ones were fetched within the 10 MB total cap
    (``attached_ids``) and which were skipped and why (``skipped``).
    The UI sends it via POST /api/send with ``forward_message_id`` +
    ``forward_attachment_ids`` so the server re-attaches the fetched
    originals — nothing is dropped silently.
    """
    email_record = get_email_by_id_or_msg_id(mail_id)
    if not email_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")
    try:
        draft = build_forward_draft(email_record["message_id"])
    except Exception as e:
        logger.error(f"Error building forward draft for {mail_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not build the forward draft from Gmail: {str(e)}"
        )
    return {"status": "success", "draft": draft}


# ================= Thread Endpoints (Phase 1) =================

@router.get("/threads/{thread_id}")
def get_thread(thread_id: str):
    """One thread, messages chronological.

    Bodies are NOT stored in TiDB, so only the LATEST message gets its
    full body fetched from Gmail on demand; every other message carries
    has_body=false. A Gmail failure for that one body fetch degrades
    the response (empty body + body_error) instead of failing the
    whole thread.
    """
    rows = get_thread_messages(thread_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found.")

    canonical_id = rows[-1].get("thread_id") or rows[-1].get("message_id")
    messages = []
    body_error = None
    for idx, r in enumerate(rows):
        m = _mail_obj_from_row(r)
        m["has_body"] = False
        if idx == len(rows) - 1:
            try:
                full = get_email_full(r["message_id"])
                m["body_text"] = full.get("body_text", "")
                m["body_html"] = full.get("body_html", "")
                m["attachments"] = full.get("attachments", [])
                m["has_body"] = True
            except Exception as e:
                logger.error(f"Error fetching full body for latest message in thread {thread_id}: {e}")
                m["body_text"] = ""
                m["body_html"] = ""
                m["attachments"] = []
                body_error = str(e)
        messages.append(m)

    result: dict = {
        "status": "success",
        "thread": {"thread_id": canonical_id, "messages": messages},
    }
    if body_error:
        result["body_error"] = body_error
    return result


def _local_archived_flag(req) -> Optional[bool]:
    """Local is_archived mirror for a state request.

    Spam is stored as archived locally (Gmail removes spam from the
    inbox); un-spam re-adds INBOX, so it must clear the local flag
    too — otherwise a restored mail keeps archived=True until the
    next label sync. An explicit is_archived always wins.
    """
    if req.is_archived is not None:
        return req.is_archived
    if req.is_spam is True:
        return True
    if req.is_spam is False:
        return False
    return None


@router.post("/threads/{thread_id}/state")
def update_thread_state(thread_id: str, req: MailStateRequest):
    """Apply a state change to a whole thread, with Gmail-correct
    semantics (Gmail stars are per-message; read/archive/trash/spam
    apply to every message in the thread).

    - mark read/unread, archive, trash, spam: ALL messages, ONE
      batchModify call.
    - star: LATEST message only.
    - unstar: ALL starred messages in the thread.
    Local rows are updated only for the parts Gmail accepted.
    """
    rows = get_thread_messages(thread_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found.")
    if all(v is None for v in (req.is_unread, req.is_starred, req.is_trash, req.is_archived, req.is_spam)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No state change provided.")

    all_ids = [r["message_id"] for r in rows]
    latest_id = rows[-1]["message_id"]
    synced = True
    updated_ids: set = set()

    # Part 1: thread-wide flags, one batchModify for all messages.
    if any(v is not None for v in (req.is_unread, req.is_trash, req.is_archived, req.is_spam)):
        ok = batch_update_message_states(
            all_ids,
            is_unread=req.is_unread,
            is_trash=req.is_trash,
            is_archived=req.is_archived,
            is_spam=req.is_spam,
        )
        synced = synced and ok
        if ok:
            archived = _local_archived_flag(req)
            for msg_id in all_ids:
                update_processed_email_state(
                    message_id_or_pk=msg_id,
                    is_unread=req.is_unread,
                    is_archived=archived,
                    is_trashed=req.is_trash,
                )
            updated_ids.update(all_ids)

    # Part 2: stars are per-message in Gmail.
    if req.is_starred is True:
        ok = batch_update_message_states([latest_id], is_starred=True)
        synced = synced and ok
        if ok:
            update_processed_email_state(message_id_or_pk=latest_id, is_starred=True)
            updated_ids.add(latest_id)
    elif req.is_starred is False:
        starred_ids = [r["message_id"] for r in rows if r.get("is_starred")]
        if starred_ids:
            ok = batch_update_message_states(starred_ids, is_starred=False)
            synced = synced and ok
            if ok:
                for msg_id in starred_ids:
                    update_processed_email_state(message_id_or_pk=msg_id, is_starred=False)
                updated_ids.update(starred_ids)

    dashboard_cache.delete("summary")
    return {"status": "success", "synced_gmail": synced, "updated_count": len(updated_ids)}


# ================= JSON-or-FormData parsing for send/reply (Phase 2) ====
# The UI sends JSON when there are no files and multipart FormData when
# attachments are present (same field names in both). A Pydantic model
# parameter only accepts JSON, so a FormData post would 422 — these
# helpers accept either shape and return one normalized dict. Requires
# python-multipart (see requirements.txt) for request.form().

async def _read_upload_attachments(form) -> list[tuple[str, str, bytes]]:
    """Uploaded 'attachments' files from a multipart form, capped.

    Per-file and total caps are both MAX_ATTACHMENT_BYTES (10 MB), the
    same cap as the download proxy and the forward flow. Over-cap is a
    413, never a silent truncation.

    NOTE: UploadFile here is starlette's class (imported from
    starlette.datastructures), NOT fastapi's — Starlette's form parser
    returns starlette UploadFile instances, and fastapi's class is a
    subclass of it, so isinstance(x, FastAPIUploadFile) is False for
    every real upload and the files were silently dropped. Checking
    against the base class accepts both.
    """
    files = form.getlist("attachments")
    attachments: list[tuple[str, str, bytes]] = []
    total = 0
    for f in files:
        if not isinstance(f, UploadFile):
            continue
        data = await f.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Attachment '{f.filename or 'file'}' is over the 10 MB limit.",
            )
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Attachments total over the 10 MB limit.",
            )
        attachments.append((f.filename or "attachment",
                            f.content_type or "application/octet-stream", data))
    return attachments


def _form_str(form, name: str) -> str:
    v = form.get(name)
    return str(v).strip() if v is not None else ""


def _form_list(form, name: str) -> list[str]:
    return [str(v).strip() for v in form.getlist(name) if str(v).strip()]


async def _parse_json_body(request: Request):
    """request.json() with a 400 (not a 500) on malformed JSON."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Request body is not valid JSON.")


async def _parse_send_request(request: Request) -> dict:
    """POST /mails/send body as JSON or multipart FormData, normalized."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        return {
            "to": _form_str(form, "to"),
            "subject": _form_str(form, "subject"),
            "body": _form_str(form, "body"),
            "cc": _form_list(form, "cc"),
            "bcc": _form_list(form, "bcc"),
            "forward_message_id": _form_str(form, "forward_message_id") or None,
            "forward_attachment_ids": _form_list(form, "forward_attachment_ids"),
            "attachments": await _read_upload_attachments(form),
        }
    req = SendRequest.model_validate(await _parse_json_body(request))
    return {
        "to": req.to, "subject": req.subject, "body": req.body,
        "cc": req.cc or [], "bcc": req.bcc or [],
        "forward_message_id": req.forward_message_id,
        "forward_attachment_ids": req.forward_attachment_ids or [],
        "attachments": [],
    }


async def _parse_reply_request(request: Request, allow_reply_all: bool) -> dict:
    """Reply body as JSON or multipart FormData, normalized."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        return {
            "body": _form_str(form, "body"),
            "reply_all": _form_str(form, "reply_all").lower() in ("true", "1", "yes") if allow_reply_all else False,
            "cc": _form_list(form, "cc"),
            "bcc": _form_list(form, "bcc"),
            "attachments": await _read_upload_attachments(form),
        }
    payload = await _parse_json_body(request)
    if allow_reply_all:
        req = ThreadReplyRequest.model_validate(payload)
        reply_all = bool(req.reply_all)
    else:
        req = ReplyRequest.model_validate(payload)
        reply_all = False
    return {
        "body": req.body, "reply_all": reply_all,
        "cc": req.cc or [], "bcc": req.bcc or [], "attachments": [],
    }


@router.post("/threads/{thread_id}/reply")
async def reply_to_thread(thread_id: str, request: Request):
    """Reply in-thread on the thread's LATEST message.

    reply_all keeps the original Cc and adds any new Cc supplied;
    Bcc is accepted too (Gmail strips it from the sent message's
    visible headers — see gmail_service._build_outgoing_message).
    Accepts JSON or multipart FormData (attachments as 'attachments'
    files, same 10 MB caps as compose).
    """
    data = await _parse_reply_request(request, allow_reply_all=True)
    body = (data["body"] or "").strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body cannot be empty.")
    if len(body) > 100000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body is too long (max 100,000 characters).")
    cc_addrs = _validate_addresses(data["cc"], "Cc")
    bcc_addrs = _validate_addresses(data["bcc"], "Bcc")
    _check_recipient_cap(1, cc_addrs, bcc_addrs)

    rows = get_thread_messages(thread_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found.")
    latest = rows[-1]

    try:
        sent_id = reply_to_email(latest["message_id"], body, reply_all=data["reply_all"],
                                 cc_addrs=cc_addrs, bcc_addrs=bcc_addrs,
                                 attachments=data["attachments"] or None)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error sending reply in thread {thread_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the reply via Gmail: {str(e)}"
        )
    if not sent_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gmail did not return a sent message id.")
    return {
        "status": "success",
        "sent_message_id": sent_id,
        "thread_id": latest.get("thread_id") or latest.get("message_id"),
    }


@router.post("/mails/{mail_id}/reply")
async def reply_to_mail(mail_id: str, request: Request):
    """Reply in-thread via the Gmail API (not a mailto: draft).

    Accepts JSON or multipart FormData (attachments as 'attachments'
    files, same 10 MB caps as compose).
    """
    data = await _parse_reply_request(request, allow_reply_all=False)
    body = (data["body"] or "").strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body cannot be empty.")
    if len(body) > 100000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reply body is too long (max 100,000 characters).")
    cc_addrs = _validate_addresses(data["cc"], "Cc")
    bcc_addrs = _validate_addresses(data["bcc"], "Bcc")
    _check_recipient_cap(1, cc_addrs, bcc_addrs)

    email_record = get_email_by_id_or_msg_id(mail_id)
    if not email_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mail not found.")

    try:
        sent_id = reply_to_email(email_record["message_id"], body,
                                 cc_addrs=cc_addrs, bcc_addrs=bcc_addrs,
                                 attachments=data["attachments"] or None)
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
async def compose_and_send(request: Request):
    """Compose + send a new email via the Gmail API (Cc/Bcc supported).

    Accepts JSON or multipart FormData (uploaded files as 'attachments'
    fields, per-file and total caps 10 MB — a 413, never silent).
    Bcc recipients receive the mail but Gmail strips the Bcc header
    from the stored/sent copy, so their addresses never appear in any
    visible header. When ``forward_message_id`` is given, the
    original's attachments are re-attached within the 10 MB total cap;
    the response reports which were attached and which were skipped
    (with reasons) — nothing is dropped silently.
    """
    data = await _parse_send_request(request)
    to_address = (data["to"] or "").strip()
    subject = (data["subject"] or "").strip()
    body = (data["body"] or "").strip()
    if "@" not in to_address or "." not in to_address.split("@")[-1]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid recipient email address is required.")
    if not subject:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Subject cannot be empty.")
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body cannot be empty.")
    if len(body) > 100000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body is too long (max 100,000 characters).")
    cc_addrs = _validate_addresses(data["cc"], "Cc")
    bcc_addrs = _validate_addresses(data["bcc"], "Bcc")
    _check_recipient_cap(1, cc_addrs, bcc_addrs)
    forward_id = (data["forward_message_id"] or "").strip() or None
    forward_aids = [str(a).strip() for a in (data["forward_attachment_ids"] or []) if str(a).strip()]
    if forward_aids and not forward_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="forward_attachment_ids requires forward_message_id.",
        )

    attachments: list[tuple[str, str, bytes]] = list(data["attachments"] or [])
    skipped_forward: list[dict] = []
    if forward_id:
        try:
            if not forward_aids:
                forward_aids = [a["attachmentId"] for a in get_message_attachments(forward_id)]
            fwd_attachments, skipped_forward = fetch_forward_attachments(forward_id, forward_aids)
            attachments.extend(fwd_attachments)
        except Exception as e:
            logger.error(f"Error fetching forward attachments for {forward_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not fetch the original's attachments from Gmail: {str(e)}"
            )

    try:
        sent_id = send_email(to_address, subject, body,
                             cc_addrs=cc_addrs, bcc_addrs=bcc_addrs,
                             attachments=attachments or None)
    except Exception as e:
        logger.error(f"Error sending email to {to_address}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the email via Gmail: {str(e)}"
        )
    if not sent_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gmail did not return a sent message id.")
    result: dict = {"status": "success", "sent_message_id": sent_id}
    if forward_id:
        result["forwarded_attachments"] = [f for f, _, _ in fwd_attachments]
        result["skipped_forward_attachments"] = skipped_forward
        # Alias the UI reads (d.skipped_attachments || d.skipped).
        result["skipped_attachments"] = skipped_forward
    return result


# ================= Drafts (server-side autosave, Phase 2) =================
# Drafts live in TiDB (see app/database.py), so they survive refresh
# and appear in folder=drafts. A reply draft (message_id set) is sent
# in-thread via reply_to_email; a fresh draft is sent via send_email.
# The draft is deleted ONLY after Gmail confirms the send.

@router.post("/drafts")
def create_draft_endpoint(req: DraftCreateRequest):
    """Autosave a new draft (compose or reply).

    HOTFIX 2026-11-16: addresses are NOT validated here. Autosave fires
    while the user is still typing, so a half-typed address (no "@"
    yet) must not fail the save with a 400 — that was one of the two
    live autosave failures. Addresses are validated at send time
    (send_draft_endpoint / compose send), where they have to be final.
    """
    to_addrs = list(req.to or [])
    cc_addrs = list(req.cc or [])
    bcc_addrs = list(req.bcc or [])
    subject = (req.subject or "").strip()
    body = req.body or ""
    if not to_addrs and not subject and not body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Draft is empty: provide a recipient, a subject, or a body.",
        )
    if len(body) > 100000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft body is too long (max 100,000 characters).")
    try:
        draft = create_draft(to_addrs, cc_addrs, bcc_addrs, subject, body,
                             thread_id=(req.thread_id or "").strip() or None,
                             message_id=(req.message_id or "").strip() or None)
    except Exception as e:
        logger.error(f"Error creating draft: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not save the draft: {str(e)}"
        )
    return {"status": "success", "draft": draft}


@router.get("/drafts")
def list_drafts_endpoint(limit: int = 30, cursor: Optional[str] = None):
    """Server drafts, newest first (keyset-paginated)."""
    try:
        drafts, next_cursor, has_more = list_drafts(limit=limit, cursor=cursor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing drafts: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not list drafts: {str(e)}"
        )
    return {"status": "success", "drafts": drafts,
            "next_cursor": next_cursor, "has_more": has_more}


@router.get("/drafts/{draft_id}")
def get_draft_endpoint(draft_id: int):
    """One draft by id."""
    try:
        draft = get_draft(draft_id)
    except Exception as e:
        logger.error(f"Error fetching draft {draft_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch the draft: {str(e)}"
        )
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    return {"status": "success", "draft": draft}


@router.put("/drafts/{draft_id}")
def update_draft_endpoint(draft_id: int, req: DraftUpdateRequest):
    """Autosave update of a draft (partial: only given fields change).

    Addresses are not validated here (see create_draft_endpoint):
    autosave must never fail on a half-typed address.
    """
    updates: dict = {}
    if req.to is not None:
        updates["to"] = list(req.to)
    if req.cc is not None:
        updates["cc"] = list(req.cc)
    if req.bcc is not None:
        updates["bcc"] = list(req.bcc)
    if req.subject is not None:
        updates["subject"] = req.subject.strip()
    if req.body is not None:
        if len(req.body) > 100000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft body is too long (max 100,000 characters).")
        updates["body"] = req.body
    if req.thread_id is not None:
        updates["thread_id"] = (req.thread_id or "").strip() or None
    if req.message_id is not None:
        updates["message_id"] = (req.message_id or "").strip() or None
    try:
        draft = update_draft(draft_id, updates)
    except Exception as e:
        logger.error(f"Error updating draft {draft_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not update the draft: {str(e)}"
        )
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    return {"status": "success", "draft": draft}


@router.delete("/drafts/{draft_id}")
def delete_draft_endpoint(draft_id: int):
    """Discard a draft."""
    try:
        ok = delete_draft(draft_id)
    except Exception as e:
        logger.error(f"Error deleting draft {draft_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not delete the draft: {str(e)}"
        )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    return {"status": "success", "message": "Draft deleted."}


@router.post("/drafts/{draft_id}/send")
def send_draft_endpoint(draft_id: int):
    """Send a draft via Gmail, then delete it (only on success).

    Reply drafts (message_id set) go in-thread via reply_to_email —
    the draft's To list is NOT used there, because a reply's
    recipients come from the original message (reply_to_email derives
    them); the draft's Cc/Bcc are still applied on top. Fresh drafts
    need at least one To address and a subject.
    """
    try:
        draft = get_draft(draft_id)
    except Exception as e:
        logger.error(f"Error fetching draft {draft_id} for send: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not fetch the draft: {str(e)}"
        )
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    body = (draft.get("body") or "").strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft body is empty; nothing to send.")
    # Send time is where addresses must be final: validate here, not
    # at autosave (see create_draft_endpoint). A draft saved with a
    # half-typed address can only be sent once it is fixed.
    cc_addrs = _validate_addresses(draft.get("cc") or [], "Cc")
    bcc_addrs = _validate_addresses(draft.get("bcc") or [], "Bcc")
    try:
        if draft.get("message_id"):
            _check_recipient_cap(0, cc_addrs, bcc_addrs)
            sent_id = reply_to_email(draft["message_id"], body,
                                     cc_addrs=cc_addrs, bcc_addrs=bcc_addrs)
            thread_id = draft.get("thread_id")
        else:
            to_addrs = _validate_addresses(draft.get("to") or [], "To")
            _check_recipient_cap(len(to_addrs), cc_addrs, bcc_addrs)
            if not to_addrs:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft has no recipient; add a To address before sending.")
            subject = (draft.get("subject") or "").strip()
            if not subject:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Draft has no subject.")
            sent_id = send_email(to_addrs, subject, body,
                                 cc_addrs=cc_addrs, bcc_addrs=bcc_addrs)
            thread_id = None
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error sending draft {draft_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send the draft via Gmail: {str(e)}"
        )
    if not sent_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gmail did not return a sent message id.")
    # Delete only after Gmail confirmed the send.
    try:
        delete_draft(draft_id)
    except Exception as e:
        logger.error(f"Draft {draft_id} sent ({sent_id}) but could not be deleted: {e}")
    result: dict = {"status": "success", "sent_message_id": sent_id}
    if thread_id:
        result["thread_id"] = thread_id
    return result


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
            is_archived=_local_archived_flag(req),
            is_trashed=req.is_trash
        )

        # Clear mail cache so UI immediately gets fresh state
        dashboard_cache.delete("summary")

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
                is_archived=_local_archived_flag(req),
                is_trashed=req.is_trash,
            )
        dashboard_cache.delete("summary")

    return {
        "status": "success",
        "synced_gmail": gmail_ok,
        "updated_count": len(resolved) if gmail_ok else 0,
        "skipped_ids": skipped,
    }

@router.post("/sync/incremental")
def sync_incremental():
    """Run one bounded Gmail History incremental sync (see
    app/services/history_sync.py). Returns the sync result as-is:
    {"status": "success"|"busy"|"error", "mode": ..., ...}.
    """
    return run_incremental_sync()


@router.get("/sync/status")
def get_sync_status():
    """Current History sync checkpoint (history id, last sync time, mode),
    or checkpoint=None when no sync has ever run."""
    checkpoint = get_sync_checkpoint()
    return {"status": "success", "checkpoint": checkpoint}


@router.post("/sync-inbox-state")
def sync_inbox_from_gmail():
    """
    Incremental inbox sync via the Gmail History API.

    Previously this polled the latest 50 rows one by one
    (sync_inbox_labels_from_gmail) — one Gmail call per message, and it
    could never see new mail, deletes, or label changes outside those
    50. Now it runs the bounded History sync and returns how many
    records changed (added + deleted + label changes).
    """
    try:
        result = run_incremental_sync()
    except Exception as e:
        logger.error(f"Error running incremental sync: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inbox sync error: {str(e)}"
        )
    if result.get("status") == "busy":
        return {"status": "busy", "updated_count": 0}
    if result.get("status") == "error":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inbox sync error: {result.get('error', 'unknown error')}"
        )
    processed = result.get("processed", {}) or {}
    updated_count = int(processed.get("added", 0)) + int(processed.get("deleted", 0)) + int(processed.get("label_changes", 0))
    if result.get("mode") == "initial" or result.get("mode") == "reconciled":
        updated_count = int(result.get("added", 0))
    dashboard_cache.delete("summary")
    return {"status": "success", "updated_count": updated_count, "mode": result.get("mode"), "has_more": result.get("has_more", False)}

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
