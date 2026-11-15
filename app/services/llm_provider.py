import json
import logging
import math
import time
import urllib.request
import urllib.error
from typing import Any, Optional

from google import genai
from google.genai import types

from app.config import settings
from app.database import (
    get_active_api_keys,
    record_api_key_usage,
    set_api_key_cooldown,
    update_api_key
)

logger = logging.getLogger(__name__)

class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""
    pass

class QuotaExceededError(LLMProviderError):
    """Raised when an API key hits rate limits or quota boundaries (HTTP 429)."""
    pass

class InvalidKeyError(LLMProviderError):
    """Raised when an API key is revoked, unauthorized, or invalid (HTTP 401/403)."""
    pass

class ContextWindowExceededError(LLMProviderError):
    """Raised when the prompt token size exceeds model context window limits."""
    pass

class TransientServerError(LLMProviderError):
    """Raised on temporary server 5xx errors or network timeouts."""
    pass

def parse_json_response(text: str) -> dict:
    """Parse an LLM JSON reply, tolerating ```json fenced blocks.

    Gemini (``response_mime_type="application/json"``) and Groq
    (``response_format={"type": "json_object"}``) should return raw JSON,
    but models still sometimes wrap it in a markdown fence, which made
    whole batches fail permanently. One shared helper, used by the
    pipeline and llm_service, instead of each caller re-trying json.loads.
    """
    if not text:
        raise ValueError("Empty LLM response.")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip opening fence (``` or ```json) and closing fence.
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1:] if first_newline != -1 else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def call_gemini(api_key: str, model_name: str, prompt: str, is_json: bool = False) -> str:
    """Direct invocation for Google Gemini models via google-genai SDK."""
    try:
        # 60s HTTP timeout: previously there was NO timeout at all, so a
        # hung Gemini call could block a Render worker thread indefinitely.
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=60_000),
        )
        config_args = {"temperature": 0.2}
        if is_json:
            config_args["response_mime_type"] = "application/json"

        response = client.models.generate_content(
            model=model_name or settings.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(**config_args)
        )
        text = response.text
        if not text:
            raise LLMProviderError("Gemini returned an empty response.")
        return text
    except (QuotaExceededError, InvalidKeyError, ContextWindowExceededError, TransientServerError, LLMProviderError):
        raise
    except Exception as e:
        err_str = str(e).lower()
        if "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str:
            raise QuotaExceededError(f"Gemini quota/rate limit exceeded: {e}")
        elif "401" in err_str or "403" in err_str or "api_key_invalid" in err_str or "permission_denied" in err_str:
            raise InvalidKeyError(f"Invalid Gemini API key: {e}")
        # NOTE: the old check was `"context" in err_str or "token" in
        # err_str and "maximum" in err_str`. Because `and` binds tighter
        # than `or`, ANY error mentioning "context" — including a timeout
        # ("context deadline exceeded") — was misclassified as a context
        # overflow, which made the pipeline split batches forever instead
        # of retrying/failing over. Match real overflow phrasing only.
        elif ("context window" in err_str or "maximum context" in err_str
              or "exceeds the maximum number of tokens" in err_str
              or "too many tokens" in err_str or "token limit" in err_str):
            raise ContextWindowExceededError(f"Gemini context window exceeded: {e}")
        elif "500" in err_str or "503" in err_str or "timeout" in err_str or "unavailable" in err_str or "deadline exceeded" in err_str:
            raise TransientServerError(f"Gemini transient server error: {e}")
        raise LLMProviderError(f"Gemini API error: {e}")

