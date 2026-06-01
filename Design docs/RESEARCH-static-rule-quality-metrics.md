# Research memo — Static rule-quality metrics (no execution feedback)

Status: research output, not a decision doc. Compiled 2026-06-01.
Scope: signals the crawler / extractor / judge can compute on a freshly ingested
`AGENTS.md` / `CLAUDE.md` / `.cursorrules` file **before any trajectory feedback
is available** — i.e., metrics that score a rule using only the text of the file,
the surrounding file structure, and (optionally) the live repo it came from.

This is a companion to `RESEARCH-llm-judge-semantic-grouping.md`. That memo asks
"do two rules express the same intent?" — this one asks the upstream question:
**"is this candidate rule worth keeping in the corpus at all?"** Both are
extraction-time concerns. Neither requires running an agent against the rule.

Why this matters here. Sister work on *evolving* agent rules (EvolveR, ACE,
Continual Harness, AHE, Meta-Harness) all converge on one signal — per-rule
helpful/harmful counters or success rates derived from trajectories. We do **not**
have those signals in the crawler — we see each rule exactly once, the moment we
extract it, with no agent ever having run against it. The question this memo
answers is: what can we still infer about rule quality at extraction time? The
signals below feed two downstream choices: (a) what the extractor decides to
emit as a `rule` row vs. ignore (Pass 2 in `extraction_guide.md`), and (b) what
the judge sees on the `ambiguity_level` / `enforcement_mechanisms` axes.

Concrete recommendations are in §6.

---

## 1. Frame: what "rule quality" means at extraction time

A "good" rule at extraction time is one that a downstream consumer (the
labeling platform, a later judge, an evaluation harness, an evolving agent)
can act on with low ambiguity. Concretely:

- An agent reading the rule can derive a **single decision** from it.
- The rule references **something that still exists** in the world it was
  written for.
- The rule does **not duplicate** another rule in the same file or corpus.
- The rule does **not contradict** another rule in the same file.
- The rule carries **enough specificity** to distinguish a compliant from a
  non-compliant action.

None of these require running the rule. All are computable from the file plus
optionally the live repo. The five families below cover them.

---

## 2. Per-rule intrinsic quality (text-only signals)

These score a single rule from its own text. Cheap, deterministic, and the
first filter to apply.

### 2.1 Imperative force

Rules are imperative by nature. Count modal/imperative markers: `must`,
`should`, `never`, `always`, `do not`, `avoid`, `prefer`, `required`,
`forbidden`, plus sentence-initial imperative verbs (`use`, `run`, `check`,
`add`). A candidate sentence with no imperative marker is descriptive prose
that snuck into the rule pass — common in mixed `AGENTS.md` files that
include both rules and context.

Use as a filter, not a score. Zero imperative markers → demote out of the
rule stream.

### 2.2 Concrete grounding

Regex / NER over the rule for concrete tokens: file paths, function/method
names (`snake_case`, `camelCase`, `PascalCase`), version numbers, command
names, environment variable names, error codes. A simple count, normalized
by rule length, correlates strongly with downstream actionability.

Example: `Use snake_case for all module-level functions in src/db.py` →
high (3 concrete tokens: `snake_case`, `src/db.py`, `functions`). `Write
clean, maintainable code` → zero. The bottom decile on this metric is
almost always boilerplate.

### 2.3 Atomicity

A rule that mixes more than one verb-object pair is a candidate for the
two-pass extractor's compound-split logic (per `extraction_guide.md`). At
extraction time we can detect it with a parse: count distinct verb phrases
and conjunctions (`and`, `;`, `, also`). High count → emit a flag for the
splitter; the judge then sees pre-split atomic rules.

### 2.4 Falsifiability

A binary LLM-as-judge rubric: "Could you write a check that decides whether
this rule was followed?" with a yes/no answer and one-sentence reason. Cheap
to add to `judge.py` as a single axis; complements `ambiguity_level` rather
than overlapping with it (a rule can be `clear` on intent but unfalsifiable
in practice: `keep the agent helpful and honest` is clear-intent but
unfalsifiable).

### 2.5 Specificity entropy

