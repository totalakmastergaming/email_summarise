"""Legacy LLM helpers, kept for import compatibility.

These used to call Gemini directly — bypassing key rotation, cooldowns,
Groq failover, timeouts and error classification in
``app.services.llm_provider`` — and duplicated the prompts already built
in the pipeline / API layer. They are now thin wrappers over
``execute_with_failover`` so every LLM call in the app goes through the
same resilient path. Nothing in the app imports this module today; it is
kept only so external/older callers do not break.
"""
import json
import logging

from app.services.llm_provider import execute_with_failover, parse_json_response

logger = logging.getLogger(__name__)


def generate_inbox_summary(emails_data: list[dict]) -> dict:
    """
    Pass raw email metadata to the configured LLM (Gemini default, Groq
    failover) and return structured intelligence: executive summary, urgent
    action items, categorized breakdown, full formatted markdown report.
    """
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

    logger.info(f"Submitting {len(emails_data)} emails for summarisation...")
    response_text, _key_used = execute_with_failover(prompt, is_json=True)

    try:
        return parse_json_response(response_text)
    except Exception as e:
        logger.error(f"Failed to parse LLM JSON output: {e}")
        return {
            "executive_summary": "Today's emails summarized.",
            "urgent_count": 0,
            "action_items": [],
            "categories": {},
            "email_markdown_report": response_text,
        }


def answer_inbox_query(question: str, context_emails: list[dict], latest_summary: str = "") -> str:
    """
    Answers questions asked by the user in the InboxCura AI Assistant view
    using real inbox context and the configured model (via failover).
    """
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

    answer, _key_used = execute_with_failover(prompt, is_json=False)
    return answer