def call_groq(api_key: str, model_name: str, prompt: str, is_json: bool = False) -> str:
    """Invocation for Groq models via OpenAI-compatible endpoint."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    model = model_name or "openai/gpt-oss-120b"
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are InboxCura's executive email summarization engine. Follow all rules strictly and output valid structured data."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }
    if is_json:
        payload["response_format"] = {"type": "json_object"}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "InboxCura/2.0"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            try:
                content = res_data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMProviderError(f"Groq returned an unexpected payload: {str(res_data)[:200]}")
            if not content:
                raise LLMProviderError("Groq returned an empty response.")
            return content
    except urllib.error.HTTPError as e:
        status_code = e.code
        body = e.read().decode("utf-8", errors="ignore")
        if status_code == 429:
            raise QuotaExceededError(f"Groq 429 rate limit: {body}")
        elif status_code in (401, 403):
            raise InvalidKeyError(f"Groq invalid API key: {body}")
        elif status_code >= 500:
            raise TransientServerError(f"Groq 5xx server error: {body}")
        elif "context_length_exceeded" in body or "maximum context length" in body:
            raise ContextWindowExceededError(f"Groq context window exceeded: {body}")
        raise LLMProviderError(f"Groq error ({status_code}): {body}")
    except LLMProviderError:
        raise  # Malformed/empty payload: not a transient network error.
    except Exception as e:
        raise TransientServerError(f"Network error contacting Groq: {e}")

def call_generic_openai_compatible(api_key: str, model_name: str, prompt: str, endpoint: str = "https://api.openai.com/v1/chat/completions", is_json: bool = False) -> str:
    """Universal OpenAI-compatible provider caller (OpenAI, DeepSeek, Local, etc.)."""
    if not model_name:
        # No silent fallback to a hardcoded model (project no-hardcoding
        # rule): the model must come from the api_keys row / .env.
        raise LLMProviderError("No model_name configured for this API key.")
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "You are InboxCura AI. Follow all rules strictly and return structured output."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }
    if is_json:
        payload["response_format"] = {"type": "json_object"}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "InboxCura/2.0"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=35) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            try:
                content = res_data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMProviderError(f"Provider returned an unexpected payload: {str(res_data)[:200]}")
            if not content:
                raise LLMProviderError("Provider returned an empty response.")
            return content
    except urllib.error.HTTPError as e:
        status_code = e.code
        body = e.read().decode("utf-8", errors="ignore")
        if status_code == 429:
            raise QuotaExceededError(f"Provider 429 rate limit: {body}")
        elif status_code in (401, 403):
            raise InvalidKeyError(f"Provider auth error: {body}")
        elif status_code >= 500:
            raise TransientServerError(f"Provider 5xx error: {body}")
        raise LLMProviderError(f"Provider error ({status_code}): {body}")
    except LLMProviderError:
        raise  # Malformed/empty payload: not a transient network error.
    except Exception as e:
        raise TransientServerError(f"Network connection error: {e}")

def call_provider(key_record: dict, prompt: str, is_json: bool = False) -> str:
    """
    Polymorphic dispatcher: routes request based on provider field.
    Never exposes raw secrets in logs.
    """
    provider = key_record.get("provider", "gemini").lower()
    api_key = key_record.get("api_key", "")
    model_name = key_record.get("model_name", "")

    if provider == "gemini":
        return call_gemini(api_key, model_name, prompt, is_json=is_json)
    elif provider == "groq":
        return call_groq(api_key, model_name, prompt, is_json=is_json)
    elif provider == "openai":
        return call_generic_openai_compatible(api_key, model_name, prompt, endpoint="https://api.openai.com/v1/chat/completions", is_json=is_json)
    else:
        # Fallback to Gemini if configured
        return call_gemini(api_key, model_name, prompt, is_json=is_json)

# ================= Dynamic Email Batching =================

def compute_dynamic_batches(emails: list[dict], available_keys_count: int = 1) -> list[list[dict]]:
    """
    Intelligently partitions emails into dynamic batches based on:
    - Total email volume
    - Content length and estimated token consumption
    - Number of active API keys available
    
    Guarantees:
    - 100% of emails are placed in a batch (no email skipped).
    - Batches are balanced for concurrent/failover processing.
    """
    if not emails:
        return []

    total_emails = len(emails)
    if total_emails <= 4:
        return [emails]

    # Calculate average token footprint from subject + snippet
    total_chars = sum(len(e.get("subject", "")) + len(e.get("snippet", "")) for e in emails)
    avg_chars_per_email = total_chars / total_emails if total_emails else 100

    # Determine dynamic batch size
    if avg_chars_per_email > 400:
        # Heavy content: smaller batch size to avoid context exhaustion
        target_batch_size = 4
    elif avg_chars_per_email > 200:
        target_batch_size = 5
    else:
        # Lighter notifications/newsletters: larger batch size
        target_batch_size = 7

    # If we have multiple API keys, adjust so batches distribute evenly
    if available_keys_count > 1 and total_emails >= available_keys_count * 2:
        num_batches = max(available_keys_count, math.ceil(total_emails / target_batch_size))
        actual_batch_size = math.ceil(total_emails / num_batches)
        target_batch_size = min(target_batch_size, actual_batch_size)

    batches = []
    for i in range(0, total_emails, target_batch_size):
        batches.append(emails[i:i + target_batch_size])

    logger.info(f"Dynamic batching: {total_emails} emails partitioned into {len(batches)} batches (target size ~{target_batch_size}).")
    return batches

# ================= Resilient Execution with Automatic Failover =================

def execute_with_failover(prompt: str, is_json: bool = True, preferred_provider: Optional[str] = None) -> tuple[str, dict]:
    """
    Executes an LLM prompt with automatic failover, retry with exponential backoff,
    and dynamic API key switching.

    Returns:
        (response_text, key_record_used)
    """
    active_keys = get_active_api_keys(provider=preferred_provider)
    if not active_keys and preferred_provider:
        # Fallback to any active provider
        active_keys = get_active_api_keys()

    if not active_keys:
        # Last-resort fallback: use environment variables directly
        if settings.GEMINI_API_KEY:
            logger.info("Using fallback environment GEMINI_API_KEY directly.")
            dummy_key = {
                "id": 0,
                "provider": "gemini",
                "label": "Environment Default",
                "api_key": settings.GEMINI_API_KEY,
                "model_name": settings.GEMINI_MODEL
            }
            res = call_gemini(settings.GEMINI_API_KEY, settings.GEMINI_MODEL, prompt, is_json=is_json)
            return res, dummy_key
        raise LLMProviderError("No active AI provider API keys available and no environment key configured.")

    attempted_keys = set()
    errors_encountered = []

    for key in active_keys:
        key_id = key["id"]
        if key_id in attempted_keys:
            continue
        attempted_keys.add(key_id)

        # Retry loop for transient issues with this specific key
        max_transient_retries = 2
        for attempt in range(max_transient_retries):
            try:
                logger.info(f"Executing prompt via key #{key_id} ({key['label']}, provider={key['provider']}, model={key['model_name']})")
                result_text = call_provider(key, prompt, is_json=is_json)
                
                # Success: record usage stats
                if key_id > 0:
                    record_api_key_usage(key_id, success=True)
                return result_text, key

            except QuotaExceededError as e:
                logger.warning(f"Quota/rate limit on key #{key_id} ({key['label']}): {e}. Placing on 90s cooldown and failing over.")
                if key_id > 0:
                    set_api_key_cooldown(key_id, seconds=90, error_msg=str(e))
                errors_encountered.append(f"Key #{key_id} 429: {str(e)[:100]}")
                break  # Break out of retry loop to switch to next key immediately

            except InvalidKeyError as e:
                logger.error(f"Invalid API key #{key_id} ({key['label']}): {e}. Disabling key and failing over.")
                if key_id > 0:
                    update_api_key(key_id, {"is_active": False})
                    record_api_key_usage(key_id, success=False, error_msg=str(e))
                errors_encountered.append(f"Key #{key_id} 401/403: {str(e)[:100]}")
                break

            except ContextWindowExceededError as e:
                logger.error(f"Context window exceeded on key #{key_id}: {e}")
                errors_encountered.append(f"Context error: {str(e)[:100]}")
                raise  # Must be handled by batch splitter in pipeline

            except TransientServerError as e:
                backoff = (attempt + 1) * 2
                logger.warning(f"Transient error on key #{key_id} (attempt {attempt + 1}/{max_transient_retries}): {e}. Backing off {backoff}s...")
                time.sleep(backoff)
                if attempt == max_transient_retries - 1:
                    if key_id > 0:
                        record_api_key_usage(key_id, success=False, error_msg=str(e))
                    errors_encountered.append(f"Key #{key_id} 5xx: {str(e)[:100]}")

            except Exception as e:
                logger.error(f"Unexpected error executing key #{key_id}: {e}")
                if key_id > 0:
                    record_api_key_usage(key_id, success=False, error_msg=str(e))
                errors_encountered.append(f"Key #{key_id} err: {str(e)[:100]}")
                break

    # If all keys failed
    raise LLMProviderError(f"All available API keys ({len(active_keys)}) failed. Errors: {'; '.join(errors_encountered)}")
