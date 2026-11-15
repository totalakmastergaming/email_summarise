"""Gmail History API incremental sync (Phase 1).

Keeps the TiDB mirror current without re-listing the whole mailbox:
Gmail's history.list() returns only what changed since the stored
historyId checkpoint (adds, deletes, label changes), and this module
applies those deltas to ``processed_emails``.

Checkpoint rules (the important part):
- The checkpoint ONLY advances after a run processes ALL of its pages
  successfully. A run that stops early (fetch cap, page cap, error)
  returns ``has_more=true`` and leaves the old checkpoint in place.
- Reprocessing is safe: message upserts are idempotent and label
  deltas are set operations on the local labels CSV, so replaying the
  same history records cannot corrupt state.
- An expired startHistoryId (Gmail answers 404/410) triggers a
  CONTROLLED reconciliation: bounded re-fetch of the last 7 days and a
  fresh checkpoint. The local cache is NEVER wiped — reconciliation
  only upserts, it never deletes rows.
- Deleted messages are soft-trashed locally (is_trashed=TRUE), never
  removed from TiDB.

Bounds (Render free tier, 512MB, no background workers): at most 5
history pages x 100 records per page per run, at most 100 metadata
fetches per run. A run that hits a bound stops early with
``has_more=true``; the next trigger resumes from the same checkpoint.
"""
import logging
import threading
import time
from typing import Optional

from googleapiclient.errors import HttpError

from app.database import (
    get_sync_checkpoint,
    set_sync_checkpoint,
    save_processed_emails,
    mark_messages_trashed,
    apply_label_delta,
)
from app.services.gmail_service import (
    fetch_recent_emails,
    get_message_metadata,
    get_profile_history_id,
    list_history,
)

logger = logging.getLogger(__name__)

# Single-flight guard: the sync can be triggered from four places at
# once (inbox list background trigger, /api/sync/incremental, the
# rewritten /api/sync-inbox-state, the pipeline pre-step). A
# non-blocking lock makes the second caller get a clean "busy" answer
# instead of two concurrent runs applying the same deltas.
_sync_lock = threading.Lock()

# Background-trigger throttle: GET /api/mails kicks off a background
# sync at most once per interval, so normal inbox browsing does not
# hammer the Gmail API.
_BACKGROUND_MIN_INTERVAL_SECONDS = 120
_last_background_attempt = 0.0

MAX_HISTORY_PAGES = 5
MAX_RECORDS_PER_PAGE = 100
MAX_FETCHES_PER_RUN = 100


def run_incremental_sync(max_records: int = 500) -> dict:
    """Run one bounded incremental sync. Never raises for sync errors —
    failures are returned as {"status": "error", ...} with the checkpoint
    left untouched. Returns {"status": "busy"} when another run holds
    the lock.
    """
    if not _sync_lock.acquire(blocking=False):
        logger.info("Incremental sync already running; refusing concurrent run.")
        return {"status": "busy", "message": "An incremental sync is already in progress."}
    try:
        return _run_incremental_sync_locked(max_records)
    finally:
        _sync_lock.release()


def _run_incremental_sync_locked(max_records: int) -> dict:
    try:
        checkpoint = get_sync_checkpoint()
    except Exception as e:
        logger.error(f"Incremental sync: could not read checkpoint: {e}")
        return {"status": "error", "error": f"Could not read sync checkpoint: {e}", "has_more": True}

    try:
        if not checkpoint or not checkpoint.get("history_id"):
            return _initial_sync()
        return _incremental_sync(checkpoint["history_id"], max_records)
    except HttpError as e:
        # Expired startHistoryId: Gmail answers 404 (unknown id) or 410
        # (id too old, history pruned). Controlled reconciliation — see
        # module docstring. Any OTHER HttpError is a plain failure.
        status_code = getattr(e.resp, "status", None)
        if status_code in (404, 410):
            logger.warning(
                f"Gmail history id expired (HTTP {status_code}); reconciling "
                "with a bounded 7-day re-fetch (local cache is NOT wiped)."
            )
            return _reconcile_after_expiry()
        logger.error(f"Incremental sync failed with Gmail HTTP error: {e}")
        return {"status": "error", "error": str(e), "processed": {"added": 0, "deleted": 0, "label_changes": 0}, "has_more": True}
    except Exception as e:
        logger.error(f"Incremental sync failed: {e}")
        return {"status": "error", "error": str(e), "processed": {"added": 0, "deleted": 0, "label_changes": 0}, "has_more": True}


def _initial_sync() -> dict:
    """No checkpoint yet: bounded 7-day fetch, then anchor the
    checkpoint to the profile's current historyId."""
    profile_history_id = get_profile_history_id()
    emails = fetch_recent_emails(range_config={"preset": "last_7_days"}, max_results=100)
    if emails:
        save_processed_emails(emails)
    set_sync_checkpoint(profile_history_id, "initial")
    logger.info(f"Initial sync: stored {len(emails)} mails, checkpoint={profile_history_id}.")
    return {"status": "success", "mode": "initial", "added": len(emails), "has_more": False}


