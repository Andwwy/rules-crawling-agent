"""Feedback-driven extraction-guidance revision (admin UI "Revise" tab).

The labeling platform writes human feedback into the SAME MotherDuck database
the crawler owns. In practice labelers do two things that reach this DB: ADD
rules the extractor missed (optionally linked to a source line), and COMMENT on
existing rules. (The schema also has accept/reject/skip extraction labels, but
the live labeling platform doesn't produce them, so we ignore them here.)

This module aggregates that real feedback and asks an LLM to propose an UPDATED
version of the extraction-guidance DOCUMENT — the same doc shown/edited in the
Revise tab, a real file at guidance/guidance.md with append-only history in the
guidance_version table (see guidance_store.py). The admin UI shows the proposed
document as a green/red diff against the current one; on approval it writes the
file + a new version. The model returns the full document so the UI can diff it.

This module READS feedback and CALLS the model. The writes (saving an approved
or manually-edited version) happen via guidance_store.save_guidance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import EXTRACTOR_MODEL
from .db import conn
from .guidance_store import read_guidance
from .llm import agent_complete

__all__ = [
    "Feedback",
    "RevisionResult",
    "gather_feedback",
    "propose_revision",
]

# Per-category row cap on the feedback pulled into one revision. Keeps the
# meta-prompt bounded even on a large corpus; recurring patterns surface well
# within this many examples.
FEEDBACK_LIMIT = 60

# Output-token ceiling for the revision call. The model returns the FULL updated
# guidance document (the seed is ~5-6k tokens), so this is generous — a thin
# addendum-sized cap would truncate the doc mid-section.
REVISE_MAX_OUTPUT_TOKENS = 8000

# Sentinel markers framing the returned document. A delimiter (not JSON) keeps a
# long markdown doc with quotes/newlines robust to parse — no escaping needed.
_DOC_BEGIN = "===BEGIN GUIDANCE==="
_DOC_END = "===END GUIDANCE==="


# -----------------------------------------------------------------------------
# Feedback aggregation
# -----------------------------------------------------------------------------

@dataclass
class Feedback:
    """Aggregated labeler feedback (each list is capped at FEEDBACK_LIMIT).

    Only the signals the live labeling platform actually writes:
      • hand_added    — rules a human inserted that the extractor missed
      • rule_comments — free-text notes on existing rules
    """
    hand_added: list[tuple]      # (rule_text, kind, project, path, section, l_start, l_end)
    rule_comments: list[tuple]   # (body, rule_text, kind, handle)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "hand_added": len(self.hand_added),
            "rule_comments": len(self.rule_comments),
        }

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def gather_feedback(c, limit: int = FEEDBACK_LIMIT) -> Feedback:
    """Read the extraction-side feedback signals from the DB into a Feedback.
    Read-only. `c` is an open DuckDB connection."""
    # Hand-added rules — a human inserted a rule the extractor missed
    # (extraction_reason is NULL only for hand-added rows under extract-v2),
    # optionally linked to the source line range / section it belongs to.
    hand_added = c.execute(
        """
        SELECT r.rule_text, sf.kind,
               sp.owner || '/' || sp.name AS project, sf.path,
               r.section_anchor, r.line_start, r.line_end
          FROM rule r
          JOIN rules_file sf ON sf.id = r.rules_file_id
          JOIN source_project sp ON sp.id = sf.project_id
         WHERE r.extraction_reason IS NULL
         ORDER BY r.extracted_at DESC
         LIMIT ?
        """,
        [limit],
    ).fetchall()

    # Free-text comments on rules — the richest "what's wrong / what to watch
    # for" signal.
    rule_comments = c.execute(
        """
        SELECT rc.body, r.rule_text, sf.kind, l.handle
          FROM rule_comment rc
          JOIN rule r        ON r.id  = rc.rule_id
          JOIN rules_file sf ON sf.id = r.rules_file_id
          JOIN labeler l     ON l.id  = rc.labeler_id
         ORDER BY rc.created_at DESC
         LIMIT ?
        """,
        [limit],
    ).fetchall()

    return Feedback(
        hand_added=hand_added,
        rule_comments=rule_comments,
    )


# -----------------------------------------------------------------------------
# Prompt construction
# -----------------------------------------------------------------------------

def _clip(s: str | None, n: int = 240) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …"


def build_digest(fb: Feedback) -> str:
    """Render the aggregated feedback into a compact, model-readable digest."""
    out: list[str] = []
    if fb.hand_added:
        out.append("## RULES HUMANS ADDED (the extractor missed these — learn to catch them):")
        for rt, kind, project, path, section, l0, l1 in fb.hand_added:
            out.append(f"- [{kind}] {_clip(rt)}")
            where = f"{project}/{path}"
            if l0 is not None and l1 is not None:
                where += f":L{l0}-L{l1}"
            if section:
                where += f"  §{section}"
            out.append(f"    ↳ {where}")
    if fb.rule_comments:
        out.append("\n## COMMENTS on rules:")
        for body, rt, kind, _h in fb.rule_comments:
            out.append(f'- [{kind}] on "{_clip(rt, 120)}": {_clip(body)}')
    if not out:
        return "(no labeler feedback found)"
    return "\n".join(out)


_META_SYSTEM = f"""\
You maintain the EXTRACTION GUIDANCE document for an automated agent-rule \
extraction system. You are given the CURRENT document (markdown) and a digest \
of HUMAN FEEDBACK from labelers reviewing the extractor's output: rules humans \
ADDED that the extractor missed, and free-text COMMENTS on existing rules.

