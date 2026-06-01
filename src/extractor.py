"""LLM two-pass rule extractor (always-on).

Implements `extraction_guide.md`: one Perplexity Agent call per file that
(1) segments the file into source blocks (semantically coherent spans) and
(2) extracts the atomic, normalized rules within each block, each with a short
`reason` (WHY the span is a rule) and a minimal 1-indexed line range.

Output shape (consumed by crawl.py):
    Extraction(blocks=[ExtractedBlock…], rules=[ExtractedRule…])
Each rule's `block_index` points into `blocks` (or None when the model didn't
attach it), giving the 1-block→N-rule relation the v0.4 schema persists as
source_block ← rule.source_block_id.

This replaces the v0.3 regex heuristics wholesale. The schema doesn't care
which extractor produced a row — rule.extractor_version
(config.EXTRACTOR_PROMPT_VERSION) distinguishes versions later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import EXTRACTOR_MAX_LINES, EXTRACTOR_MAX_OUTPUT_TOKENS, EXTRACTOR_MODEL
from .llm import agent_complete, parse_json_object


# Block kinds the prompt is allowed to emit (free-form TEXT in the schema; we
# keep the set small and guide-aligned for consistent admin-UI display).
_BLOCK_KINDS = frozenset({"heading_section", "paragraph", "list", "code"})

# rule_type values (mirrors the rule_type enum in the schema). Anything the model
# returns outside this set is coerced to 'rule'.
_RULE_TYPES = frozenset({"rule", "context"})


@dataclass
class ExtractedBlock:
    """One segmented source unit (→ a source_block row)."""
    kind: str | None
    line_start: int      # 1-indexed, against the original file
    line_end: int


@dataclass
class ExtractedRule:
    """One extracted span (→ a rule row). `text` is normalized to a directive
    (rule_type='rule') or a concise context statement (rule_type='context'); the
    line range still points at the verbatim source. `execution_guidance` carries
    the "how" the source pairs with a rule (commands/steps/caveats), kept beside
    the directive rather than folded into it."""
    text: str
    line_start: int
    line_end: int
    section_anchor: str | None      # nearest enclosing heading
    extraction_reason: str | None   # WHY this span is a rule/context (≤ ~120 chars)
    block_index: int | None         # index into Extraction.blocks, or None
    rule_type: str = "rule"         # 'rule' (directive) | 'context' (project/action info)
    execution_guidance: str | None = None  # the "how" paired with a rule, or None


@dataclass
class Extraction:
    blocks: list[ExtractedBlock]
    rules: list[ExtractedRule]
    # Usage from the underlying Agent call, surfaced so the crawl loop can price
    # the extractor's spend (None on the no-API empty-file short-circuit).
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


# -----------------------------------------------------------------------------
# Prompt — a faithful, condensed transcription of extraction_guide.md (§1–§5).
# Bump config.EXTRACTOR_PROMPT_VERSION whenever this text or the output contract
# changes; it's written to rule.extractor_version.
# -----------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You extract atomic RULES and salient project/action CONTEXT from one \
agent-instruction file (CLAUDE.md, AGENTS.md, .cursorrules, .cursor/rules/*.mdc, \
SKILL.md, command/agent files, etc.). Tag every extracted span with a rule_type.

A RULE (rule_type="rule") is a single, self-contained, NORMATIVE directive — one \
statement of what an actor MUST / MUST NOT / SHOULD / MAY / is conditionally \
required to do. The actor is usually the agent, but may be the user, system, or \
organization. Three tests, ALL required:
  (N) Normative — prescribes or constrains behavior; not a mere fact or a label.
  (S) Self-contained — expresses ONE directive, checkable on its own.
  (A) Actionable — some behavior complies (it need NOT be auto-checkable; "Write \
clean code" is a valid, if vague, rule).

CONTEXT (rule_type="context") is project/action information an agent would RELY \
ON while working but that is not itself a directive: the tech stack, repo/dir \
structure, domain facts, environment, or what a tool/command/action does. It is \
extracted and TAGGED (not dropped) so labelers see it distinctly from rules. \
A statement that changes what the agent must DO is a rule; one it merely USES is \
context. When unsure between rule and context, prefer "rule" if any modality \
(must/should/never) is present, else "context".

Work in TWO passes.

PASS 1 — segment the file into SOURCE BLOCKS (semantically coherent spans):
  - A markdown heading (#, ##, ...) OR a standalone bold label (**...:**) anchors \
a block: the heading and everything beneath it, up to the next equal-or-higher \
heading.
  - A heading-delimited list is ONE block, not one block per item.
  - In a region with NO enclosing heading, split on blank lines: each paragraph \
or standalone line is its own block.
  - A fenced code block belongs to the block of the heading/paragraph that \
introduces it.
  Every content line belongs to exactly one block. A block may yield 0, 1, or \
many rules.

PASS 2 — extract the atomic rules WITHIN each block. For each candidate line:
  1. Heading / blank / badge / boilerplate / license / changelog / TOC -> SKIP. \
EXCEPTION (assertion label): a full clause stating a constraint, even ending in \
':' and introducing a block (e.g. "**Subconscious runs LOCAL to the agent:**"), \
IS a rule -> extract + normalize. A noun-phrase topic label \
("**PR Creation Checklist:**", "**Key principles:**") is a heading -> SKIP; its \
children are the rules. Discriminator = behavioral fork: a title has none, an \
assertion does.
  2. Deterministic config / YAML frontmatter (model:, tools:, allowed-tools:) -> \
SKIP. EXCEPTION: a single-line `description:` in a SKILL.md / agent / command \
file IS a rule.
  3. A bare file/path reference list item ("- src/api/routes.ts") -> SKIP.
  4. A code fence -> SKIP its commands/structure, but EXTRACT a directive COMMENT \
or a CONSTRAINT ANNOTATION inside it: "# Prefer .venv; fall back to venv" -> rule; \
"workingDirectory (stored property, NOT derived from tmux)" -> rule. SKIP plain \
type/format descriptions ("index (0 for primary session)").
  5. Non-directive content — classify, don't blanket-skip. A fact that sets a \
behavioral expectation is a RULE (normalize it): "We use 2-space indentation." \
-> rule. SUBSTANTIVE project/action context an agent would rely on — tech stack, \
repo/dir structure, domain facts, environment, what a tool/command does \
("The CI runs on GitHub Actions.", "Browse logs with `hermes logs`.") -> extract \
as type "context" (lightly normalized, kept close to the source). SKIP only the \
bare "why" / rationale with no actionable content, marketing prose, and trivia. \
Test: would an agent doing a relevant task USE this? changes what it must DO -> \
rule; merely informs -> context; neither -> skip.
  6. A navigation pointer ("Read run_agent.py", "See ADDING_A_PLATFORM.md.") -> \
EXTRACT (usually weak / high-ambiguity, still a directive). Keep any attached \
condition.
  7. A line packing MULTIPLE independently-checkable actions -> SPLIT into one \
rule per action ("Run tests and lint before committing." -> two rules). A single \
action over a SET stays ONE rule ("Never commit secrets, API keys, or \
credentials."). Do NOT over-split coherent directives.

NORMALIZE rule_text — a RULE becomes a standalone directive; CONTEXT becomes a \
concise standalone statement of the fact:
  - Already imperative/modal (must/should/never/may): keep the wording; only \
strip bullet markers (-, *, 1.) and surrounding whitespace.
  - Declarative / implied / fragment: rewrite into the directive it implies \
("Functions are named in snake_case." -> "Use snake_case for function names."). \
Context: state the fact plainly, do NOT force it into a directive.
  - Preserve meaning EXACTLY: keep modality strength (should != must), conditions \
(if ...), scope, exceptions, every substantive noun, and a non-default actor. Do \
NOT add specificity, drop conditions, or invent detail.
  - Join multi-line bullets into one rule_text.

EXECUTION GUIDANCE — "execution_guidance": when the source pairs a RULE with \
concrete how-to-comply (commands to run, ordered steps, caveats, or a worked \
example that demonstrates compliance), put a concise version there. Keep it WITH \
the rule: do NOT fold it into rule_text and do NOT discard it. Use null when a \
rule has no attached guidance; context rows are normally null.

LINE RANGES (1-indexed, using the "N|" numbers in the SOURCE below):
  - The minimal span containing the rule; exclude the heading line above it and \
trailing blank lines.
  - A one-line rule has line_start == line_end; a wrapped bullet spans its \
continuation lines.
  - When a compound line splits into several rules, each split rule keeps the \
SAME line range (the original line).
  - Each rule's range MUST fall inside its block's range.

For each item, give a `reason`: <= 120 chars, WHY this span is a rule or context \
/ which case applies ("Obligation, triggered" / "Implied fact, normalized" / \
"Directive comment in fence" / "Navigation pointer" / "Assertion label" / \
"Project context, agent relies on it").

OUTPUT — return ONLY this JSON object. No prose, no markdown fences.
{
  "source_blocks": [
    {"index": 0, "kind": "heading_section", "line_start": 1, "line_end": 9}
  ],
  "rules": [
    {"block_index": 0, "rule_type": "rule", "rule_text": "The agent must inspect relevant files before making code changes.", "execution_guidance": null, "line_start": 3, "line_end": 3, "section_anchor": "Agent Instructions", "reason": "Obligation, triggered"},
    {"block_index": 0, "rule_type": "rule", "rule_text": "Run the test suite before committing.", "execution_guidance": "Run `npm test`; keep coverage >= 80%.", "line_start": 5, "line_end": 6, "section_anchor": "Agent Instructions", "reason": "Obligation + attached how-to"},
    {"block_index": 0, "rule_type": "context", "rule_text": "The project is a Python monorepo with packages under packages/.", "execution_guidance": null, "line_start": 8, "line_end": 8, "section_anchor": "Agent Instructions", "reason": "Project context, agent relies on it"}
  ]
}
"rule_type" is "rule" (a normative directive) or "context" (project/action info \
the agent relies on). "execution_guidance" is the attached how-to string, or \
null. "kind" is one of: heading_section, paragraph, list, code. "section_anchor" \
is the nearest enclosing heading text, or null. If the file has no rules or \
context, return the blocks you found and "rules": []."""


