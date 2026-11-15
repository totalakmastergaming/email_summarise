import json
import logging
import threading
import time
import uuid
from datetime import date as date_type, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.database import (
    get_system_config,
    save_processed_emails,
    create_summary_job,
    update_summary_job,
    create_summary_batch,
    update_summary_batch,
    get_job_batches,
    save_daily_report,
    get_unprocessed_message_ids
)
from app.cache import invalidate_inbox_caches
from app.services.gmail_service import fetch_recent_emails, send_daily_digest_email
from app.services.llm_provider import (
    compute_dynamic_batches,
    execute_with_failover,
    ContextWindowExceededError,
    LLMProviderError,
    parse_json_response
)

logger = logging.getLogger(__name__)

# Single-flight guard: the pipeline can be triggered from three places at
# once (in-app scheduler, /api/cron-trigger, manual Sync/Summary buttons).
# Without this, two concurrent runs fetch the same mail, both summarise it,
# and the user gets TWO digest emails. A non-blocking lock makes the second
# caller get a clean "already running" answer instead of a duplicate run.
_pipeline_lock = threading.Lock()


def _today_in_schedule_tz() -> date_type:
    """'Today' in the user's configured schedule timezone.

    Previously ``datetime.utcnow().date()`` — a 18:00 IST run is 12:30 UTC,
    so late-evening IST runs could be archived under the wrong (UTC) date.
    """
    tz_name = "Asia/Kolkata"
    try:
        cfg = get_system_config("summary_schedule") or {}
        tz_name = cfg.get("timezone") or tz_name
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    return datetime.now(tz).date()

# Primary spam & promotional noise filter keywords
NOISE_KEYWORDS = [
    "unsubscribe", "promotions", "newsletter", "digest", "marketing",
    "special offer", "50% off", "flash sale", "no-reply", "noreply",
    "notification only", "do not reply", "terms of service", "privacy policy"
]

