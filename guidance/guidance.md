# Extraction guidance (`extraction.md`)

Status: canonical guidance, compiled 2026-06-01. This is the human-readable
companion to the operative extraction prompt — the `_SYSTEM_PROMPT` constant in
[`src/extractor.py`](../src/extractor.py). It aggregates, in one place:

1. **the two-pass extraction spec** the extractor actually runs (§§2–3),
2. the **extraction-time quality signals** that decide whether a candidate rule
   is worth keeping (§4, from `RESEARCH-static-rule-quality-metrics.md`),
3. the **equivalence / de-duplication** approach for collapsing rules that say
   the same thing (§5, from `RESEARCH-llm-judge-semantic-grouping.md`),
4. the **deferred decisions** that bound the scope (§6, from `DECISIONS.md`), and
5. the **runtime feedback loop** that lets labeler corrections refine the prompt
   without a code change (§7).

It supersedes the dangling `extraction_guide.md` references in `README.md` and the
two RESEARCH memos — that file is the spec this document reconstructs and extends.

> **Source of truth.** The executable spec is the `_SYSTEM_PROMPT` string in
> `src/extractor.py`; this doc tracks it in prose. When the prompt or output
> contract changes, bump `config.EXTRACTOR_PROMPT_VERSION` (written to
> `rule.extractor_version`) **and** update §§2–3 here in the same change, so rows
> stay attributable to the prompt that made them and the two never drift.

---

## 1. Where extraction sits in the pipeline

Per `README.md`, each crawl pass runs: **discover** trending agent projects →
**segment + extract** each source file → **embed** each rule (1024-d) →
*(optional)* **classify** on the 4-axis judge taxonomy → **write** to the v0.4
DuckDB/MotherDuck schema → **serve** the admin UI.

Extraction is step two: **one LLM call per file** that turns a raw agent-rule
file (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `SKILL.md`,
command/agent files, `llms.txt`, …) into two row types the schema persists:

- `source_block` — a semantically coherent span of the file (a heading section,
  paragraph, list, or fenced code block).
- `rule` — an atomic, normalized span tagged `rule_type` (`rule` | `context`),
  each carrying a `reason`, an optional `execution_guidance`, and a back-reference
  to its source block (`rule.source_block_id`, the 1-block → N-rule relation).

Only rule-bearing blocks are persisted. This replaced the v0.3 regex heuristics
wholesale; the schema does not care which extractor produced a row.

---

## 2. What we extract: RULE vs CONTEXT

Every extracted span is tagged with a `rule_type`.

**RULE** (`rule_type="rule"`) — a single, self-contained, **normative** directive:
one statement of what an actor MUST / MUST NOT / SHOULD / MAY / is conditionally
required to do. The actor is usually the agent, but may be the user, system, or
organization. Three tests, **all** required:

- **(N) Normative** — prescribes or constrains behavior; not a mere fact or label.
- **(S) Self-contained** — expresses ONE directive, checkable on its own.
- **(A) Actionable** — some behavior complies. It need not be auto-checkable:
  "Write clean code" is a valid, if vague, rule.

**CONTEXT** (`rule_type="context"`) — project/action information an agent would
**rely on** while working but that is not itself a directive: tech stack, repo/dir
structure, domain facts, environment, or what a tool/command/action does. It is
extracted and **tagged** (not dropped) so labelers see it distinctly from rules.

**Discriminator.** A statement that changes what the agent must *do* is a rule;
one it merely *uses* is context. When unsure, prefer `rule` if any modality
(must / should / never) is present, else `context`.

---

## 3. The two-pass algorithm

### Pass 1 — segment the file into SOURCE BLOCKS

- A markdown heading (`#`, `##`, …) **or** a standalone bold label (`**…:**`)
  anchors a block: the heading and everything beneath it, up to the next
  equal-or-higher heading.
- A heading-delimited list is **one** block, not one block per item.
- In a region with **no** enclosing heading, split on blank lines: each paragraph
  or standalone line is its own block.
- A fenced code block belongs to the block of the heading/paragraph that
  introduces it.

Every content line belongs to exactly one block. A block may yield 0, 1, or many
rules. `kind` is one of `heading_section` | `paragraph` | `list` | `code`.

### Pass 2 — extract the atomic rules within each block

For each candidate line, apply this decision procedure in order:

