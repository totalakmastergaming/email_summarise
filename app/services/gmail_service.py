import base64
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Optional, Union
import markdown

from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
import httplib2
from googleapiclient.discovery import build
from app.config import settings

logger = logging.getLogger(__name__)

# Gmail API calls previously had NO HTTP timeout (googleapiclient default),
# so a stalled Gmail call could hang a Render worker thread forever.
GMAIL_HTTP_TIMEOUT = 30


def _utcnow_naive() -> datetime:
    """Timezone-naive UTC now (matches TiDB DATETIME columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_gmail_client():
    """Build authenticated Gmail API client using stored refresh token."""
    if not settings.GMAIL_REFRESH_TOKEN or not settings.GMAIL_CLIENT_ID:
        raise ValueError("Gmail credentials missing. Please set GMAIL_CLIENT_ID and GMAIL_REFRESH_TOKEN.")

    creds = Credentials(
        token=None,
        refresh_token=settings.GMAIL_REFRESH_TOKEN,
        client_id=settings.GMAIL_CLIENT_ID,
        client_secret=settings.GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=GMAIL_HTTP_TIMEOUT))
    return build('gmail', 'v1', http=http, cache_discovery=False)

def _extract_email_metadata(msg_id: str, detail: dict) -> dict:
    """Extract and normalize email header metadata, snippet, and Gmail state flags."""
    headers = {h['name']: h['value'] for h in detail.get('payload', {}).get('headers', [])}
    label_ids = detail.get('labelIds', [])
    is_unread = 'UNREAD' in label_ids
    is_starred = 'STARRED' in label_ids
    is_spam = 'SPAM' in label_ids
    is_trash = 'TRASH' in label_ids

    date_str = headers.get("Date", "")
    received_dt = None
    if detail.get("internalDate"):
        try:
            # internalDate is epoch milliseconds (UTC).
            received_dt = datetime.fromtimestamp(
                int(detail["internalDate"]) / 1000.0, tz=timezone.utc
            ).replace(tzinfo=None)
        except Exception:
            pass

    return {
        "id": msg_id,
        "thread_id": detail.get("threadId"),
        "sender": headers.get("From", "Unknown Sender"),
        "subject": headers.get("Subject", "(No Subject)"),
        "date": date_str,
        "received_at": received_dt.isoformat() if received_dt else None,
        "snippet": detail.get("snippet", ""),
        "label_ids": label_ids,
        "is_unread": is_unread,
        "is_starred": is_starred,
        "is_spam": is_spam,
        "is_trash": is_trash
    }

def _decode_b64url(data: str) -> str:
    """Decode Gmail's base64url body part to text (tolerates bad parts)."""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> tuple[str, str]:
    """Walk a Gmail message payload and return (plain_text, html).

    Prefers text/plain; falls back to text/html when a message is
    HTML-only. Only the first part of each type is kept (keeps memory
    bounded on 512MB Render — no giant attachment decoding; attachment
    parts are skipped entirely).
    """
    text_plain = ""
    text_html = ""

    def walk(part: dict):
        nonlocal text_plain, text_html
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        if mime == "text/plain" and not text_plain:
            text_plain = _decode_b64url(body.get("data", ""))
        elif mime == "text/html" and not text_html:
            text_html = _decode_b64url(body.get("data", ""))
        for sub in part.get("parts", []) or []:
            if text_plain and text_html:
                return
            walk(sub)

    walk(payload or {})
    return text_plain, text_html


def get_email_full(message_id: str) -> dict:
    """Fetch one message with its FULL body for the reading view.

    Previously the reader only ever saw the snippet (``format='metadata'``),
    so the reading view could never show the actual email. This fetches
    ``format='full'`` and returns plain-text + HTML bodies plus headers.
    """
    service = get_gmail_client()
    detail = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()
    meta = _extract_email_metadata(message_id, detail)
    text_plain, text_html = _extract_body(detail.get("payload", {}))
    meta["body_text"] = text_plain
    meta["body_html"] = text_html
    meta["thread_id"] = detail.get("threadId")
    return meta


