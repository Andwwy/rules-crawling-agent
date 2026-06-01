# Research memo — LLM-as-judge for semantic grouping of rules

Status: research output, not a decision doc. Compiled 2026-05-19.
Scope: techniques for using an LLM as the *equivalence* judge over short imperative
rules (the rules in AGENTS.md / CLAUDE.md / .cursorrules), to replace or augment
the cosine-threshold gate that today decides whether two rules merge into one
`rule_semantic_cluster` row.

The current `src/judge.py` only does taxonomy classification (7 axes →
enum values). This memo is about extending the judge concept to **equivalence
decisions**, which is the gap the cosine threshold and the §8 calibration session
are trying to plug. Concrete recommendations are in §5.

---

## 1. Frame: what "semantic grouping" means here

Two rules belong in the same cluster if they express **the same operational
intent** for an LLM coding agent — i.e., a compliant agent would do the same
thing in response to either rule. Surface form is irrelevant. Examples:

- `always run pytest before pushing` ≡ `green CI is required before merge`
- `use type hints` ≢ `annotate every public function` (the second is strictly
  narrower; "related" but not the same intent)
- `do not use console.log` ≢ `prefer structured logging over print statements`
  (sibling rules, distinct prescriptions)

A cosine-only gate, even calibrated, conflates "paraphrase" with "neighbouring
intent." The literature consistently finds the failure mode is in the ambiguous
band — for our 0.6b embedder, that's roughly cosine ∈ [0.85, 0.93] per the
README. That is exactly where an LLM judge earns its keep.

## 2. Patterns from the literature (verified)

I dispatched four parallel research agents covering academic papers, practitioner
write-ups, judge mechanics, and evaluation methodology. The list below is the
filtered intersection — papers/posts that map cleanly onto our use case, with
citations spot-checked against the source.

### 2.1 Canonicalize-then-compare (highest leverage)

The strongest recurring pattern: have the LLM rewrite each input into a
**short canonical intent statement**, then embed *those* and cluster the rewrites.