def _reconcile_after_expiry() -> dict:
    """History id expired: bounded 7-day re-fetch + fresh checkpoint.

    Deliberately NOT a full resync and NOT a cache wipe — it upserts the
    recent window and re-anchors. Anything older stays as it was; the
    next incremental runs keep it current from the new checkpoint.
    """
    profile_history_id = get_profile_history_id()
    emails = fetch_recent_emails(range_config={"preset": "last_7_days"}, max_results=100)
    if emails:
        save_processed_emails(emails)
    set_sync_checkpoint(profile_history_id, "reconciled")
    logger.warning(f"History reconciled: re-fetched {len(emails)} mails, new checkpoint={profile_history_id}.")
    return {
        "status": "success",
        "mode": "reconciled",
        "added": len(emails),
        "has_more": False,
        "warning": "Gmail history id expired; re-fetched the last 7 days and reset the checkpoint. No local mail was deleted.",
    }


def _incremental_sync(start_history_id: int, max_records: int) -> dict:
    processed = {"added": 0, "deleted": 0, "label_changes": 0}
    records_seen = 0
    fetches = 0
    has_more = False
    page_token: Optional[str] = None
    latest_history_id: Optional[str] = None

    for _page in range(MAX_HISTORY_PAGES):
        resp = list_history(start_history_id, page_token)
        latest_history_id = resp.get("historyId")
        records = resp.get("history", []) or []
        for record in records:
            if records_seen >= max_records:
                has_more = True
                break
            records_seen += 1
            # messagesAdded: collect ids, fetch metadata (bounded),
            # upsert. Order matters — records are applied in the order
            # Gmail returned them.
            added_items = []
            for entry in record.get("messagesAdded", []) or []:
                msg_id = (entry.get("message") or {}).get("id")
                if not msg_id:
                    continue
                if fetches >= MAX_FETCHES_PER_RUN:
                    # Cap hit mid-run: stop NOW, do not advance the
                    # checkpoint. The next run replays from the same
                    # checkpoint and picks these up.
                    has_more = True
                    break
                try:
                    added_items.append(get_message_metadata(msg_id))
                except Exception as e:
                    # One unreadable message must not abort the sync;
                    # the next run retries it from the same checkpoint.
                    logger.warning(f"Incremental sync: could not fetch metadata for {msg_id}: {e}")
                    continue
                fetches += 1
            if has_more:
                break
            if added_items:
                save_processed_emails(added_items)
                processed["added"] += len(added_items)

            # messagesDeleted: soft-trash locally, never delete rows.
            deleted_ids = [
                (entry.get("message") or {}).get("id")
                for entry in record.get("messagesDeleted", []) or []
                if (entry.get("message") or {}).get("id")
            ]
            if deleted_ids:
                processed["deleted"] += mark_messages_trashed(deleted_ids)

            # labelsAdded / labelsRemoved: apply the delta to the local
            # labels CSV + derived flags, with no extra Gmail calls.
            # Messages not in TiDB are skipped (nothing to update).
            for kind, is_add in (("labelsAdded", True), ("labelsRemoved", False)):
                for entry in record.get(kind, []) or []:
                    msg_id = (entry.get("message") or {}).get("id")
                    label_ids = entry.get("labelIds", []) or []
                    if not msg_id or not label_ids:
                        continue
                    if is_add:
                        applied = apply_label_delta(msg_id, add_labels=label_ids)
                    else:
                        applied = apply_label_delta(msg_id, remove_labels=label_ids)
                    if applied:
                        processed["label_changes"] += 1

        if has_more:
            break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    else:
        # All 5 pages consumed and Gmail still has more pages.
        if page_token:
            has_more = True

    if has_more:
        logger.info(f"Incremental sync stopped early (has_more=true): processed={processed}. Checkpoint NOT advanced.")
        return {"status": "success", "mode": "incremental", "processed": processed, "has_more": True}

    if latest_history_id is not None:
        set_sync_checkpoint(int(latest_history_id), "incremental")
    logger.info(f"Incremental sync done: processed={processed}, checkpoint={latest_history_id}.")
    return {"status": "success", "mode": "incremental", "processed": processed, "has_more": False}


def maybe_trigger_background_sync() -> bool:
    """Kick off a background incremental sync, throttled to at most once
    per _BACKGROUND_MIN_INTERVAL_SECONDS. Never blocks the caller;
    returns True when a sync thread was started. Called from
    GET /api/mails so browsing the inbox keeps the mirror fresh.
    """
    global _last_background_attempt
    now = time.monotonic()
    if now - _last_background_attempt < _BACKGROUND_MIN_INTERVAL_SECONDS:
        return False
    _last_background_attempt = now
    thread = threading.Thread(
        target=_background_sync_run, daemon=True, name="history-sync-background"
    )
    thread.start()
    return True


def _background_sync_run() -> None:
    try:
        result = run_incremental_sync()
        logger.info(
            f"Background incremental sync finished: status={result.get('status')} "
            f"mode={result.get('mode')}"
        )
    except Exception as e:
        # run_incremental_sync already returns errors as dicts; this is
        # a last-resort guard so a background thread can never crash.
        logger.warning(f"Background incremental sync raised: {e}")
