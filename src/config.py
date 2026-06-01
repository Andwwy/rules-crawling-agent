"""Model + pipeline constants used by extractor.py, judge.py, embedder.py,
crawl.py, and admin_app.py.

Single-key prototype: the extractor, judge, and embedder all route through
Perplexity (PERPLEXITY_API_KEY) — see src/llm.py for the routing rationale.
Pre-prod swaps (Anthropic SDK for the LLM calls, voyage-3 for embeddings) are
small and localized; the schema columns don't move.
"""

# -----------------------------------------------------------------------------
# Embeddings
# -----------------------------------------------------------------------------
# Must produce a 1024-d vector to fit rule.embedding's column.
# PROTOTYPE: Perplexity `pplx-embed-v1-0.6b`. PROD: "voyage-3" (also 1024-d).
EMBEDDING_MODEL = "pplx-embed-v1-0.6b"

# -----------------------------------------------------------------------------
# Extractor (LLM two-pass — see extraction_guide.md and extractor.py)
# -----------------------------------------------------------------------------
# Routed via the Perplexity Agent API, like the judge. Haiku keeps prototype
# cost down; bumping to `anthropic/claude-sonnet-4-5` measurably improves
# gray-zone recall (implied facts, fence comments, assertion labels) if needed.
EXTRACTOR_MODEL = "anthropic/claude-haiku-4-5"

# Bump whenever the extraction prompt/output contract changes — it's written to
# rule.extractor_version so rows stay attributable to the prompt that made them.
# extract-v2 = adds rule_type ('rule' vs 'context') + execution_guidance.
EXTRACTOR_PROMPT_VERSION = "extract-v2"

# Hard cap on how many source lines we send per file. Files over this are
# truncated (line numbers stay 1-indexed against the original; trailing rules
# are simply missed — the labeling "Add Rule" flow recovers them). Keeps a
# single Agent call within a sane token + latency budget.
EXTRACTOR_MAX_LINES = 600

# Output-token ceiling for the extractor call. A dense file can yield dozens of
# blocks + rules; give it room so the JSON isn't truncated mid-array.
EXTRACTOR_MAX_OUTPUT_TOKENS = 6000

# -----------------------------------------------------------------------------
# Judge (4-axis taxonomy — see judge.py)
# -----------------------------------------------------------------------------
# Routed via Perplexity Agent API (no markup over Anthropic direct).
JUDGE_MODEL = "anthropic/claude-haiku-4-5"

# v2 = the four-axis taxonomy (prerequisites / enforcement_mechanisms /
# triggers / ambiguity). v1 was the seven single-valued enums (pre-v0.4).
# v3 = requires a per-rule `rationale` justifying this rule's classification.
JUDGE_PROMPT_VERSION = "v3"

JUDGE_MAX_OUTPUT_TOKENS = 700

# Set False to skip judge calls during ingest (rules are still stored,
# rule_llm_decision rows are simply not written). Saves API spend while
# iterating on extractor/embedder. Flip to True when ready to classify.
JUDGE_ENABLED = False

# -----------------------------------------------------------------------------
# Admin / ops settings keys (crawl_settings key/value table)
# -----------------------------------------------------------------------------
# The Revise flow (admin UI) aggregates labeler feedback into a guidance
# addendum and stores it under this key. The extractor appends it to its frozen
# _SYSTEM_PROMPT at crawl time (threaded through crawl._process_file →
# extractor.extract). Empty/absent means "no addendum" — the base prompt runs
# unchanged. Shared by crawl.py, revise.py, and admin_app.py so the key string
# lives in exactly one place.
GUIDANCE_ADDENDUM_SETTING = "extraction_guidance_addendum"
