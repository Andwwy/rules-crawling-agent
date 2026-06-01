"""Perplexity Agent API as the four-axis taxonomy judge (prototype routing).

v0.4 taxonomy (replaces v0.3's seven single-valued enums):
    prerequisites           — TEXT[]  what info/context is needed to enforce
    enforcement_mechanisms  — TEXT[]  how compliance could be checked
    triggers                — TEXT[]  when the rule should fire / be checked
    ambiguity_level         — enum (none/low/medium/high) + ambiguity_notes

The first three axes are free-form lists (the labeling UI is a multi-line list,
no enum lock-in); only ambiguity_level is enum-constrained, so it's the one axis
read from the DB at boot to stay schema-driven.

`classify()` returns a JudgeDecision that maps 1:1 onto a rule_llm_decision row
(append-only history): it captures the prompt, the raw response, parse_ok +
parse_error, the parsed axes, and ops metadata (latency, tokens). A malformed
model reply is NOT an exception — it's a stored row with parse_ok=false. Only
transport failures (after llm.py's retries) propagate.

Routing note: see src/llm.py — Anthropic models are routed through Perplexity so
the prototype needs a single key. Pre-prod reverts to the Anthropic SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb

from .config import JUDGE_MAX_OUTPUT_TOKENS, JUDGE_MODEL, JUDGE_PROMPT_VERSION
from .db import enum_values
from .llm import agent_complete, parse_json_object


@dataclass
class JudgeDecision:
    """Maps onto one rule_llm_decision row. Parsed axes are None when
    parse_ok is False."""
    prompt_messages: list[dict]
    raw_response: str
    parse_ok: bool
    parse_error: str | None = None
    prerequisites: list[str] | None = None
    enforcement_mechanisms: list[str] | None = None
    triggers: list[str] | None = None
    ambiguity_level: str | None = None
    ambiguity_notes: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


_EXAMPLE = {
    "prerequisites": ["Repo access", "Diff"],
    "enforcement_mechanisms": ["bash", "llm_judge"],
    "triggers": ["pre_commit"],
    "ambiguity_level": "low",
    "ambiguity_notes": "Clear action, but 'relevant' tests is open to interpretation.",
    "confidence": 0.72,
    "rationale": "Triggered obligation checkable by running the test suite before a commit.",
}


def _build_prompt(ambiguity_levels: list[str]) -> str:
    levels = ", ".join(ambiguity_levels) if ambiguity_levels else "none, low, medium, high"
    return (
        "You classify ONE rule extracted from an agent-instruction file, along "
        "four axes. Return ONLY a JSON object — no prose, no markdown fences.\n\n"
        "Axes:\n"
        "  prerequisites: list of strings — what information/context is needed to "
        "ENFORCE this rule (e.g. [\"No information\"], [\"Bash\"], "
        "[\"Repo access\", \"Diff\"]). Use [\"No information\"] when nothing is needed.\n"
        "  enforcement_mechanisms: list of strings — how compliance could be "
        "checked (e.g. [\"linter\"], [\"regex\"], [\"llm_judge\"], [\"bash\"], "
        "[\"human_review\"]).\n"
        "  triggers: list of strings — when the rule should fire / be checked "
        "(e.g. [\"session_init\"], [\"pre_commit\"], [\"post_exec\"], "
        "[\"final_output\"], [\"settings.json\"]).\n"
        f"  ambiguity_level: EXACTLY one of [{levels}] — how much interpretation "
        "the rule needs before an actor can apply it.\n"
        "  ambiguity_notes: one sentence on the ambiguity, or \"\" if none.\n"
        "  confidence: float in [0, 1].\n"
        "  rationale: REQUIRED — one sentence (<= 200 chars) justifying why THIS "
        "rule gets these axes; the per-rule reason a human reviewer would read.\n\n"
        "The three list axes are free-form, but prefer short lowercase snake_case "
        "tokens and reuse the same token across rules when the concept matches. "
        "Lists must be non-empty (use [\"No information\"] / [\"none\"] when "
        "nothing genuinely applies).\n\n"
        "Output schema example:\n" + json.dumps(_EXAMPLE, indent=2)
    )


def _as_str_list(value) -> list[str] | None:
    """Coerce a value into a clean list[str]; None if there's nothing usable."""
    if isinstance(value, list):
        out = [str(x).strip() for x in value if str(x).strip()]
        return out or None
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return None


class Judge:
    def __init__(self, conn: duckdb.DuckDBPyConnection, model: str | None = None):
        # `model` overrides the configured JUDGE_MODEL (the admin UI's per-run
        # judge picker passes it through). The chosen id is written to each
        # rule_llm_decision row via the `model_id` property below.
        self._model = model or JUDGE_MODEL
        self._ambiguity_levels = enum_values(conn, "ambiguity_level")
        self._allowed_ambiguity = set(self._ambiguity_levels)
        self.system_prompt = _build_prompt(self._ambiguity_levels)

    def classify(self, rule_text: str, source_kind: str) -> JudgeDecision:
        user = f"source_kind: {source_kind}\nrule: {rule_text}"
        prompt_messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]
        # The Agent API takes a single `input` string (no separate system param),
        # so inline both with explicit markers.
        prompt = (
            f"SYSTEM:\n{self.system_prompt}\n\n"
            f"USER:\n{user}\n\n"
            "Respond with ONLY the JSON object."
        )
        result = agent_complete(
            prompt, model=self._model, max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS
        )
        decision = JudgeDecision(
            prompt_messages=prompt_messages,
            raw_response=result.text,
            parse_ok=False,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        try:
            obj = parse_json_object(result.text)
        except json.JSONDecodeError as e:
            decision.parse_error = str(e)
            return decision

        level = obj.get("ambiguity_level")
        if level not in self._allowed_ambiguity:
            level = None
        confidence = obj.get("confidence")
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        notes = obj.get("ambiguity_notes")
        rationale = obj.get("rationale")

        decision.parse_ok = True
        decision.prerequisites = _as_str_list(obj.get("prerequisites"))
        decision.enforcement_mechanisms = _as_str_list(obj.get("enforcement_mechanisms"))
        decision.triggers = _as_str_list(obj.get("triggers"))
        decision.ambiguity_level = level
        decision.ambiguity_notes = notes.strip() if isinstance(notes, str) and notes.strip() else None
        decision.confidence = confidence
        decision.rationale = rationale.strip()[:1000] if isinstance(rationale, str) and rationale.strip() else None
        return decision

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def prompt_version(self) -> str:
        return JUDGE_PROMPT_VERSION