- **Clio** (Anthropic, 2024) — extracts a one-sentence "facet" per
  conversation, embeds the facet, clusters with k-means, then has the LLM name
  and verify each cluster. The canonicalization step is what makes the
  embedding+cluster step work at all on heterogeneous short text.
  [PDF](https://assets.anthropic.com/m/7e1ab885d1b24176/original/Clio-Privacy-Preserving-Insights-into-Real-World-AI-Use.pdf)
- **IDAS** — De Raedt et al., NLP4ConvAI@ACL 2023, [arXiv:2305.19783](https://arxiv.org/abs/2305.19783).
  Same canonicalize-then-cluster pattern for intent discovery; SOTA on
  Banking/StackOverflow/CLINC datasets.
- **k-LLMmeans / "Summaries as Centroids"** — Diaz-Rodriguez, ICLR 2026,
  [arXiv:2502.09667](https://arxiv.org/abs/2502.09667). Replaces numeric
  centroids with LLM-generated summary strings; assignment still happens in
  embedding space, so cost stays roughly linear. Good fit once we have stable
  clusters and want auditable centroids.

Why this matters for us: the rules we ingest have wildly different surface
form — bullet points, sentences, fragments, sometimes with project-specific
nouns ("never edit `apps/web/src/legacy/`"). Canonicalizing to "do not modify
legacy module" before embedding collapses a class of paraphrases that the
0.6b embedder otherwise misses.

### 2.2 Pairwise judge over candidate pairs (workhorse)

The default LLM-judge shape for equivalence is binary pairwise: "do A and B
express the same intent?" with structured output.

- **ClusterLLM** — Zhang/Wang/Shang, EMNLP 2023,
  [arXiv:2305.14871](https://arxiv.org/abs/2305.14871). Uses ChatGPT for two
  prompt shapes: pairwise "do A and B belong to the same category?" (sets
  cluster granularity) and triplet "does A correspond to B better than C?"
  (refines the embedder). Total LLM cost averaged $0.60/dataset across 14
  benchmarks.
- **SPILL** — Lin/Hasibi/Verberne, ACL Findings 2025,
  [arXiv:2503.15351](https://arxiv.org/abs/2503.15351). Two stages:
  embedder picks k candidates around a seed → LLM picks which share the
  same intent → pool to refine. No fine-tuning needed. Most "turnkey" recipe
  for what we want.
- **everyrow.io/dedupe write-up** — Poyiadzi,
  [dev.to](https://dev.to/rafael_poyiadzi_85c2eed09/introducing-everyrowiodedupe-an-llm-based-approach-to-semantic-deduplication-40kl).
  Engineering retro on production semantic dedup: embedding-similarity
  pre-filter → pairwise LLM match/no-match → union-find for transitive closure
  → re-validate clusters of size > 2. Calls out transitive false positives
  ("A~B, B~C, ergo A~C") as the main trap.

### 2.3 Edge-point refinement (best fit for our calibration UI)

The literature directly addresses our shape: cheap embedder does the bulk,
LLM is only called on items near the decision boundary.

- **LLMEdgeRefine** — Feng et al., EMNLP 2024,
  [aclanthology.org](https://aclanthology.org/2024.emnlp-main.1025/). LLM
  touches only boundary/edge points of embedding clusters to reassign or split.
- **LITA** — [arXiv:2412.12459](https://arxiv.org/abs/2412.12459). Same
  philosophy, applied to topic augmentation.

This maps 1:1 onto our calibration UI: the 0.85/0.88/0.91 bands *are* the edge
points; the 0.97 band needs no judge, the 0.70 band needs no judge.

### 2.4 Judge mechanics — bias mitigations that apply to equivalence

From the LLM-as-judge literature ([Zheng et al. 2023](https://arxiv.org/abs/2306.05685),
[Gu et al. survey 2024](https://arxiv.org/abs/2411.15594)):

- **Position bias is real and large** even for binary equivalence. The robust
  mitigation is **swap-and-agree**: call the judge with (A, B) and (B, A),
  accept only on agreement; treat disagreement as "uncertain → don't merge."
  Instruction-only fixes ("ignore order") under-perform.
- **Reason-before-verdict.** With structured JSON output, put `rationale`
  *before* `same_intent` in the schema. The model decodes left-to-right; this
  forces a commit-to-reason before commit-to-answer. CoT gains are smaller for
  binary classification than for math, but the side benefit — debuggable
  rationales for the calibration UI — is large.
- **4-level rubric instead of binary**: `identical / equivalent / related /
  distinct`. Map only `identical+equivalent` to MERGE; `related` lands in a
  `see-also` band (which we already have via `SEE_ALSO_FLOOR`). Practitioner
  retros consistently report this beats binary "are these the same?".
- **Self-consistency for ambiguous cases**: k=3 samples at T≈0.7, majority
  vote. Cost-prohibitive globally; cheap if reserved for the swap-disagreed
  or low-confidence subset.
- **Haiku-class models are workable but need help.** Small models show
  inconsistency at low temperature; swap + 3-sample vote is the practical
  recipe to get to acceptable consistency.

### 2.5 Things the literature does *not* settle

- No published number for "how much embedding recall is enough before the LLM
  judge becomes the bottleneck" specifically in the *clustering* setting. IR
  reranking literature suggests the answer saturates once top-k recall@k > 0.9
  ([arXiv:2501.09186](https://arxiv.org/abs/2501.09186)), but that's retrieval.
- No public write-up from Anthropic (Claude Code), Cursor, Aider, Continue, or
  Cowork describing an automated LLM-based deduper for AGENTS.md / .cursorrules
  files. This appears to be an **unfilled niche**; the closest prior art is
  Clio + everyrow.io/dedupe + ClusterLLM, none from the coding-agent ecosystem.

## 3. Evaluation methodology

Our calibration UI gives us pair-level labels at sampled cosine bands.
That's enough for the pair-level metrics; for cluster-level metrics we'd want
a small (~50-item) hand-partitioned gold set in addition.

### 3.1 Metrics

- **Pair-level**: precision, recall, F1, PR-AUC (preferred over ROC-AUC
  under "same" class rarity), and Matthews Correlation Coefficient (MCC) for
  imbalanced classes.
- **Cluster-level**: B³ precision/recall/F1 (Bagga & Baldwin, LREC 1998) is
  the standard in coreference/entity resolution and is our closest analog;
  Adjusted Rand Index (Hubert & Arabie 1985) as a second metric; Adjusted
  Mutual Information instead of plain NMI when comparing across cluster counts
  (Vinh et al., JMLR 2010).

### 3.2 Confidence calibration

If the judge emits `confidence ∈ [0, 1]`:

- Plot a **reliability diagram**, report **ECE** with 15 bins and **Brier
  score** as a proper scoring rule (Guo et al., ICML 2017).
- Fit **Platt scaling** on ~150 of our 200 pairs (isotonic regression overfits
  below ~1000 examples; Niculescu-Mizil & Caruana, ICML 2005).
- Verbalized confidence often beats logit-derived confidence for RLHF'd
  models — Tian et al., "Just Ask for Calibration," EMNLP 2023.

### 3.3 Judge-vs-human agreement

- **Cohen's κ** for binary same/different. Aim for κ ≥ 0.7 against a single
  trusted human, or κ ≥ 0.6 against the median of multiple humans (McHugh 2012
  is stricter than Landis & Koch 1977; pick a target before measuring).
- Report κ with bootstrap CIs — point estimates on 200 pairs are noisy
  (±0.1 typical).

### 3.4 Stratified-by-band reporting

Always report metrics **per cosine band** rather than averaged across bands.
The judge's marginal value is concentrated in the 0.85–0.93 band; averaging
hides whether the judge actually helps where it needs to.

### 3.5 Drift across judge versions

When Haiku 4.5 → 4.6 (or we revert to Anthropic SDK, per `judge.py`'s
docstring):

- Keep the 200 labeled pairs as a **frozen regression set**.
- Run **differential testing**: both versions on a large unlabeled pool,
  label only the disagreements (typically 50–200 pairs if disagreement <15%).
- Report metric deltas with **paired bootstrap CIs** (Koehn, EMNLP 2004).
- **Refit Platt** per version — confidence calibration does not transfer.

## 4. Cost / O(n²) control

The judge call is the bottleneck. Standard techniques, in increasing order of
sophistication:

1. **Exact-hash dedup first** — we already do this.
2. **Metadata blocking**: only judge pairs that share at least one taxonomy
   axis value (or are in neighbouring axes). Typically 5–20× pair reduction
   at near-zero recall cost. **We have these labels already from the
   taxonomy judge** — using them as a blocker is essentially free.
3. **Embedding ANN top-k** at a low cosine floor (~0.75, not 0.93); only
   judge those pairs. This is what the cascade in §5 assumes.
4. **Canopy clustering** (loose/tight thresholds, McCallum 2000) — only
   needed if n grows past ~50k rules.
5. **Centroid-judge mode** once clusters exist: maintain a summary per cluster
   (k-LLMmeans style); for new rules ask "does X belong to {cluster
   summary}?" — O(n × |clusters|), not O(n²).

## 5. Concrete recommendation for our prototype

Given Haiku 4.5 via Perplexity, the 1024-d 0.6b embedder, DuckDB+vss, and the
existing calibration UI, the recipe with highest evidence-per-line-of-code is:

**Cascade with three bands plus canonicalization.**

1. **Canonicalize on ingest** (extends `judge.py`). For each rule, call Haiku
   once during the existing taxonomy classify pass and add a `canonical_form`
   field to the rule row: a one-sentence imperative restatement, normalized
   to second-person, present tense, no project nouns. Embed `canonical_form`
   instead of `rule_text` for the clustering pipeline (keep `rule_text` for
   display). This is the Clio/IDAS pattern. ~+1 token per rule, no extra
   API call (piggyback on the existing judge call).
2. **Blocking**: only consider pairs within the same `rule_kind` value
   (taxonomy axis we already extract). Cheap, large recall preservation.
3. **Embedding ANN** within block, top-k=10 at cosine ≥ 0.75 over
   `canonical_form` embeddings. Generates candidate pairs.
4. **Pair-level decision bands**:
   - cosine ≥ τ_high (currently 0.93, recalibrate): auto-merge, no LLM call.
   - cosine ∈ [τ_low, τ_high) (currently 0.75–0.93): pairwise LLM judge with
     **swap test** — call Haiku twice with positions swapped. Schema:
     `{rationale: str, verdict: "identical"|"equivalent"|"related"|"distinct",
     confidence: float ∈ [0,1]}`. `rationale` is decoded first by design.
     Merge iff both calls return `identical` or `equivalent` *and* the
     verdicts agree across the swap.
   - cosine < τ_low: no LLM call, not merged.
5. **Ambiguity escalation**: if the swap test disagrees, run self-consistency
   (k=3 at T=0.7, majority vote across 3 verdicts × 2 orders = 6 calls). If
   still uncertain, surface in the calibration UI for human label.
6. **Transitive closure check**: when union-find would merge clusters of
   combined size > 2, judge the cross-cluster boundary pair explicitly
   rather than closing automatically. (everyrow.io/dedupe failure mode.)
7. **Once stable, switch to centroid mode**: store a Haiku-generated summary
   per cluster (already partly satisfied by `canonical_form` of the cluster
   representative); for new rules ask "does this belong to {summary}?".
   Drops to O(n × |clusters|).

This is a relatively conservative recipe — each step has direct support in
the cited papers (Clio for §1, ClusterLLM for §3, LLMEdgeRefine for §4,
everyrow.io for §6, k-LLMmeans for §7). It also fits the existing prototype
structure: §1 is a `judge.py` extension, §3 lives in `cluster.py`, §4 is the
gate the §8 calibration session is already designed to tune, and §5 is a new
table.

### What to measure during the §8 calibration session

In addition to the existing precision-vs-threshold plot:

- B³ F1 over a small hand-partitioned 50-rule gold set.
- Cohen's κ between Haiku judge and human label, stratified by cosine band.
- ECE on Haiku's confidence after Platt scaling on 150 pairs.
- Cost: tokens-per-merge-decision; should sit under ~600 in/out tokens per
  pair given the canonical-form length cap.

## 6. Caveats and known unknowns

- Haiku-class consistency at zero/low temperature is mediocre; the recipe
  above leans on swap + self-consistency to compensate. If we move to Sonnet
  in production we can drop self-consistency.
- The 0.6b Perplexity embedder is noisier than `voyage-3` (the production
  embedder in DESIGN.md). The cosine bands in this memo are best treated as
  shape, not magnitudes — recalibrate post-embedder-swap.
- The "no published coding-agent rule deduper" finding may simply mean it
  exists internally at Cursor/Cohere/Anthropic but wasn't published; not a
  guarantee no one's done it.
- Several research-agent claims were spot-checked against the sources and
  passed; one (everyrow.io's exact "CREATE/MERGE/SKIP" verdict labels) was
  imprecise — the source describes match/no-match, not three verdicts. The
  4-level rubric in §5 is a synthesis from multiple sources, not lifted from
  one paper.

## Sources

Verified citations only:

- [Clio, Anthropic 2024](https://assets.anthropic.com/m/7e1ab885d1b24176/original/Clio-Privacy-Preserving-Insights-into-Real-World-AI-Use.pdf)
- [ClusterLLM (Zhang et al., EMNLP 2023)](https://arxiv.org/abs/2305.14871)
- [SPILL (Lin et al., 2025)](https://arxiv.org/abs/2503.15351)
- [LLMEdgeRefine (Feng et al., EMNLP 2024)](https://aclanthology.org/2024.emnlp-main.1025/)
- [IDAS (De Raedt et al., 2023)](https://arxiv.org/abs/2305.19783)
- [k-LLMmeans / Summaries as Centroids (Diaz-Rodriguez, ICLR 2026)](https://arxiv.org/abs/2502.09667)
- [LITA (2024)](https://arxiv.org/abs/2412.12459)
- [Few-Shot Clustering with LLMs (Viswanathan et al., TACL 2024)](https://arxiv.org/abs/2307.00524)
- [GoalEx (Wang et al., 2023)](https://arxiv.org/abs/2305.13749)
- [Judging LLM-as-a-Judge (Zheng et al., 2023)](https://arxiv.org/abs/2306.05685)
- [Survey on LLM-as-a-Judge (Gu et al., 2024)](https://arxiv.org/abs/2411.15594)
- [G-Eval (Liu et al., 2023)](https://arxiv.org/abs/2303.16634)
- [Prometheus (Kim et al., ICLR 2024)](https://arxiv.org/abs/2310.08491)
- [ChatEval (Chan et al., ICLR 2024)](https://arxiv.org/abs/2308.07201)
- [Just Ask for Calibration (Tian et al., EMNLP 2023)](https://arxiv.org/abs/2305.14975)
- [On Calibration of Modern Neural Networks (Guo et al., ICML 2017)](https://arxiv.org/abs/1706.04599)
- [Dedup of paper titles with LLM (2024)](https://arxiv.org/abs/2410.01141)
- [Cleanlab near-duplicate docs](https://docs.cleanlab.ai/)
- [Dedupe.io — how it works](https://dedupe.io/documentation/how-it-works.html)
- [everyrow.io/dedupe (Poyiadzi, dev.to)](https://dev.to/rafael_poyiadzi_85c2eed09/introducing-everyrowiodedupe-an-llm-based-approach-to-semantic-deduplication-40kl)
- [Vespa — LLM-as-a-judge for retrieval](https://blog.vespa.ai/improving-retrieval-with-llm-as-a-judge/)
- [Anthropic — Claude Code best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