def filter_spam_and_noise(emails: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Separates actionable, critical correspondence from spam, automated blasts, and promo noise.
    Returns: (actionable_emails, noise_emails)
    """
    actionable = []
    noise = []

    for e in emails:
        # Check explicit Gmail spam tag
        if e.get("is_spam") or e.get("is_trash"):
            noise.append(e)
            continue

        subj = (e.get("subject") or "").lower()
        sender = (e.get("sender") or "").lower()
        snippet = (e.get("snippet") or "").lower()

        # If sender or subject strongly indicates automated noise.
        # NOISE_KEYWORDS (module constant) is the shared list — previously
        # it was defined but never used while two inline lists duplicated
        # parts of it. The inline promo list is kept for phrases the
        # constant does not cover ("% off", "black friday", ...).
        is_automated = any(k in sender for k in ["no-reply", "noreply", "digest", "notification", "updates@"])
        is_promo = any(k in subj or k in snippet for k in NOISE_KEYWORDS) or any(
            k in subj or k in snippet
            for k in ["sale", "discount", "% off", "black friday", "newsletter", "weekly digest"]
        )

        # Protect security alerts, verifications, or explicit user requests
        is_important = any(k in subj for k in ["urgent", "security alert", "action required", "deadline", "invoice", "payment", "interview", "invitation", "verify"])

        if (is_automated or is_promo) and not is_important:
            noise.append(e)
        else:
            actionable.append(e)

    logger.info(f"Email filtering: {len(actionable)} actionable emails prioritized, {len(noise)} noise/promotional emails grouped.")
    return actionable, noise

def process_batch_slice(batch_emails: list[dict], batch_idx: int, total_batches: int) -> dict:
    """
    Analyzes an individual batch slice independently (Map stage).
    Extracts key takeaways, urgent flags, and action items.
    """
    prompt = f"""
You are analyzing Batch {batch_idx + 1} of {total_batches} of an executive inbox.
Review the following {len(batch_emails)} emails carefully and extract structured takeaways.

Return ONLY a valid JSON object matching this schema:
{{
  "batch_summary": "Crisp 1-2 sentence overview of these emails.",
  "urgent_items": [
    {{
      "subject": "...",
      "sender": "...",
      "reason": "Why this is time-sensitive"
    }}
  ],
  "action_items": [
    {{
      "task": "Specific actionable task",
      "sender": "Person/Entity requesting it",
      "due": "Deadline or timeframe (e.g. 'Today', 'Friday 11:59 PM', 'ASAP')",
      "priority": "High" | "Medium" | "Low"
    }}
  ],
  "key_topics": ["List of distinct subject topics/threads discussed"]
}}

Emails in this batch:
{json.dumps(batch_emails, indent=2, default=str)}
"""
    try:
        response_text, key_used = execute_with_failover(prompt, is_json=True)
        parsed = parse_json_response(response_text)
        parsed["key_id_used"] = key_used.get("id")
        parsed["provider_used"] = key_used.get("provider")
        return parsed
    except ContextWindowExceededError:
        # Context window exceeded: split batch in half and process sub-batches.
        # Base case: a SINGLE email that still overflows cannot be split
        # further — previously mid = 0 produced process_batch_slice([])
        # and process_batch_slice([same email]) forever (infinite
        # recursion -> RecursionError). Fail loudly instead.
        if len(batch_emails) <= 1:
            logger.error(f"Batch {batch_idx + 1}: a single email exceeds the context window; cannot split further.")
            raise
        logger.warning(f"Batch {batch_idx + 1} exceeded context window. Splitting in half...")
        mid = len(batch_emails) // 2
        res1 = process_batch_slice(batch_emails[:mid], batch_idx, total_batches)
        res2 = process_batch_slice(batch_emails[mid:], batch_idx, total_batches)
        return {
            "batch_summary": f"{res1.get('batch_summary', '')} {res2.get('batch_summary', '')}".strip(),
            "urgent_items": res1.get("urgent_items", []) + res2.get("urgent_items", []),
            "action_items": res1.get("action_items", []) + res2.get("action_items", []),
            "key_topics": list(set(res1.get("key_topics", []) + res2.get("key_topics", []))),
            "key_id_used": res1.get("key_id_used"),
            "provider_used": res1.get("provider_used")
        }
    except Exception as e:
        logger.error(f"Failed to process batch {batch_idx + 1}: {e}")
        raise

def consolidate_all_batches(batch_results: list[dict], noise_emails: list[dict], total_email_count: int) -> dict:
    """
    Consolidates independent batch results into one cohesive executive briefing (Reduce stage).
    Deduplicates related topics, merges action lists, and structures the final output.
    """
    prompt = f"""
You are the Executive Inbox Consolidation Engine for InboxCura.
You have processed {total_email_count} total emails across {len(batch_results)} batches.
Consolidate these batch outputs into one unified, elegant, and actionable briefing.

Batch Outputs:
{json.dumps(batch_results, indent=2, default=str)}

Informational/Noise Emails Count: {len(noise_emails)}

Requirements:
1. "executive_summary": A high-impact 2-3 sentence overview of what matters in this inbox.
2. "urgent_count": Total number of genuine urgent items requiring action.
3. "action_items": Consolidated, deduplicated list of tasks with priority ('High', 'Medium', 'Low').
4. "categories": Categorized breakdown:
   - "urgent": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ]
   - "work_projects": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ]
   - "finance_billing": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ]
   - "updates_info": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ]
5. "email_markdown_report": A complete, beautifully formatted Markdown report with clean headings, bullet points, deadlines, and a clear executive layout ready for email delivery.

