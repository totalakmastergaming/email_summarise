import logging
from datetime import datetime
from typing import Optional, Any
from fastapi import APIRouter, Header, HTTPException, status, Path
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
    update_gmail_message_state,
    sync_inbox_labels_from_gmail
)
from app.services.llm_provider import (
    execute_with_failover,
    call_provider,
    LLMProviderError
)
from app.services.pipeline_service import execute_consolidated_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["API"])

# ================= Request / Response Schemas =================

class ChatRequest(BaseModel):
    question: str

class MailStateRequest(BaseModel):
    is_unread: Optional[bool] = None
    is_starred: Optional[bool] = None
    is_trash: Optional[bool] = None

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
def get_inbox_mails():
    """
    Returns processed emails formatted for the InboxCura UI.
    Cached in RAM with 60s TTL for instant page loads.
    """
    cached = dashboard_cache.get("mails")
    if cached is not None:
        cached_copy = dict(cached)
        cached_copy["cached"] = True
        return cached_copy

    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT * FROM processed_emails ORDER BY id DESC LIMIT 60")
            rows = cur.fetchall()

        mails = []
        for r in rows:
            created = r.get("created_at")
            time_str = created.strftime("%I:%M %p") if created else "Today"
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
                "today": True,
                "unread": is_unr,
                "urgent": is_urgent,
                "starred": is_sta,
                "needsReply": is_urgent,
                "tag": "Alert" if is_urgent else "Inbound",
                "cat": "primary"
            })

        result = {"status": "success", "mails": mails, "cached": False}
        dashboard_cache.set("mails", result, ttl=60)
        return result
    except Exception as e:
        logger.error(f"Error retrieving mails for InboxCura: {e}")
        return {"status": "error", "mails": []}

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
            return {"status": "success", "synced_gmail": False}

        msg_id = email_record["message_id"]

        # Update TiDB
        update_processed_email_state(
            message_id_or_pk=msg_id,
            is_unread=req.is_unread,
            is_starred=req.is_starred
        )

        # Clear mail cache so UI immediately gets fresh state
        dashboard_cache.delete("mails")

        # Sync back to Gmail
        gmail_ok = update_gmail_message_state(
            message_id=msg_id,
            is_unread=req.is_unread,
            is_starred=req.is_starred,
            is_trash=req.is_trash
        )

        return {"status": "success", "synced_gmail": gmail_ok, "message_id": msg_id}
    except Exception as e:
        logger.error(f"Error syncing mail state: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"State synchronization error: {str(e)}"
        )

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
                is_starred=state_item["is_starred"]
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
    with get_db_cursor() as cur:
        cur.execute("SELECT * FROM api_keys WHERE id = %s", (key_id,))
        key = cur.fetchone()
    
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
{__import__('json').dumps(recent[:20], indent=2, default=str)}

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

@router.post("/cron-trigger")
def cron_trigger(authorization: str = Header(None)):
    """Target webhook endpoint for cron-job.org or external cloud schedulers."""
    expected_header = f"Bearer {settings.CRON_SECRET}"
    if authorization != expected_header:
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