1. **Heading / blank / badge / boilerplate / license / changelog / TOC → SKIP.**
   - *Exception (assertion label):* a full clause stating a constraint, even
     ending in `:` and introducing a block (e.g. `**Subconscious runs LOCAL to
     the agent:**`), **is a rule** → extract + normalize.
   - A noun-phrase topic label (`**PR Creation Checklist:**`, `**Key
     principles:**`) is a heading → SKIP; its children are the rules.
   - *Discriminator = behavioral fork:* a title has none, an assertion does.
2. **Deterministic config / YAML frontmatter** (`model:`, `tools:`,
   `allowed-tools:`) **→ SKIP.** *Exception:* a single-line `description:` in a
   `SKILL.md` / agent / command file **is a rule**.
3. **A bare file/path reference list item** (`- src/api/routes.ts`) **→ SKIP.**
4. **A code fence → SKIP** its commands/structure, **but EXTRACT** a directive
   **comment** or a **constraint annotation** inside it: `# Prefer .venv; fall
   back to venv` → rule; `workingDirectory (stored property, NOT derived from
   tmux)` → rule. SKIP plain type/format descriptions (`index (0 for primary
   session)`).
5. **Non-directive content — classify, don't blanket-skip.**
   - A **fact that sets a behavioral expectation** is a RULE (normalize it):
     "We use 2-space indentation." → rule.
   - **Substantive project/action context** an agent would rely on — tech stack,
     repo/dir structure, domain facts, environment, what a tool/command does
     ("The CI runs on GitHub Actions.", "Browse logs with `hermes logs`.") →
     extract as type **`context`** (lightly normalized, kept close to the source).
   - SKIP only the bare "why" / rationale with no actionable content, marketing
     prose, and trivia.
   - *Test:* would an agent doing a relevant task USE this? Changes what it must
     DO → rule; merely informs → context; neither → skip.
6. **A navigation pointer** ("Read run_agent.py", "See ADDING_A_PLATFORM.md.")
   **→ EXTRACT** (usually weak / high-ambiguity, still a directive). Keep any
   attached condition.