Return ONLY a valid JSON object matching the above schema.
"""
    response_text, key_used = execute_with_failover(prompt, is_json=True)
    try:
        parsed = parse_json_response(response_text)
        return parsed
    except Exception as e:
        logger.error(f"Consolidation JSON parse error: {e}\nRaw: {response_text}")
        return {
            "executive_summary": "Inbox analyzed across all processing batches.",
            "urgent_count": 0,
            "action_items": [],
            "categories": {},
            "email_markdown_report": response_text
        }

def execute_consolidated_pipeline(range_config: Optional[dict] = None, force_all: bool = False) -> dict:
    """
    Fault-tolerant, all-or-nothing pipeline orchestration:
    1. Fetch emails from Gmail respecting configured range.
    2. Synchronize states with TiDB database.
    3. Filter spam/promotional noise.
    4. Dynamically batch actionable emails.
    5. Track job and batches in TiDB.
    6. Process every batch with multi-key failover.
    7. All-or-nothing verification: abort delivery if any batch fails.
    8. Consolidate batch results into final briefing.
    9. Deliver final report via Gmail and archive in TiDB.
    """
    job_uuid = str(uuid.uuid4())
    # Single-flight: refuse a second concurrent run (scheduler + cron +
    # manual trigger can overlap). The lock is released in `finally`.
    if not _pipeline_lock.acquire(blocking=False):
        logger.warning("Pipeline already running; refusing concurrent run.")
        return {
            "status": "busy",
            "message": "A summary pipeline run is already in progress. Try again when it finishes.",
            "job_uuid": job_uuid,
        }
    try:
        return _execute_consolidated_pipeline_locked(job_uuid, range_config, force_all)
    finally:
        _pipeline_lock.release()


def _execute_consolidated_pipeline_locked(job_uuid: str, range_config: Optional[dict] = None, force_all: bool = False) -> dict:
    """
    Fault-tolerant, all-or-nothing pipeline orchestration (see
    execute_consolidated_pipeline docstring for the stage list).
    Runs under the _pipeline_lock single-flight guard.
    """
    logger.info(f"Starting consolidated summary pipeline (Job {job_uuid})...")

    # 1. Resolve date range configuration
    if not range_config:
        range_config = get_system_config("summary_range", {"preset": "last_1_day"})
    range_desc = f"{range_config.get('preset', 'last_1_day')}"

    # 2. Fetch from Gmail
    raw_emails = fetch_recent_emails(range_config=range_config, max_results=settings.MAX_EMAILS_PER_RUN)
    if not raw_emails:
        return {
            "status": "success",
            "message": f"No emails found in Gmail for selected range ({range_desc}).",
            "processed_count": 0,
            "job_uuid": job_uuid
        }

    # 3. Deduplicate BEFORE saving: which fetched ids are new to TiDB?
    # Previously save_processed_emails() ran FIRST, so every fetched id
    # already existed by the time get_unprocessed_message_ids() ran — the
    # dedup always returned an empty set and every run re-summarised
    # everything (the `if not target_emails: target_emails = raw_emails`
    # fallback then masked it completely).
    candidate_ids = [e["id"] for e in raw_emails]
    unprocessed_ids = get_unprocessed_message_ids(candidate_ids) if not force_all else set(candidate_ids)

    # 4. Synchronize states to TiDB (now that dedup has been computed).
    save_processed_emails(raw_emails)

    target_emails = [e for e in raw_emails if e["id"] in unprocessed_ids]
    if not target_emails:
        # All have been processed; if user explicitly clicked summary, summarize all matching the date range
        target_emails = raw_emails

    # 5. Filter spam and noise
    actionable_emails, noise_emails = filter_spam_and_noise(target_emails)
    emails_to_summarize = actionable_emails if actionable_emails else target_emails

    # 6. Dynamic intelligent batching
    batches = compute_dynamic_batches(emails_to_summarize)
    total_batches = len(batches)
    total_emails_count = len(emails_to_summarize)

    # 7. Create persistent job record in TiDB
    job_id = create_summary_job(
        job_uuid=job_uuid,
        date_range_info=range_desc,
        total_emails=total_emails_count,
        total_batches=total_batches
    )

    batch_results = []
    completed_batches = 0
    failed_batches = 0

    # 8. Process every batch with resilient failover & tracking
    for idx, batch in enumerate(batches):
        batch_ids = [e["id"] for e in batch]
        batch_record_id = create_summary_batch(
            job_id=job_id,
            batch_index=idx,
            email_count=len(batch),
            email_ids=batch_ids
        )

        max_batch_retries = 3
        batch_success = False
        last_error = None

        for attempt in range(max_batch_retries):
            try:
                logger.info(f"Processing Job {job_uuid} - Batch {idx + 1}/{total_batches} ({len(batch)} emails, attempt {attempt + 1})...")
                batch_data = process_batch_slice(batch, idx, total_batches)

                # Persist batch success in TiDB
                update_summary_batch(
                    batch_id=batch_record_id,
                    status="SUCCESS",
                    batch_summary=batch_data.get("batch_summary"),
                    action_items=batch_data.get("action_items"),
                    retry_count=attempt
                )
                batch_results.append(batch_data)
                completed_batches += 1
                batch_success = True
                break

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Batch {idx + 1} attempt {attempt + 1} failed: {e}")
                # Back off before retrying: previously the 3 attempts fired
                # back-to-back, so a rate-limit/5xx was retried into the
                # same throttled window and burned quota for nothing.
                if attempt < max_batch_retries - 1:
                    time.sleep((attempt + 1) * 2)

        if not batch_success:
            failed_batches += 1
            update_summary_batch(
                batch_id=batch_record_id,
                status="FAILED",
                error_message=last_error,
                retry_count=max_batch_retries
            )
            logger.error(f"Critical: Batch {idx + 1} permanently failed after {max_batch_retries} attempts.")

    # 9. All-or-Nothing Verification
    update_summary_job(job_id, {
        "completed_batches": completed_batches,
        "failed_batches": failed_batches
    })

    if failed_batches > 0 or completed_batches < total_batches:
        err_msg = f"All-or-nothing check failed: {failed_batches}/{total_batches} batches failed. Aborting delivery to prevent partial summary."
        logger.error(err_msg)
        update_summary_job(job_id, {
            "status": "FAILED",
            "error_message": err_msg
        })
        return {
            "status": "error",
            "message": err_msg,
            "job_uuid": job_uuid,
            "completed_batches": completed_batches,
            "total_batches": total_batches
        }

    # 10. Consolidate (Reduce stage)
    logger.info(f"All {total_batches} batches succeeded. Consolidating final briefing...")
    final_briefing = consolidate_all_batches(batch_results, noise_emails, total_emails_count)

    exec_summary = final_briefing.get("executive_summary", "")
    urgent_count = final_briefing.get("urgent_count", 0)
    action_items = final_briefing.get("action_items", [])
    markdown_report = final_briefing.get("email_markdown_report", "")

    # 11. Save to daily_reports and update job status
    today_date = _today_in_schedule_tz()
    save_daily_report(
        report_date=today_date,
        total_emails=total_emails_count,
        urgent_count=urgent_count,
        summary_markdown=markdown_report,
        action_items=action_items
    )

    update_summary_job(job_id, {
        "status": "COMPLETED",
        "executive_summary": exec_summary,
        "email_markdown_report": markdown_report,
        "action_items": action_items,
        "urgent_count": urgent_count
    })

    # 12. Dispatch final email digest to user
    subject = f"⚡ Consolidated Briefing ({total_emails_count} emails, {total_batches} batches) - {today_date.strftime('%b %d, %Y')}"
    send_daily_digest_email(subject, markdown_report)

    # 13. Invalidate caches so UI reflects new sync immediately
    invalidate_inbox_caches()

    logger.info(f"Consolidated summary successfully completed and delivered! (Job {job_uuid})")
    return {
        "status": "success",
        "job_uuid": job_uuid,
        "total_emails": total_emails_count,
        "total_batches": total_batches,
        "urgent_count": urgent_count,
        "executive_summary": exec_summary,
        "action_items": action_items
    }