TF-IDF over the rule, with the rest of the same file as background. A
distinctive rule has high IDF tokens; boilerplate ("be polite", "follow
best practices") has near-zero. Cheap to compute alongside the embedding
that's already on `rule.embedding`. Useful as a numeric quality score
attached to the row.

---

## 3. Cross-rule structure (file-level signals)

These need the rest of the file or the rest of the corpus. They map directly
onto operations the extractor and the (eventual) labeler already do.

### 3.1 Embedding-based redundancy within the file

Pair-wise cosine over rules from the same file. Two rules above the
calibration threshold are surface duplicates the extractor accidentally
emitted twice (common when an `AGENTS.md` repeats a rule in both an
"Overview" section and a "Coding standards" section). Keep one, link the
other to the first as a `same_rule` edge in the future graph schema, or
silently drop. Uses the same embeddings already stored on `rule.embedding`,
so cost is zero.

This is the *intra-file* analog of the inter-file semantic-grouping problem
in `RESEARCH-llm-judge-semantic-grouping.md`. The threshold can be looser
here: rules in the same file with cosine > 0.93 are nearly always genuine
duplicates, vs. the calibration range [0.85, 0.93] needed across files.

### 3.2 Contradiction detection

Two rules in the same file that prescribe opposite actions on the same
predicate. Cheap pre-filter: lexical overlap on the verb-object pair plus
negation polarity check (`use X` vs `do not use X`, `always X` vs
`never X`). Pair-wise sweep is O(n²) but n per file is small (median
20-40 rules per `AGENTS.md` in the current corpus, per `crawl.py --stats`).

When the lexical pre-filter fires, escalate to an LLM judge call: "Do these
two rules contradict each other in any plausible context?" with a
three-class output (`contradict` / `compatible` / `context-dependent`).
Flag contradictions on the rule rows; do not silently choose a winner.

### 3.3 Coverage / orphan detection

Cluster all rules in the file by topic (embedding-based or LLM-tagged with
a short topic label per rule). Single-member clusters are either uniquely
valuable or stale-and-irrelevant; either way they deserve a human glance.
Dense clusters (5+ rules on the same micro-topic) are over-covered and a
later compression pass can summarize them.

This is a structural signal, not a hard filter. Expose it on the Browse tab
as a "cluster size" column so the human labeler can spot both extremes
quickly.

### 3.4 Hierarchy compliance

A rule under a Markdown heading should be on-topic for that heading. An
LLM judge with a one-shot rubric ("Does this rule belong under this
section heading?") catches copy-paste cruft cheaply. The two-pass extractor
already preserves the `source_block` → heading lineage; this metric just
asks the model to verify the assignment.

---

## 4. World-grounding (codebase signals)

These need the live repo. Cheap to compute incrementally because the
crawler already has the GitHub adapter wired up.

### 4.1 Reference validity

Every concrete token surfaced by §2.2 (file path, function name, version
number, command, env var) should resolve in the current repo. For paths,
HEAD `GET /repos/{owner}/{repo}/contents/{path}`. For symbols, a code
search over the repo. For versions, parse `package.json` / `pyproject.toml`
/ `go.mod`.

A rule referencing dead identifiers (`Use the @deprecated tools/legacy.py
adapter`) is the single largest source of stale-and-harmful rules in
long-lived agent files. Catching them at extraction time prevents
shipping rules that contradict the current codebase.

Cost note. Each repo verification is at most one extra GitHub call per
referenced token, batched. The crawler is already rate-limit-aware; add
this only after the 24-hour skip is hit so we're amortizing API calls.

### 4.2 Tool-schema match

When the rule mentions a tool or API name that we have a schema for (the
canonical builtin tool set, or any MCP tool we cache), check that the
argument names, types, and behaviors described in the rule match the
current schema. Mismatches are stale-API rules.

Defer until the agent-evolution loop ships — the schema cache doesn't
exist in the prototype yet — but the design hook should be reserved on
`rule` now.

---

## 5. Provenance signals (git-history)

These require the file to be in a git repo (it is — every source is a
GitHub commit). Cheap to compute via `git blame` over the file at the
commit we crawled.

### 5.1 Edit recency

`git blame` per line gives the commit that last touched each rule. A rule
untouched for many commits while surrounding files churned is stale-candidate
territory. Store the last-edit commit SHA and timestamp on the `rule` row;
no policy attached yet — let the labeling UI surface it.

### 5.2 Survivor count

Number of times the rule has been modified and kept across the file's
history. High survivor count = load-bearing rule (someone keeps coming
back to refine it); single-edit rules are either trivially right or no
one has touched them. Compute via `git log -L` on the rule's line range.

### 5.3 Author concentration

Rule added by a code owner (via `CODEOWNERS` if present) weighs more than
a drive-by edit. Surface in the UI; do not auto-filter — code ownership
information is patchy across the corpus.

---

## 6. LLM-as-judge meta-metrics (model evaluation, no execution)

This extends the existing judge with axes that score the rule itself, not
the rule's content. Each axis is one short rubric, one Haiku call per rule.
Cheap to fold into the existing judge pipeline (`src/judge.py`); the prompt
template lives next to the taxonomy axes.

Proposed rubric set (4 axes, ordinal 1-5):

| Axis | Question | Pruning signal |
|---|---|---|
| Necessity | Would removing this rule plausibly change a compliant agent's behavior? | 1 = no change → low value |
| Generality | Is this rule advice any well-behaved agent would follow anyway? | 5 = common sense → low value |
| Actionability | Can this rule be turned into a concrete check at runtime? | 1 = vague → unfalsifiable |
| Risk-asymmetry | How much worse is violating the rule than following it? | 5 = high cost to violate → keep |

Average or vote across 2-3 judge prompts for robustness. This is the
static analog of ACE's helpful/harmful counter from trajectory feedback —
weaker signal because no real agent has tried the rule, but recoverable.

The four axes are deliberately orthogonal to the existing 7-axis taxonomy
in `src/judge.py`: those classify *what kind of rule* it is; these score
*how good a rule it is*. Both can be stored on `rule_llm_decision` with
distinct `decision_kind` tags so prompt versions on each axis evolve
independently.

---

## 7. Anti-patterns the static pipeline should catch

Combining the above, the corpus-level prune candidates are:

- **Vague platitudes** (§2.1, §2.2, §2.5): zero imperative markers + zero
  concrete grounding + low IDF.
- **Self-duplicates** (§3.1): cosine > 0.93 with another rule in the same
  file.
- **Self-contradictions** (§3.2): lexical+polarity match plus LLM judge.
- **Stale references** (§4.1): one or more referenced identifiers no
  longer resolve in HEAD.
- **Section-mismatched cruft** (§3.4): rule semantically off-topic for
  its containing heading.
- **Common-sense filler** (§6 Generality = 5): the judge says any model
  would do this anyway.

A rule failing two or more of these can be auto-suppressed from the
default labeler queue. A rule failing exactly one should surface with a
flag so the human labeler decides.

---

## 8. Recommendations for the prototype

In implementation order, smallest delta first:

**Phase A — pure-text signals on the rule row.** Add four numeric columns
to `rule` (the schema already accepts ad-hoc columns per `db/duckdb_schema.sql`):
`imperative_force` (count), `concrete_grounding` (normalized count), `tfidf_specificity`
(float), `atomicity_flag` (bool). All four are computable in the extractor
output pass; no extra LLM calls. Surface them on the Browse tab as a
"quality" column group; do **not** filter the corpus on them yet.

**Phase B — intra-file structural signals.** Wire §3.1 (intra-file
cosine dedup) and §3.4 (hierarchy compliance) into the extractor's
post-process step. §3.1 reuses the embedding already computed; §3.4 is
one Haiku call per rule. Add `intra_file_dup_of` (FK to `rule.id`) and
`hierarchy_compliant` (bool) columns.

**Phase C — judge-side quality axes.** Extend `src/judge.py` with the four
§6 axes behind a `QUALITY_JUDGE_ENABLED` config flag mirroring `JUDGE_ENABLED`.
Log each axis to `rule_llm_decision` with a distinct `decision_kind`.

**Phase D (deferred) — world-grounding.** Reference validity (§4.1) is
high-value but adds GitHub API load. Defer until the 24-hour skip cache
proves the rate budget can absorb it, or move to a nightly pass.

**Phase E (deferred) — provenance.** §5 needs `git blame` against the
commit we crawled. Cheap per-repo but the schema doesn't have line-range
provenance yet beyond the `source_block` span. Revisit when the schema
gets line-edit history.

None of A-C require trajectory feedback. None require running a single
agent. All five families are pure read-and-judge over the file as it lands
in the corpus, which is exactly the regime the crawler operates in today.

---

## 9. What this memo deliberately does not cover

- **Trajectory-derived quality** (helpful/harmful counters, success rate,
  metric scores). Those are the domain of `Evolving_Agent_Rules_Report.md`
  in `Agentic RL/` and the EvolveR/ACE/Continual-Harness/AHE lineage. They
  require an agent loop the crawler does not own.
- **Inter-file semantic clustering**. That is the subject of
  `RESEARCH-llm-judge-semantic-grouping.md`. The intra-file dedup in §3.1
  is the upstream cousin; the inter-file judge runs after extraction is
  settled.
- **Hard regex enforcement / auto-tagging `enforced` rules**. Deferred per
  `DECISIONS.md` (2026-05-28). Several of the §2 and §4 signals overlap
  with that decision's scope; revisit together when the
  `enforcement_mechanisms` axis stabilizes.

---

## References

- Companion memo (inter-file equivalence): `RESEARCH-llm-judge-semantic-grouping.md`
- Companion report (trajectory-driven evolution): `../../../Agentic RL/Evolving_Agent_Rules_Report.md`
- Deferred decisions touching this scope: `DECISIONS.md` (2026-05-28)
- Extraction spec the rules come from: `extraction_guide.md` (referenced in `README.md`)
- ACE bullet+counter playbook design: [arXiv 2510.04618](https://arxiv.org/abs/2510.04618)
- EvolveR principle metric score: [arXiv 2510.16079](https://arxiv.org/abs/2510.16079)
- AHE per-edit manifest discipline: [arXiv 2604.25850](https://arxiv.org/abs/2604.25850)