def _number_lines(lines: list[str]) -> str:
    """Render the source with 1-indexed "N| " prefixes so the model can cite
    exact line numbers. Width tracks the line count for clean alignment."""
    width = max(4, len(str(len(lines))))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


def _coerce_int(value, lo: int, hi: int) -> int | None:
    """Best-effort int in [lo, hi]; None if unparseable/out of range after clamp
    can't recover it (negatives, non-numeric)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < lo:
        n = lo
    if n > hi:
        n = hi
    return n


def extract(raw: str, kind: str | None = None, model: str | None = None,
            guidance_addendum: str | None = None) -> Extraction:
    """One Agent call → (blocks, rules). Returns empty on an empty/blank file
    without calling the API. `model` overrides the configured EXTRACTOR_MODEL
    (the admin UI's per-run extractor picker passes it through). Transport and
    JSON-parse failures propagate; the crawl loop logs them per-file
    (file_failed) and continues.

    `guidance_addendum` is the human-feedback-derived guidance produced by the
    admin UI's Revise flow (stored in crawl_settings, threaded down through
    crawl._process_file). When non-empty it is appended to the frozen
    _SYSTEM_PROMPT under a clearly-labeled header, so the base prompt stays the
    source of truth and the addendum only adds — never silently rewrites — it."""
    model = model or EXTRACTOR_MODEL
    lines_all = raw.splitlines()
    if not any(line.strip() for line in lines_all):
        return Extraction(blocks=[], rules=[])

    truncated = len(lines_all) > EXTRACTOR_MAX_LINES
    lines = lines_all[:EXTRACTOR_MAX_LINES]
    n_lines = len(lines)

    header = f"This file's kind is: {kind}.\n" if kind else ""
    if truncated:
        header += (
            f"NOTE: only the first {n_lines} lines are shown (file is "
            f"{len(lines_all)} lines); extract from what is shown.\n"
        )
    system_prompt = _SYSTEM_PROMPT
    if guidance_addendum and guidance_addendum.strip():
        system_prompt = (
            f"{_SYSTEM_PROMPT}\n\n"
            "ADDITIONAL GUIDANCE (learned from labeler feedback — apply ALONGSIDE "
            "the rules above; it refines, never overrides, them):\n"
            f"{guidance_addendum.strip()}"
        )
    prompt = (
        f"SYSTEM:\n{system_prompt}\n\n"
        f"USER:\n{header}SOURCE (numbered):\n{_number_lines(lines)}\n\n"
        "Respond with ONLY the JSON object."
    )

    result = agent_complete(
        prompt, model=model, max_output_tokens=EXTRACTOR_MAX_OUTPUT_TOKENS
    )
    obj = parse_json_object(result.text)

    # --- Blocks: build a list + a map from the model's reported index to its
    #     position in our list (positions are what rules reference downstream). ---
    blocks: list[ExtractedBlock] = []
    index_to_pos: dict[int, int] = {}
    for raw_block in obj.get("source_blocks", []) or []:
        if not isinstance(raw_block, dict):
            continue
        ls = _coerce_int(raw_block.get("line_start"), 1, n_lines)
        le = _coerce_int(raw_block.get("line_end"), 1, n_lines)
        if ls is None or le is None:
            continue
        if le < ls:
            ls, le = le, ls
        kind_val = raw_block.get("kind")
        if kind_val not in _BLOCK_KINDS:
            kind_val = None
        reported = _coerce_int(raw_block.get("index"), 0, 1_000_000)
        pos = len(blocks)
        blocks.append(ExtractedBlock(kind=kind_val, line_start=ls, line_end=le))
        if reported is not None and reported not in index_to_pos:
            index_to_pos[reported] = pos

    # --- Rules ---
    rules: list[ExtractedRule] = []
    for raw_rule in obj.get("rules", []) or []:
        if not isinstance(raw_rule, dict):
            continue
        text = (raw_rule.get("rule_text") or "").strip()
        if len(text) < 3:
            continue
        ls = _coerce_int(raw_rule.get("line_start"), 1, n_lines)
        le = _coerce_int(raw_rule.get("line_end"), 1, n_lines)
        if ls is None or le is None:
            continue
        if le < ls:
            ls, le = le, ls
        anchor = raw_rule.get("section_anchor")
        anchor = anchor.strip() if isinstance(anchor, str) and anchor.strip() else None
        reason = raw_rule.get("reason")
        reason = reason.strip()[:500] if isinstance(reason, str) and reason.strip() else None
        rtype = raw_rule.get("rule_type")
        rtype = rtype.strip().lower() if isinstance(rtype, str) else ""
        if rtype not in _RULE_TYPES:
            rtype = "rule"
        guidance = raw_rule.get("execution_guidance")
        guidance = guidance.strip() if isinstance(guidance, str) and guidance.strip() else None
        block_pos = index_to_pos.get(_coerce_int(raw_rule.get("block_index"), 0, 1_000_000))
        rules.append(ExtractedRule(
            text=text,
            line_start=ls,
            line_end=le,
            section_anchor=anchor,
            extraction_reason=reason,
            block_index=block_pos,
            rule_type=rtype,
            execution_guidance=guidance,
        ))

    return Extraction(
        blocks=blocks,
        rules=rules,
        model=model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


# Convenience for callers/tests that want the parsed object echoed back as JSON.
def extraction_to_json(ex: Extraction) -> str:
    return json.dumps(
        {
            "source_blocks": [vars(b) for b in ex.blocks],
            "rules": [vars(r) for r in ex.rules],
        },
        indent=2,
    )