Produce an UPDATED version of the document that folds in what the feedback \
teaches — e.g. patterns the extractor is missing, distinctions to draw, wording \
to clarify. Requirements:
  - Make MINIMAL, TARGETED edits. Preserve everything still correct VERBATIM — \
do not reword, reorder, or drop sections the feedback doesn't touch.
  - Generalize from the feedback to the underlying guidance — do NOT paste \
specific rule texts, file paths, or project names into the document.
  - Prioritize RECURRING signals over one-off instances. If the feedback is too \
thin or noisy to justify any change, return the document UNCHANGED.
  - Keep the document's existing structure, headings, and markdown style.

Return EXACTLY this shape and nothing else:
<one to three sentences summarizing what you changed and why>
{_DOC_BEGIN}
<the complete updated document, markdown>
{_DOC_END}"""


def build_meta_prompt(current_doc: str, digest: str) -> str:
    """Assemble the full SYSTEM/USER prompt for the revision call."""
    return (
        f"SYSTEM:\n{_META_SYSTEM}\n\n"
        f"USER:\n"
        f"=== CURRENT GUIDANCE DOCUMENT ===\n"
        f"{current_doc or '(empty)'}\n\n"
        f"=== LABELER FEEDBACK DIGEST ===\n"
        f"{digest}\n\n"
        f"Return the summary line(s), then the complete document between the markers."
    )


def _parse_revision(text: str) -> tuple[str, str] | None:
    """Pull (summary, document) out of the model reply. None if the markers are
    missing/malformed (caller surfaces parse_ok=False + the raw text)."""
    i = text.find(_DOC_BEGIN)
    j = text.rfind(_DOC_END)
    if i == -1 or j == -1 or j < i:
        return None
    summary = text[:i].strip()
    document = text[i + len(_DOC_BEGIN):j].strip("\n")
    return summary, document


# -----------------------------------------------------------------------------
# Revision
# -----------------------------------------------------------------------------

@dataclass
class RevisionResult:
    new_content: str            # proposed full updated document ("" when parse failed)
    summary: str
    feedback: Feedback
    model: str
    parse_ok: bool
    parse_error: str | None = None
    raw_response: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


def propose_revision(model: str | None = None,
                     current_doc: str | None = None) -> RevisionResult:
    """Gather feedback, ask the model for an updated guidance document, and
    return the parsed proposal (NOT yet applied — the admin UI writes it on
    Approve). A malformed reply is returned with parse_ok=False and the raw
    text, never raised, so the UI can show what came back."""
    model = model or EXTRACTOR_MODEL
    with conn() as c:
        fb = gather_feedback(c)
    if current_doc is None:
        current_doc = read_guidance()

    digest = build_digest(fb)
    prompt = build_meta_prompt(current_doc, digest)
    result = agent_complete(prompt, model=model,
                            max_output_tokens=REVISE_MAX_OUTPUT_TOKENS)

    parsed = _parse_revision(result.text)
    if parsed is None:
        return RevisionResult(
            new_content="", summary="", feedback=fb, model=model,
            parse_ok=False,
            parse_error=f"missing {_DOC_BEGIN} / {_DOC_END} markers",
            raw_response=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    summary, document = parsed
    return RevisionResult(
        new_content=document,
        summary=summary,
        feedback=fb,
        model=model,
        parse_ok=True,
        raw_response=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