7. **A line packing MULTIPLE independently-checkable actions → SPLIT** into one
   rule per action ("Run tests and lint before committing." → two rules). A
   single action over a **set** stays ONE rule ("Never commit secrets, API keys,
   or credentials."). Do **not** over-split coherent directives.

### Normalization (`rule_text`)

A RULE becomes a standalone directive; CONTEXT becomes a concise standalone
statement of the fact.

- **Already imperative/modal** (must / should / never / may): keep the wording;
  only strip bullet markers (`-`, `*`, `1.`) and surrounding whitespace.
- **Declarative / implied / fragment:** rewrite into the directive it implies
  ("Functions are named in snake_case." → "Use snake_case for function names.").
  Context: state the fact plainly; do **not** force it into a directive.
- **Preserve meaning exactly:** keep modality strength (`should` ≠ `must`),
  conditions (`if …`), scope, exceptions, every substantive noun, and a
  non-default actor. Do **not** add specificity, drop conditions, or invent detail.
- **Join multi-line bullets** into one `rule_text`.

### Execution guidance (`execution_guidance`)

When the source pairs a RULE with concrete **how-to-comply** — commands to run,
ordered steps, caveats, or a worked example that demonstrates compliance — put a
concise version in `execution_guidance`. Keep it **with** the rule: do not fold it
into `rule_text` and do not discard it. Use `null` when a rule has no attached
guidance; context rows are normally `null`.

### Line ranges (1-indexed, against the `N|` numbers in the source)

- The **minimal** span containing the rule; exclude the heading line above it and
  trailing blank lines.
- A one-line rule has `line_start == line_end`; a wrapped bullet spans its
  continuation lines.
- When a compound line splits into several rules, each split rule keeps the
  **same** line range (the original line).
- Each rule's range **must** fall inside its block's range.

### The `reason` field

≤ 120 chars, stating WHY this span is a rule or context / which case applies —
e.g. "Obligation, triggered" / "Implied fact, normalized" / "Directive comment in
fence" / "Navigation pointer" / "Assertion label" / "Project context, agent relies
on it".

### Output contract

Return **only** this JSON object (no prose, no markdown fences):

```json
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
```

- `rule_type` ∈ {`rule`, `context`}; `kind` ∈ {`heading_section`, `paragraph`,
  `list`, `code`}; `section_anchor` is the nearest enclosing heading text, or
  `null`. If the file has no rules or context, return the blocks you found and
  `"rules": []`.

### Operational caps (see `src/config.py`)

- `EXTRACTOR_MAX_LINES` (600) — files over this are truncated; line numbers stay
  1-indexed against the original and trailing rules are simply missed (the
  labeling "Add Rule" flow recovers them).
- `EXTRACTOR_MAX_OUTPUT_TOKENS` (6000) — a dense file can yield dozens of blocks +
  rules; room so the JSON isn't truncated mid-array.
- The extractor is **always on** (one call per file, the only way rules are
  produced). The judge ships disabled (`JUDGE_ENABLED=False`).

---

## 4. What "good" looks like — static quality signals

Companion memo: `RESEARCH-static-rule-quality-metrics.md`. These score a rule
**before any trajectory feedback exists** — using only the file text, its
structure, and (optionally) the live repo. They feed two choices: what Pass 2
emits as a `rule` vs. ignores, and what the judge sees on its axes. None require
running an agent.

A "good" rule at extraction time is one a downstream consumer can act on with low
ambiguity: an agent can derive a **single decision** from it; it references
**something that still exists**; it does **not duplicate** or **contradict**
another rule in the file; and it carries **enough specificity** to separate a
compliant from a non-compliant action.

### 4.1 Per-rule intrinsic (text-only — cheapest, apply first)

- **Imperative force** — count modal/imperative markers (`must`, `should`,
  `never`, `always`, `do not`, `avoid`, `prefer`, `required`, `forbidden`, plus
  sentence-initial imperative verbs). Use as a **filter, not a score**: zero
  markers → demote out of the rule stream (it's descriptive prose). This is the
  basis of Pass 2 case 5.
- **Concrete grounding** — count concrete tokens (file paths, function/method
  names, version numbers, command names, env vars, error codes), normalized by
  rule length. Correlates strongly with actionability; the bottom decile is almost
  always boilerplate.
- **Atomicity** — count distinct verb phrases + conjunctions (`and`, `;`,
  `, also`). High count → the compound-split logic of Pass 2 case 7 should fire.
- **Falsifiability** — a yes/no judge rubric: "Could you write a check that
  decides whether this rule was followed?" Complements `ambiguity_level` (a rule
  can be clear-intent yet unfalsifiable: "keep the agent helpful and honest").
- **Specificity entropy** — TF-IDF over the rule with the rest of the file as
  background; distinctive rules have high-IDF tokens, boilerplate ("be polite",
  "follow best practices") near-zero.

### 4.2 Cross-rule structure (needs the rest of the file/corpus)

- **Intra-file redundancy** — pairwise cosine over rules from the same file
  (reuses `rule.embedding`, so cost is zero). In-file `cosine > 0.93` are nearly
  always genuine duplicates (looser than the cross-file `[0.85, 0.93]` band of
  §5). Keep one, link the rest.
- **Contradiction detection** — lexical overlap on the verb-object pair + negation
  polarity (`use X` vs `do not use X`). On a hit, escalate to an LLM judge
  (`contradict` / `compatible` / `context-dependent`); flag, do not auto-pick a
  winner.
- **Coverage / orphan** — cluster the file's rules by topic; single-member
  clusters and dense (5+) clusters both deserve a human glance. Structural signal,
  not a hard filter (surface as a "cluster size" column).
- **Hierarchy compliance** — a rule under a heading should be on-topic for it; a
  one-shot judge rubric catches copy-paste cruft. The two-pass extractor already
  preserves the `source_block` → heading lineage.

### 4.3 World-grounding (needs the live repo)

- **Reference validity** — every concrete token from §4.1 should resolve in the
  current repo (paths via the contents API, symbols via code search, versions via
  `package.json` / `pyproject.toml` / `go.mod`). Dead identifiers are the single
  largest source of stale-and-harmful rules. Amortize against the 24-hour skip.
- **Tool-schema match** — when a rule names a tool/API we have a schema for, check
  the argument names/types/behaviors match. *Deferred* until the schema cache
  exists.

### 4.4 Provenance (needs git history)

`git blame` / `git log -L` against the crawled commit yields **edit recency**
(stale-candidate if untouched while neighbours churn), **survivor count** (high =
load-bearing), and **author concentration** (`CODEOWNERS`). Surface in the UI; do
not auto-filter. *Deferred* — the schema lacks line-edit provenance beyond the
`source_block` span.

### 4.5 LLM-judge meta-metrics (scores the rule, not its content)

Four orthogonal axes, ordinal 1–5, one Haiku rubric each — the static analog of a
trajectory helpful/harmful counter:

| Axis | Question | Pruning signal |
|---|---|---|
| Necessity | Would removing this rule plausibly change a compliant agent's behavior? | 1 = no change → low value |
| Generality | Is this advice any well-behaved agent would follow anyway? | 5 = common sense → low value |
| Actionability | Can this be turned into a concrete runtime check? | 1 = vague → unfalsifiable |
| Risk-asymmetry | How much worse is violating than following? | 5 = high cost to violate → keep |

### 4.6 Anti-patterns to catch

Corpus-level prune candidates: **vague platitudes** (no imperative markers + no
grounding + low IDF), **self-duplicates** (in-file cosine > 0.93),
**self-contradictions**, **stale references** (an identifier no longer resolves),
**section-mismatched cruft**, **common-sense filler** (Generality = 5). A rule
failing **two or more** can be auto-suppressed from the default labeler queue; a
rule failing **exactly one** should surface with a flag for a human to decide.

### 4.7 Implementation order (smallest delta first)

- **Phase A — pure-text signals** on the `rule` row (`imperative_force`,
  `concrete_grounding`, `tfidf_specificity`, `atomicity_flag`): computable in the
  extractor output pass, no extra LLM calls. Surface, do not yet filter.
- **Phase B — intra-file structural** (§4.2 redundancy + hierarchy): reuse the
  embedding; one Haiku call per rule for hierarchy.
- **Phase C — judge-side quality axes** (§4.5) behind a `QUALITY_JUDGE_ENABLED`
  flag mirroring `JUDGE_ENABLED`; log to `rule_llm_decision` with a distinct
  `decision_kind`.
- **Phase D (deferred)** — world-grounding (§4.3); adds GitHub load.
- **Phase E (deferred)** — provenance (§4.4); needs line-edit history.

---

## 5. Equivalence & de-duplication (downstream, extraction-adjacent)

Companion memo: `RESEARCH-llm-judge-semantic-grouping.md`. The intra-file dedup in
§4.2 is the extraction-time cousin of this inter-file problem; the full grouping
runs after extraction settles.

Two rules belong in one cluster if they express the **same operational intent** —
a compliant agent would do the same thing in response to either. Surface form is
irrelevant (`always run pytest before pushing` ≡ `green CI is required before
merge`), but neighbouring intent is not equivalence (`use type hints` ≢ `annotate
every public function`). A cosine-only gate conflates paraphrase with neighbouring
intent in the ambiguous band (≈ `[0.85, 0.93]` for the 0.6b embedder) — exactly
where an LLM judge earns its keep.

**Recommended recipe** — cascade with canonicalization:

1. **Canonicalize on ingest** (piggyback the judge call): add a `canonical_form` —
   a one-sentence imperative restatement, second-person, present tense, no project
   nouns — and embed *that* for clustering (keep `rule_text` for display). The
   Clio / IDAS pattern; collapses paraphrases the small embedder otherwise misses.
2. **Block** on a shared taxonomy-axis value (near-free pair reduction).
3. **Embedding ANN** within block, top-k at cosine ≥ ~0.75 over `canonical_form`.
4. **Decision bands:** ≥ τ_high → auto-merge; `[τ_low, τ_high)` → pairwise LLM
   judge with a **swap test** (call with positions swapped, merge only on
   agreement) and **reason-before-verdict** JSON (`{rationale, verdict, confidence}`
   with verdict ∈ `identical` | `equivalent` | `related` | `distinct`; merge only
   `identical`/`equivalent`); < τ_low → no call.
5. **Escalate** swap-disagreements to self-consistency (k=3 @ T≈0.7), then to the
   human calibration UI.
6. **Transitive-closure check** — when union-find would merge clusters of combined
   size > 2, judge the cross-cluster boundary pair explicitly (the everyrow.io
   failure mode: "A~B, B~C, ⟹ A~C").
7. **Centroid mode** once stable: keep a summary per cluster; for new rules ask
   "does this belong to {summary}?" — O(n × |clusters|), not O(n²).

Report metrics **per cosine band** (the judge's value concentrates in
`[0.85, 0.93]`); target Cohen's κ ≥ 0.7 against a trusted human.

---

## 6. Deferred decisions that bound this scope

From `DECISIONS.md` (2026-05-28):

- **Deterministic regex enforcement is deferred (detect-and-ignore).** Some rules
  are deterministically checkable ("line length ≤ 100", "no `console.log`"). The
  pipeline may *detect* these incidentally but takes **no action** — no auto-tag as
  `enforced`/`solved`, no red UI highlight, no judge short-circuit. The four-axis
  `enforcement_mechanisms` taxonomy is still settling; hard-coding a
  regex→`enforced` shortcut now would bake in a classification we might soon
  reorganize. Revisit when that axis stops churning across judge prompt versions.

Several §4 (concrete grounding, reference validity) and §4.5 signals overlap this
decision's scope; revisit them together.

---

## 7. The runtime feedback loop (Revise)

The base `_SYSTEM_PROMPT` is **frozen**; it is never edited in place to chase a
single bad extraction. Instead, labeler corrections are folded back through a
non-destructive, reversible addendum:

1. **Gather** (`src/revise.py` → `gather_feedback`) reads the human signal already
   in the corpus: rejected extractions, edited `rule_text`, files with logged
   missed rules, hand-added rules, and rule/block comments.
2. **Propose** (`propose_revision`) sends a digest of that feedback to the LLM,
   which proposes an **additive** guidance addendum — it generalizes patterns the
   base prompt is missing and *refines, never overrides*, the rules above. Thin
   feedback yields an empty proposal.
3. **Review** — the admin UI's **Revise** tab shows the proposal's summary +
   patterns in an editable box. A human edits and clicks **Apply** (or discards).
4. **Store** — applied text is saved to `crawl_settings` under
   `config.GUIDANCE_ADDENDUM_SETTING` (`"extraction_guidance_addendum"`).
5. **Apply at crawl time** — `crawl.run_one_pass` reads the addendum once per pass
   and threads it through `crawl._process_file` → `extractor.extract(...,
   guidance_addendum=...)`, which appends it to the frozen `_SYSTEM_PROMPT` under a
   clearly-labeled header. Empty/absent ⇒ the base prompt runs unchanged.

Because the addendum lives in settings and is *appended* (never substituted), it
is fully reversible (the Revise tab's "Remove current addendum") and requires no
code edit or prompt-version bump. A structural change to the base spec still goes
through §2–§3 + `EXTRACTOR_PROMPT_VERSION`; the addendum is for incremental,
feedback-driven refinement between those.

---

## References

Extraction-time quality (companion): `RESEARCH-static-rule-quality-metrics.md` ·
Inter-file equivalence (companion): `RESEARCH-llm-judge-semantic-grouping.md` ·
Deferred decisions: `DECISIONS.md` · Pipeline overview: `../README.md` · Operative
prompt + output contract: `../src/extractor.py` · Models / caps / versions /
addendum key: `../src/config.py`.

Verified external citations (from the companion memos):

- [Clio, Anthropic 2024](https://assets.anthropic.com/m/7e1ab885d1b24176/original/Clio-Privacy-Preserving-Insights-into-Real-World-AI-Use.pdf)
- [ClusterLLM (Zhang et al., EMNLP 2023)](https://arxiv.org/abs/2305.14871)
- [SPILL (Lin et al., 2025)](https://arxiv.org/abs/2503.15351)
- [LLMEdgeRefine (Feng et al., EMNLP 2024)](https://aclanthology.org/2024.emnlp-main.1025/)
- [IDAS (De Raedt et al., 2023)](https://arxiv.org/abs/2305.19783)
- [k-LLMmeans / Summaries as Centroids (Diaz-Rodriguez, ICLR 2026)](https://arxiv.org/abs/2502.09667)
- [Judging LLM-as-a-Judge (Zheng et al., 2023)](https://arxiv.org/abs/2306.05685)
- [Survey on LLM-as-a-Judge (Gu et al., 2024)](https://arxiv.org/abs/2411.15594)
- [Just Ask for Calibration (Tian et al., EMNLP 2023)](https://arxiv.org/abs/2305.14975)
- [ACE bullet+counter playbook](https://arxiv.org/abs/2510.04618) ·
  [EvolveR principle metric score](https://arxiv.org/abs/2510.16079) ·
  [AHE per-edit manifest discipline](https://arxiv.org/abs/2604.25850)
