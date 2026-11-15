import base64
import logging
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional, Union
import markdown

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.config import settings

logger = logging.getLogger(__name__)

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
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)

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
            # internalDate is epoch milliseconds
            received_dt = datetime.utcfromtimestamp(int(detail["internalDate"]) / 1000.0)
        except Exception:
            pass

    return {
        "id": msg_id,
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

def build_gmail_query_from_range(range_config: Optional[dict] = None, fallback_hours: int = 24) -> str:
    """
    Constructs a Gmail search query string supporting:
    - Presets: 'last_1_day' through 'last_7_days'
    - Custom date range: start_date, end_date (YYYY-MM-DD or YYYY/MM/DD)
    - Custom time range: timestamps or ISO strings
    """
    now = datetime.utcnow()
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
    response = service.users().messages().list(
        userId='me',
        q=query,
        maxResults=max_results
    ).execute()
    
    messages = response.get('messages', [])
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
            
            email_items.append(_extract_email_metadata(msg_id, detail))
        except Exception as e:
            logger.error(f"Error fetching message {msg_id}: {e}")
            continue

    return email_items

def update_gmail_message_state(message_id: str, is_unread: Optional[bool] = None, is_starred: Optional[bool] = None, is_trash: Optional[bool] = None) -> bool:
    """
    Synchronize state changes directly back to Gmail:
    - Mark Read: removes 'UNREAD' label
    - Mark Unread: adds 'UNREAD' label
    - Star: adds 'STARRED' label
    - Unstar: removes 'STARRED' label
    - Trash: adds 'TRASH' label
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
