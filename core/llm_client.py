from __future__ import annotations
import asyncio
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

LLM_URL   = os.getenv("XNAV_LLM_URL",   "http://localhost:11434").rstrip("/")
LLM_MODEL = os.getenv("XNAV_LLM_MODEL", "mistral")
LLM_KEY   = os.getenv("XNAV_LLM_KEY",   "")


class LLMError(Exception):
    """Raised when the LLM transport fails after retries. Callers decide fallback."""


def is_openai_compat(url: str = LLM_URL) -> bool:
    """True if the URL looks like an OpenAI-compatible API (not Ollama)."""
    return any(x in url for x in ("openai.com", "openrouter.ai", "/v1"))


def is_configured() -> bool:
    """True if a usable LLM endpoint is configured."""
    if is_openai_compat(LLM_URL):
        return bool(LLM_KEY)
    # Local Ollama or similar — assume usable if URL is set
    return bool(LLM_URL)


def parse_json_response(raw: str) -> dict:
    """Extract JSON from a raw LLM response, handling markdown code fences."""
    text = raw.strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    return json.loads(text)


def _build_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if LLM_KEY:
        headers["Authorization"] = f"Bearer {LLM_KEY}"
    if "openrouter.ai" in LLM_URL:
        headers["HTTP-Referer"] = "https://github.com/xnav"
        headers["X-Title"] = "Xnav"
    return headers


async def call_llm(
    prompt: str,
    *,
    system: str | None = None,
    timeout: float = 60.0,
    max_tokens: int | None = None,
) -> str:
    """
    Call the configured LLM, returning the raw assistant content string.
    Raises LLMError on failure after retries.
    """
    headers = _build_headers()
    openai_compat = is_openai_compat(LLM_URL)

    async with httpx.AsyncClient(timeout=timeout) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                if openai_compat:
                    messages = []
                    if system:
                        messages.append({"role": "system", "content": system})
                    messages.append({"role": "user", "content": prompt})
                    payload: dict = {
                        "model": LLM_MODEL,
                        "messages": messages,
                        "temperature": 0.2,
                    }
                    if max_tokens:
                        payload["max_tokens"] = max_tokens
                    resp = await client.post(f"{LLM_URL}/chat/completions", headers=headers, json=payload)
                else:
                    # Ollama /api/generate has no role split — concatenate
                    full_prompt = f"{system}\n\n{prompt}" if system else prompt
                    payload = {"model": LLM_MODEL, "prompt": full_prompt, "stream": False}
                    if max_tokens:
                        payload["options"] = {"num_predict": max_tokens}
                    resp = await client.post(f"{LLM_URL}/api/generate", headers=headers, json=payload)

                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("LLM rate limited — retrying in %ds (attempt %d/3)", wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue

                if not resp.is_success:
                    raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")

                if openai_compat:
                    return resp.json()["choices"][0]["message"]["content"]
                return resp.json().get("response", "")

            except LLMError:
                raise
            except Exception as e:
                last_err = e
                logger.warning("LLM call attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                continue

        raise LLMError(f"LLM call failed after 3 attempts: {last_err}")
