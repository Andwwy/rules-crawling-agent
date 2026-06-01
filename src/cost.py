"""Model catalog + token-cost estimation for the runtime model picker.

The admin UI lets an operator choose which Perplexity Agent API model the
extractor and judge each run on (see admin_app.py "Models & spending cap").
This module is the single source of truth for:

  - AGENT_MODELS — the curated picker list (1 cheap + 1 premium per provider:
    Anthropic / OpenAI / Google), each with its `provider/model` id and price.
  - cost_usd()   — turn (model, input_tokens, output_tokens) into a USD estimate
    so the crawl loop can accumulate session spend and enforce a hard cap.

Prices are USD per MILLION tokens, transcribed from the Perplexity Agent API
models doc (https://docs.perplexity.ai/docs/agent-api/models) as of 2026-05.
They drift — treat the running total as an ESTIMATE, not a billing figure.
Update PRICING_AS_OF when you refresh them.

Gemini 3.1 Pro has tiered pricing (a higher rate above a 200k-token context).
Our prompts are far under that (extractor caps at ~600 source lines + 6k output
tokens; the judge is tiny), so we use the base (<=200k) tier for both Gemini
and accept a negligible under-count in the unlikely large-context case.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_AS_OF = "2026-05 (Perplexity Agent API docs)"


@dataclass(frozen=True)
class ModelInfo:
    id: str               # exact string passed in the Agent API "model" field
    label: str            # short UI label
    provider: str         # anthropic | openai | google
    tier: str             # cheap | premium
    input_per_mtok: float   # USD / 1M input tokens
    output_per_mtok: float  # USD / 1M output tokens


# Order = grouped by provider, cheap then premium. The first entry is also the
# pipeline default (mirrors config.EXTRACTOR_MODEL / JUDGE_MODEL).
AGENT_MODELS: list[ModelInfo] = [
    ModelInfo("anthropic/claude-haiku-4-5", "Claude Haiku 4.5", "anthropic", "cheap", 1.0, 5.0),
    ModelInfo("anthropic/claude-opus-4-7", "Claude Opus 4.7", "anthropic", "premium", 5.0, 25.0),
    ModelInfo("openai/gpt-5.4-nano", "GPT-5.4 nano", "openai", "cheap", 0.20, 1.25),
    ModelInfo("openai/gpt-5.5", "GPT-5.5", "openai", "premium", 5.0, 30.0),
    ModelInfo("google/gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", "google", "cheap", 0.25, 1.50),
    ModelInfo("google/gemini-3.1-pro-preview", "Gemini 3.1 Pro", "google", "premium", 2.0, 12.0),
]

MODEL_BY_ID: dict[str, ModelInfo] = {m.id: m for m in AGENT_MODELS}
MODEL_IDS: list[str] = [m.id for m in AGENT_MODELS]


def model_label(model_id: str) -> str:
    """UI-friendly name; falls back to the raw id for off-catalog models."""
    info = MODEL_BY_ID.get(model_id)
    return info.label if info else model_id


def cost_usd(model_id: str, input_tokens: int | None, output_tokens: int | None) -> float:
    """USD estimate for one call. Unknown models or missing token counts cost
    0.0 (we under-count rather than guess) — so the cap is conservative, never
    charging for usage we can't price."""
    info = MODEL_BY_ID.get(model_id)
    if info is None:
        return 0.0
    inp = (input_tokens or 0) / 1_000_000 * info.input_per_mtok
    out = (output_tokens or 0) / 1_000_000 * info.output_per_mtok
    return inp + out