def build_gmail_query_from_range(range_config: Optional[dict] = None, fallback_hours: int = 24) -> str:
    """
    Constructs a Gmail search query string supporting:
    - Presets: 'last_1_day' through 'last_7_days'
    - Custom date range: start_date, end_date (YYYY-MM-DD or YYYY/MM/DD)
    - Custom time range: timestamps or ISO strings
    """
    now = _utcnow_naive()
    if not range_config:
        since_date = (now - timedelta(hours=fallback_hours)).strftime("%Y/%m/%d")
        return f"after:{since_date}"

    preset = range_config.get("preset", "last_1_day")
    preset_days_map = {
        "last_1_day": 1,
        "last_2_days": 2,
        "last_3_days": 3,
        "last_4_days": 4,
        "last_5_days": 5,
        "last_6_days": 6,
        "last_7_days": 7,
    }

    if preset in preset_days_map:
        days = preset_days_map[preset]
        since_date = (now - timedelta(days=days)).strftime("%Y/%m/%d")
        return f"after:{since_date}"

    if preset == "custom":
        query_parts = []
        start_val = range_config.get("custom_start")
        end_val = range_config.get("custom_end")

        if start_val:
            # Handle YYYY-MM-DD or full timestamp
            clean_start = str(start_val).replace("-", "/")
            if "T" in clean_start:
                clean_start = clean_start.split("T")[0]
            query_parts.append(f"after:{clean_start}")

        if end_val:
            clean_end = str(end_val).replace("-", "/")
            if "T" in clean_end:
                clean_end = clean_end.split("T")[0]
            query_parts.append(f"before:{clean_end}")

        if query_parts:
            return " ".join(query_parts)

    # Default fallback
    since_date = (now - timedelta(hours=fallback_hours)).strftime("%Y/%m/%d")
    return f"after:{since_date}"

