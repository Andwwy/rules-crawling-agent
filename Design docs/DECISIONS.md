# Design decisions log

Short, durable records of deliberate choices — especially the ones we chose
*not* to build yet, so a future implementer knows it was considered, not missed.

---

## 2026-05-28 — Defer deterministic regex enforcement ("solved"/"enforced" rules)

**Status:** Deferred (detect-and-ignore).

**Context.** Some extracted rules are deterministically enforceable — e.g. a rule
whose compliance can be checked by a literal pattern match rather than judgment
("line length <= 100", "no `console.log`", "files must end in a newline"). The
proposal was to add a *regex schema*: match `rule_text` against patterns, auto-tag
the matches as `enforced` / `solved`, render them in **red** in both UIs to flag
that no human/LLM judgment is needed, and short-circuit the LLM judge for that
subset (saving spend + latency).

**Decision.** Do **not** build this yet. The crawler/labeler may *detect* such
rules incidentally, but takes **no action** on a match — no auto-tag, no red
highlight, no judge short-circuit. The four-axis `enforcement_mechanisms` taxonomy
(see `judge.py`) is still settling; hard-coding a regex→`enforced` shortcut now
would bake in a classification we might soon reorganize, and the red-highlight UI
affordance would imply a guarantee we can't yet stand behind.

**Revisit when.** The `enforcement_mechanisms` axis stabilizes (values stop
churning across judge prompt versions). At that point, reconsider: (a) a pattern
table keyed to a stable enforcement category, (b) the `enforced`/`solved` rule
state, and (c) the red UI treatment.

**Where this is also recorded (keep in sync):**
- `db/duckdb_schema.sql` — `FUTURE (deferred 2026-05-28)` note above
  `CREATE TABLE rule_llm_decision`.
- `../../labeling-platform/documents/schema-v0.4.duckdb.sql` — identical note.

**Scope note.** This was decision #5 of the 2026-05-28 extraction/labeling rework.
The other four decisions from that round shipped: rule_type + execution_guidance
per rule (extract-v2), per-rule judge rationale (judge v3), `<repo>/path` document
display, and a reliable repo-page link fallback for the 404-prone deep links.
