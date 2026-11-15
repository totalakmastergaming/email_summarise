import json
import logging
from google import genai
from google.genai import types
from app.config import settings

logger = logging.getLogger(__name__)

def _get_gemini_client():
    """Reusable client initialization with configuration check."""
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured in .env.")
    if not settings.GEMINI_MODEL:
        raise ValueError("GEMINI_MODEL is not configured in .env. Please define GEMINI_MODEL (e.g. GEMINI_MODEL=gemini-3.5-flash).")
    return genai.Client(api_key=settings.GEMINI_API_KEY)

def generate_inbox_summary(emails_data: list[dict]) -> dict:
    """
    Pass raw email metadata to Google Gemini and return structured intelligence:
    - Executive summary
    - Urgent action items
    - Categorized breakdown
    - Full formatted markdown report
    """
    client = _get_gemini_client()

    prompt = f"""
You are an elite, discreet personal executive assistant.
Analyze these {len(emails_data)} emails received today and produce a high-impact, actionable end-of-day briefing.

Return ONLY a valid JSON object matching this exact schema:
{{
  "executive_summary": "A 2-3 sentence high-level overview of what happened in my inbox today.",
  "urgent_count": <integer count of items needing immediate user attention or replies>,
  "action_items": [
    {{
      "task": "Specific actionable task description",
      "sender": "Person/Organization requesting it",
      "due": "Deadline, timeframe, or 'ASAP'",
      "priority": "High" | "Medium" | "Low"
    }}
  ],
  "categories": {{
    "urgent": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ],
    "work_projects": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ],
    "finance_billing": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ],
    "newsletters_updates": [ {{"subject": "...", "sender": "...", "takeaway": "..."}} ]
  }},
  "email_markdown_report": "A complete, beautifully formatted Markdown report with emojis, bullet points, clear headings, and action lists ready to be read in an email client."
}}

Here is the email data:
{json.dumps(emails_data, indent=2, default=str)}
"""

    logger.info(f"Submitting {len(emails_data)} emails to {settings.GEMINI_MODEL}...")

    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )

    try:
        parsed_data = json.loads(response.text)
        return parsed_data
    except Exception as e:
        logger.error(f"Failed to parse Gemini JSON output: {e}\nRaw response:\n{response.text}")
        return {
            "executive_summary": "Today's emails summarized.",
            "urgent_count": 0,
            "action_items": [],
            "categories": {},
            "email_markdown_report": response.text
        }

def answer_inbox_query(question: str, context_emails: list[dict], latest_summary: str = "") -> str:
    """
    Answers questions asked by the user in the InboxCura AI Assistant view
    using real inbox context and the model specified in .env.
    """
    client = _get_gemini_client()

    prompt = f"""
You are the InboxCura personal email assistant.
Answer the user's question concisely and accurately based on their current email inbox and today's summary.

User Question: {question}

Latest Daily Briefing Summary:
{latest_summary or 'No summary generated yet.'}

Recent Inbound Emails in Inbox:
{json.dumps(context_emails[:20], indent=2, default=str)}

Guidelines:
- Format your response in clean HTML (using <b>, <br>, <i>, <ul>, <li>) suitable for inline chat bubbles.
- Be direct, executive, and actionable.
- If asked to draft a reply, provide a polished, ready-to-send draft.
"""

    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3)
    )
    return response.text