def fetch_recent_emails(range_config: Optional[dict] = None, hours: int = 24, max_results: int = 150) -> list[dict]:
    """
    Fetch emails within the specified date/time range.
    Returns cleaned metadata dictionaries including live Gmail label states.
    """
    service = get_gmail_client()
    query = build_gmail_query_from_range(range_config, fallback_hours=hours)

    logger.info(f"Querying Gmail with query: '{query}', maxResults: {max_results}")
    # Paginate: Gmail's list() returns one page plus a nextPageToken.
    # Previously only the first page was read, so any mailbox with more
    # than max_results matching messages silently lost the rest.
    messages = []
    page_token = None
    while len(messages) < max_results:
        response = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=min(max_results - len(messages), 100),
            pageToken=page_token
        ).execute()
        messages.extend(response.get('messages', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break

    if not messages:
        logger.info(f"No messages found in Gmail for query: {query}")
        return []

    email_items = []
    for msg in messages:
        msg_id = msg['id']
        try:
            detail = service.users().messages().get(
                userId='me',
                id=msg_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()

            item = _extract_email_metadata(msg_id, detail)
            # threadId from the list response (authoritative, no extra call).
            item["thread_id"] = msg.get("threadId") or item.get("thread_id")
            email_items.append(item)
        except Exception as e:
            logger.error(f"Error fetching message {msg_id}: {e}")
            continue

    return email_items

def get_message_metadata(message_id: str) -> dict:
    """Fetch one message's metadata (no body) for the History sync.

    The sync learns a message id from a history record and needs its
    headers/labels/thread to upsert it — one cheap metadata call, not a
    full-body fetch.
    """
    service = get_gmail_client()
    detail = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    return _extract_email_metadata(message_id, detail)


def get_profile_history_id() -> int:
    """Current Gmail historyId for the account (users.getProfile).

    This is the checkpoint value a fresh or reconciled sync anchors to.
    """
    service = get_gmail_client()
    profile = service.users().getProfile(userId="me").execute()
    return int(profile.get("historyId"))


def list_history(start_history_id: int, page_token: Optional[str] = None) -> dict:
    """One page of Gmail History records from startHistoryId.

    historyTypes cover adds, deletes and label changes; maxResults is
    capped at 100 per page by the caller (history_sync caps the run at
    5 pages). Raises googleapiclient HttpError — callers translate a
    404/410 into a controlled reconciliation.
    """
    service = get_gmail_client()
    kwargs: dict = {
        "userId": "me",
        "startHistoryId": str(start_history_id),
        "maxResults": 100,
        "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
    }
    if page_token:
        kwargs["pageToken"] = page_token
    return service.users().history().list(**kwargs).execute()


def update_gmail_message_state(message_id: str, is_unread: Optional[bool] = None, is_starred: Optional[bool] = None, is_trash: Optional[bool] = None, is_archived: Optional[bool] = None, is_spam: Optional[bool] = None) -> bool:
    """
    Synchronize state changes directly back to Gmail:
    - Mark Read: removes 'UNREAD' label
    - Mark Unread: adds 'UNREAD' label
    - Star: adds 'STARRED' label
    - Unstar: removes 'STARRED' label
    - Trash: adds 'TRASH' label
    - Archive: removes 'INBOX' label (Gmail archive); un-archive re-adds it
    - Spam: adds 'SPAM' label and removes 'INBOX' (Gmail moves it out of
      the inbox); un-spam reverses both
    """
    try:
        service = get_gmail_client()
        add_labels = []
        remove_labels = []

        if is_unread is not None:
            if is_unread:
                add_labels.append("UNREAD")
            else:
                remove_labels.append("UNREAD")

        if is_starred is not None:
            if is_starred:
                add_labels.append("STARRED")
            else:
                remove_labels.append("STARRED")

        if is_trash is not None:
            if is_trash:
                add_labels.append("TRASH")
            else:
                remove_labels.append("TRASH")

        if is_archived is not None:
            if is_archived:
                remove_labels.append("INBOX")
            else:
                add_labels.append("INBOX")

        if is_spam is not None:
            if is_spam:
                add_labels.append("SPAM")
                remove_labels.append("INBOX")
            else:
                remove_labels.append("SPAM")
                add_labels.append("INBOX")

        body = {}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels

        if body:
            service.users().messages().modify(userId="me", id=message_id, body=body).execute()
            logger.info(f"Successfully synced Gmail state for {message_id}: add={add_labels}, remove={remove_labels}")
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to synchronize state with Gmail for message {message_id}: {e}")
        return False


def batch_update_message_states(message_ids: list[str], is_unread: Optional[bool] = None, is_starred: Optional[bool] = None, is_trash: Optional[bool] = None, is_archived: Optional[bool] = None, is_spam: Optional[bool] = None) -> bool:
    """Apply one label change to many messages in a SINGLE Gmail call.

    Previously there was no bulk path at all: mark-all-read / archive-all
    in the UI could only flip local TiDB rows (or fire one API call per
    message), so Gmail itself never changed. ``batchModify`` does it in
    one request (Gmail caps a batch at 1000 ids; callers cap at 100).
    Returns True only if Gmail accepted the change.
    """
    if not message_ids:
        return False
    add_labels: list[str] = []
    remove_labels: list[str] = []
    if is_unread is not None:
        (add_labels if is_unread else remove_labels).append("UNREAD")
    if is_starred is not None:
        (add_labels if is_starred else remove_labels).append("STARRED")
    if is_trash is not None:
        (add_labels if is_trash else remove_labels).append("TRASH")
    if is_archived is not None:
        (remove_labels if is_archived else add_labels).append("INBOX")
    if is_spam is not None:
        if is_spam:
            add_labels.append("SPAM")
            remove_labels.append("INBOX")
        else:
            remove_labels.append("SPAM")
            add_labels.append("INBOX")
    if not add_labels and not remove_labels:
        return False
    try:
        service = get_gmail_client()
        body: dict = {"ids": list(message_ids)}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels
        service.users().messages().batchModify(userId="me", body=body).execute()
        logger.info(f"Batch-synced Gmail state for {len(message_ids)} messages: add={add_labels}, remove={remove_labels}")
        return True
    except Exception as e:
        logger.error(f"Failed to batch-sync Gmail state for {len(message_ids)} messages: {e}")
        return False

def list_user_labels() -> list[dict]:
    """User-created Gmail labels, for the label menu in the UI.

    Returns [{id, name}] for labels of type 'user' only — system labels
    (INBOX, STARRED, ...) are managed by the state endpoints instead.
    """
    service = get_gmail_client()
    response = service.users().labels().list(userId="me").execute()
    return [
        {"id": lab.get("id"), "name": lab.get("name")}
        for lab in response.get("labels", [])
        if lab.get("type") == "user"
    ]


def modify_message_labels(message_id: str, add_labels: Optional[list[str]] = None, remove_labels: Optional[list[str]] = None) -> bool:
    """Add / remove arbitrary Gmail labels on one message.

    Used by the real label action in the UI (previously a local-only
    toast that changed nothing). Returns True only if Gmail accepted
    the modify call.
    """
    add_labels = add_labels or []
    remove_labels = remove_labels or []
    if not add_labels and not remove_labels:
        return False
    body: dict = {}
    if add_labels:
        body["addLabelIds"] = list(add_labels)
    if remove_labels:
        body["removeLabelIds"] = list(remove_labels)
    try:
        service = get_gmail_client()
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        logger.info(f"Modified labels for {message_id}: add={add_labels}, remove={remove_labels}")
        return True
    except Exception as e:
        logger.error(f"Failed to modify labels for {message_id}: {e}")
        return False


def sync_inbox_labels_from_gmail(message_ids: list[str]) -> list[dict]:
    """
    Query Gmail for current labelIds for a list of message IDs.
    Returns list of updated states: [{message_id, is_unread, is_starred}].
    """
    if not message_ids:
        return []

    service = get_gmail_client()
    updated_states = []
    for mid in message_ids:
        try:
            detail = service.users().messages().get(
                userId='me',
                id=mid,
                format='minimal'
            ).execute()
            label_ids = detail.get('labelIds', [])
            updated_states.append({
                "message_id": mid,
                "is_unread": "UNREAD" in label_ids,
                "is_starred": "STARRED" in label_ids,
                "is_trash": "TRASH" in label_ids,
                # Archived in Gmail == no longer in INBOX.
                "is_archived": "INBOX" not in label_ids,
                "label_ids": label_ids
            })
        except Exception as e:
            logger.error(f"Error checking Gmail labels for message {mid}: {e}")

    return updated_states

def send_daily_digest_email(subject_title: str, markdown_content: str) -> bool:
    """
    Send the compiled executive briefing to your personal email address.
    Formats both plain-text and responsive HTML.
    """
    if not settings.MY_EMAIL:
        logger.warning("MY_EMAIL is not configured. Skipping email dispatch.")
        return False
        
    service = get_gmail_client()
    
    msg = EmailMessage()
    msg['Subject'] = subject_title
    msg['From'] = 'me'
    msg['To'] = settings.MY_EMAIL

    # Plaintext fallback
    msg.set_content(markdown_content)

    # HTML formatted version
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f6f6f8; color: #13131c; padding: 24px; line-height: 1.6; }}
        .container {{ max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 12px; padding: 32px; border: 1px solid #e6e6eb; box-shadow: 0 4px 12px rgba(0,0,0,0.04); }}
        h1 {{ font-size: 22px; color: #13131c; border-bottom: 2px solid #efedfd; padding-bottom: 12px; margin-top: 0; }}
        h2 {{ font-size: 17px; color: #4c3fd6; margin-top: 24px; border-bottom: 1px solid #f0f0f4; padding-bottom: 6px; }}
        h3 {{ font-size: 15px; color: #13131c; margin-top: 18px; }}
        ul {{ padding-left: 22px; }}
        li {{ margin-bottom: 8px; }}
        blockquote {{ border-left: 4px solid #6357e8; margin: 16px 0; padding-left: 14px; color: #55556a; font-style: italic; background: #fbfbfc; padding: 10px 14px; border-radius: 4px; }}
        .badge {{ display: inline-block; background: #efedfd; color: #4c3fd6; font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}
        .footer {{ font-size: 12px; color: #8a8a9c; margin-top: 32px; text-align: center; border-top: 1px solid #e6e6eb; padding-top: 16px; }}
      </style>
    </head>
    <body>
      <div class="container">
        {markdown.markdown(markdown_content, extensions=['tables', 'fenced_code'])}
        <div class="footer">
          Delivered by <strong>InboxCura AI</strong> &bull; Backed by TiDB Serverless & Gemini 3.5 Flash
        </div>
      </div>
    </body>
    </html>
    """
    msg.add_alternative(html_body, subtype='html')

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    logger.info(f"Daily digest successfully sent to {settings.MY_EMAIL}")
    return True


def send_email(to_address: str, subject: str, body_text: str) -> Optional[str]:
    """Compose + send a new email via the Gmail API (gmail.send scope).

    Previously the UI could only open a ``mailto:`` draft — no real send
    from InboxCura at all, even though the granted OAuth scopes already
    include gmail.send. Returns the sent Gmail message id, or None.
    """
    service = get_gmail_client()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "me"
    msg["To"] = to_address
    msg.set_content(body_text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Sent email to {to_address} (id={sent.get('id')})")
    return sent.get("id")


def reply_to_email(message_id: str, body_text: str, reply_all: bool = False) -> Optional[str]:
    """Reply in-thread to a Gmail message via the Gmail API.

    Fetches the original (thread id, sender, subject), then sends with
    the same ``threadId`` so Gmail threads the reply correctly, with a
    ``Re:`` subject when the original has none, and In-Reply-To /
    References preserved so other clients thread it too.

    reply_all: the reply also goes to the other participants — To is
    the original sender plus the original To recipients, Cc is the
    original Cc recipients (each minus the user's own address and
    duplicates). With reply_all=False the reply goes only to the sender.
    Returns the sent id.
    """
    service = get_gmail_client()
    original = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID", "References"],
    ).execute()
    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
    _name, sender_email = parseaddr(headers.get("From", ""))
    if not sender_email:
        raise ValueError("Original message has no sender to reply to.")
    subject = headers.get("Subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}" if subject else "Re: (no subject)"

    def _addrs(header_value: str) -> list[str]:
        """Split a To/Cc header into individual email addresses."""
        addrs: list[str] = []
        for part in (header_value or "").split(","):
            _n, addr = parseaddr(part.strip())
            if addr:
                addrs.append(addr)
        return addrs

    my_email = (settings.MY_EMAIL or "").strip().lower()
    recipients = [sender_email]
    cc_list: list[str] = []
    if reply_all:
        seen = {sender_email.lower()}
        if my_email:
            seen.add(my_email)
        for addr in _addrs(headers.get("To", "")):
            if addr.lower() not in seen:
                seen.add(addr.lower())
                recipients.append(addr)
        for addr in _addrs(headers.get("Cc", "")):
            if addr.lower() not in seen:
                seen.add(addr.lower())
                cc_list.append(addr)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "me"
    msg["To"] = ", ".join(recipients)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if headers.get("Message-ID"):
        msg["In-Reply-To"] = headers["Message-ID"]
        # Preserve the original References chain, appending this
        # message's id so the thread stays linked downstream.
        orig_refs = (headers.get("References") or "").strip()
        msg["References"] = f"{orig_refs} {headers['Message-ID']}".strip()
    msg.set_content(body_text)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": original.get("threadId")},
    ).execute()
    logger.info(f"Sent reply to {sender_email} in thread {original.get('threadId')} (id={sent.get('id')}, reply_all={reply_all})")
    return sent.get("id")
