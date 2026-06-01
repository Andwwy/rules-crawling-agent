"""Shared Perplexity Agent API client.

Both the LLM extractor (extractor.py) and the taxonomy judge (judge.py) call
Perplexity's Agent API with the same transport: one `input` string in, one
JSON value out. This module owns that transport — the HTTP client, the
OpenAI-Responses-shaped text extraction, token/latency capture, and the
tolerant JSON parsing both callers need — so the two don't duplicate it.

Routing note (inherited from the old judge.py):
    We route Anthropic models through Perplexity's Agent API so the prototype
    needs ONE paid key (PERPLEXITY_API_KEY) — Perplexity passes through
    `anthropic/claude-*` at no markup. Same model, same prompt, different
    transport. Pre-prod we'll revert to the Anthropic SDK; this module is the
    single seam to change.
"""

import json
import os
import re
import time
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


PERPLEXITY_BASE = "https://api.perplexity.ai"
AGENT_PATH = "/v1/agent"


@dataclass
class AgentResult:
    text: str                # concatenated output_text segments (the model's reply)
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


_client: httpx.Client | None = None


def _http() -> httpx.Client:
    """Lazy singleton — first use reads PERPLEXITY_API_KEY, so importing this
    module never requires the key (api_smoke / --stats / tests stay importable)."""
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=PERPLEXITY_BASE,
            headers={
                "Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}",
                "Content-Type": "application/json",
            },
            timeout=120,
        )
    return _client


def _extract_text(body: dict) -> str:
    """Perplexity's Agent API mirrors OpenAI's Responses shape:
       body.output[*].content[*] = {"type": "output_text", "text": "..."}
    Concatenate every output_text segment (usually one)."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    return "".join(parts)


def _usage(body: dict) -> tuple[int | None, int | None]:
    """(input_tokens, output_tokens) from the usage block, tolerating both the
    Responses naming (input_tokens/output_tokens) and the Chat naming
    (prompt_tokens/completion_tokens). Returns (None, None) if absent."""
    usage = body.get("usage") or {}
    inp = usage.get("input_tokens", usage.get("prompt_tokens"))
    out = usage.get("output_tokens", usage.get("completion_tokens"))
    return inp, out


# Retry only on transport errors. A malformed JSON body is deterministic —
# re-asking the model wastes tokens; the caller parses + records parse_ok.
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    reraise=True,
)
def agent_complete(prompt: str, model: str, max_output_tokens: int = 1024) -> AgentResult:
    """One Agent-API call. The endpoint takes a single `input` string (no
    separate system parameter), so callers inline any system instructions with
    explicit SYSTEM/USER markers."""
    t0 = time.monotonic()
    resp = _http().post(
        AGENT_PATH,
        json={"model": model, "input": prompt, "max_output_tokens": max_output_tokens},
    )
    resp.raise_for_status()
    body = resp.json()
    latency_ms = int((time.monotonic() - t0) * 1000)
    inp, out = _usage(body)
    return AgentResult(
        text=_extract_text(body),
        input_tokens=inp,
        output_tokens=out,
        latency_ms=latency_ms,
    )


# -----------------------------------------------------------------------------
# Tolerant JSON parsing — shared by both callers.
#   Models wrap JSON in ```json fences even when told not to, and sometimes
#   prepend a prose preamble. Strategy: direct parse → de-fenced parse →
#   greedy bracket match.
# -----------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _strip_fences(text: str) -> str:
    return _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text.strip()))


def parse_json_object(text: str) -> dict:
    """Parse a top-level JSON object, tolerating fences and prose preambles."""
    text = text.strip()
    for candidate in (text, _strip_fences(text)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    m = _JSON_OBJECT_RE.search(text)
    if m:
        return json.loads(m.group(0))  # last-chance; raises if still invalid
    raise json.JSONDecodeError("no JSON object found", text, 0)


def parse_json_array(text: str) -> list:
    """Parse a top-level JSON array, tolerating fences and prose preambles."""
    text = text.strip()
    for candidate in (text, _strip_fences(text)):
        try:
            arr = json.loads(candidate)
            if isinstance(arr, list):
                return arr
        except json.JSONDecodeError:
            pass
    m = _JSON_ARRAY_RE.search(text)
    if m:
        return json.loads(m.group(0))  # last-chance; raises if still invalid
    raise json.JSONDecodeError("no JSON array found", text, 0)
