-- =============================================================================
-- Rules-in-the-Wild — DuckDB / MotherDuck schema (v0.4, 2026-05-28)
--
-- This is the crawler's bootstrap schema. The crawler OWNS table creation
-- (db.py applies this file idempotently on first connect); the labeling
-- platform connects to the SAME MotherDuck database and only reads/writes
-- rows. So this file must stay byte-for-byte aligned with the labeling
-- contract in `../../../labeling-platform/documents/schema-v0.4.duckdb.sql`,
-- with exactly two crawler-side deltas (both called out inline below):
--   1. `crawl_settings` — a crawler-only ops table (continuous-mode toggle).
--      The labeling platform ignores it.
--   2. `rule` and `source_block` carry a NON-UNIQUE index on
--      (rules_file_id, original_line_start, original_line_end) instead of a
--      UNIQUE constraint. See "WHY NON-UNIQUE" below.
--
-- v0.4 is a clean-slate redesign of v0.3:
--   1. Two per-(rule, labeler) label tables — rule_extraction_label and
--      rule_classification_label. Internal tool: reads aren't filtered by
--      labeler_id; everyone sees everyone's labels.
--   2. Real-time-write model: rule text/line edits update the `rule` row
--      directly (not a label override). The extractor's original output is
--      frozen on `rule.original_*` so edit-distance metrics stay computable.
--   3. Re-extraction redesign (2026-05-28): the extractor now emits a short
--      `rule.extraction_reason` (WHY a span is a rule), classifies each span as
--      `rule.rule_type` ('rule' vs 'context'), captures the `execution_guidance`
--      (the "how") paired with each rule, and the markdown segmentation is a
--      first-class `source_block` table (1 block → N rules via
--      `rule.source_block_id`).
--   4. Four-axis judge taxonomy (prerequisites / enforcement_mechanisms /
--      triggers / ambiguity) replacing v0.3's seven single-valued enums.
--
-- WHY NON-UNIQUE (rules_file_id, original_line_start, original_line_end):
--   The extraction guide (§3.2, §6.2) splits a compound single line into
--   multiple rules that share ONE line range (e.g. "We use 2-space indentation
--   and single quotes." → two rules, both on line 14). A UNIQUE constraint on
--   that span would reject the second rule. It is also a DuckDB-WASM foot-gun
--   for the labeling platform: WASM HANGS the connection on a constraint
--   violation instead of throwing (the platform already pre-checks rather than
--   relying on the UNIQUE). A plain index gives the lookups speed without the
--   collision. Idempotency is preserved upstream: a rules_file is extracted
--   exactly once (UNIQUE on rules_file gates re-extraction).
--
-- Pipeline:
--   Crawler → rules_file → source_block (segmented spans)
--                                        ↓
--                          rule (.original_* + editable rule_text/lines,
--                                source_block_id, extraction_reason)
--                                        ↓
--                                rule_llm_decision (4 axes)
--                                        ↓
--                            rule_extraction_label   (decision only)
--                                        ↓
--                            rule_classification_label  (predicted + edited 4 axes)
--
--   Human annotation (orthogonal to the pipeline):
--     rule_comment / source_block_comment — free-text notes a labeler attaches
--     to a rule or a source block; each comment is joined to its labeler.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Enums (taxonomy + workflow)
-- -----------------------------------------------------------------------------

-- Provenance of a rules_file: which agent-instruction convention it is.
CREATE TYPE rules_file_kind AS ENUM (
    'claude_md',                   -- CLAUDE.md, .claude/*.md
    'agents_md',                   -- AGENTS.md
    'cursor_rules',                -- .cursorrules, .cursor/rules/*.mdc
    'windsurf_rules',              -- .windsurfrules
    'aider_conventions',           -- CONVENTIONS.md, .aider.conf.yml
    'cline_rules',                 -- .clinerules
    'copilot_instructions',        -- .github/copilot-instructions.md
    'continue_rules',              -- .continue/rules
    'llms_txt',                    -- llms.txt
    'system_prompt_repo',
    'awesome_list',
    'vendor_doc',
    'claude_marketplace_manifest',
    'claude_plugin_manifest',
    'claude_plugin_command',
    'claude_plugin_agent',
    'claude_plugin_skill',
    'claude_plugin_hook_config',
    'other'
);

-- Three labeling decisions on both phases. "correct" is implicit (= the
-- labeler edited something): recoverable by comparing rule.rule_text vs
-- rule.original_rule_text (extraction) or corrected_* IS NOT NULL (classify).
CREATE TYPE label_decision AS ENUM ('accept','reject','skip');

-- Ambiguity is the 4th classification axis. Level is the structured signal
-- (metrics + filtering); ambiguity_notes (TEXT) carries the rationale.
CREATE TYPE ambiguity_level AS ENUM ('none','low','medium','high');

-- Whether an extracted span is a normative RULE the agent must follow, or
-- project/action CONTEXT that informs behavior without itself being a directive.
-- Context is extracted + tagged (not dropped) so labelers see the distinction.
CREATE TYPE rule_type AS ENUM ('rule','context');

-- Append-only history for the judge.
CREATE TYPE judge_decision_trigger AS ENUM (
    'initial',
    'prompt_revised',
    'human_disagreed',
    'manual_rerun'
);

CREATE TYPE ingestion_status AS ENUM ('queued','running','succeeded','failed','partial');

-- -----------------------------------------------------------------------------
-- 2. Source provenance
-- -----------------------------------------------------------------------------

CREATE TABLE source_project (
    id              UUID PRIMARY KEY DEFAULT uuid(),
    host            TEXT NOT NULL,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    canonical_url   TEXT NOT NULL UNIQUE,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_crawled_at TIMESTAMPTZ
);

CREATE TABLE rules_file (
    id              UUID PRIMARY KEY DEFAULT uuid(),
    project_id      UUID NOT NULL REFERENCES source_project(id),
    path            TEXT NOT NULL,
    kind            rules_file_kind NOT NULL,
    commit_sha      TEXT,
    raw_content     TEXT NOT NULL,
    content_sha256  BLOB NOT NULL,
    byte_size       INTEGER NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Appended log of rules the extractor MISSED. The labeling UI's "Add
    -- Rule" flow lets a human flag a rule the agent failed to extract; each
    -- such addition appends a one-line "[skipped_rule] …" record here.
    missed_rules    TEXT,
    UNIQUE (project_id, path, commit_sha)
);
CREATE INDEX rules_file_project_idx ON rules_file (project_id);
CREATE INDEX rules_file_sha_idx     ON rules_file (content_sha256);

-- A source_block is one segmented span of a rules_file (a heading, a list
-- item, a paragraph, a fenced code block). The crawler emits these alongside
-- the rules it extracts; each rule points at the block it came from
-- (rule.source_block_id), giving an explicit block→rule (1-to-many) relation.
--
-- line_start / line_end are LIVE editable (a labeler can widen/narrow a block
-- on the extraction page); original_line_* freeze the segmenter's output and
-- double as the natural key for re-matching a block across re-segmentation.
-- NON-UNIQUE on the span on purpose — see "WHY NON-UNIQUE" in the header.
CREATE TABLE source_block (
    id                  UUID PRIMARY KEY DEFAULT uuid(),
    rules_file_id       UUID NOT NULL REFERENCES rules_file(id),
    kind                TEXT,            -- heading | list_item | paragraph | code
    line_start          INTEGER NOT NULL,   -- LIVE editable
    line_end            INTEGER NOT NULL,   -- LIVE editable
    original_line_start INTEGER NOT NULL,   -- frozen segmenter output (natural key)
    original_line_end   INTEGER NOT NULL,
    snippet             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_edited_at      TIMESTAMPTZ         -- NULL until a labeler edits the range
);
CREATE INDEX source_block_file_idx     ON source_block (rules_file_id);
CREATE INDEX source_block_origlines_idx ON source_block (rules_file_id, original_line_start, original_line_end);

-- -----------------------------------------------------------------------------
-- 3. Labeler identity
-- -----------------------------------------------------------------------------

CREATE TABLE labeler (
    id            UUID PRIMARY KEY DEFAULT uuid(),
    handle        TEXT NOT NULL UNIQUE,        -- 'alice@example.com', shown in the dropdown
    display_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 4. Extracted rule
--    rule_text / line_start / line_end are LIVE state — labelers can update
--    rule_text directly from the extraction page (Change Rule). The original
--    extractor output is frozen into original_rule_text / original_line_*
--    so extractor accuracy is still computable from data.
--
--    "Add missing rule" inserts an ordinary rule row: it reuses the file's
--    existing extractor_version, and original_* equals rule_text/line_* at
--    insert time. The LLM judge runs on these the same as any other rule.
--
--    last_edited_at records when a labeler last edited the rule body (NULL
--    until first edit). NON-UNIQUE on the original span — see header.
-- -----------------------------------------------------------------------------

CREATE TABLE rule (
    id                          UUID PRIMARY KEY DEFAULT uuid(),
    rules_file_id               UUID NOT NULL REFERENCES rules_file(id),
    -- The segmented source span this rule was extracted from (1 block → N
    -- rules). NULLABLE: hand-added rules and pre-source_block extractions may
    -- not be linked; the UI falls back to line containment.
    source_block_id             UUID REFERENCES source_block(id),
    -- LIVE editable state
    rule_text                   TEXT NOT NULL,
    line_start                  INTEGER NOT NULL,
    line_end                    INTEGER NOT NULL,
    -- Frozen extractor output (set at insert, never updated)
    original_rule_text          TEXT NOT NULL,
    original_line_start         INTEGER NOT NULL,
    original_line_end           INTEGER NOT NULL,
    rule_text_norm              TEXT NOT NULL,
    rule_text_sha256            BLOB NOT NULL,
    section_anchor              TEXT,
    embedding                   FLOAT[1024],
    extracted_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    extractor_version           TEXT NOT NULL,
    -- The extractor's short justification for WHY this span is a rule. Set at
    -- extraction; frozen (not labeler-editable). NULL for hand-added rules.
    extraction_reason           TEXT,
    -- Extractor classification: 'rule' (a normative directive the agent must
    -- follow) vs 'context' (project/action info that informs behavior but is
    -- not itself a directive). Frozen at extraction; 'rule' for hand-added rows.
    rule_type                   rule_type NOT NULL DEFAULT 'rule',
    -- The "how": execution guidance the source pairs with this rule (commands,
    -- steps, caveats), kept beside the normalized directive. Frozen; NULL if none.
    execution_guidance          TEXT,
    -- NULL until a labeler edits rule_text from the extraction page.
    last_edited_at              TIMESTAMPTZ
);
CREATE INDEX rule_sha_idx        ON rule (rule_text_sha256);
CREATE INDEX rule_rules_file_idx ON rule (rules_file_id);
CREATE INDEX rule_origlines_idx  ON rule (rules_file_id, original_line_start, original_line_end);

-- -----------------------------------------------------------------------------
-- 5. LLM judge decision — APPEND-ONLY HISTORY
--    Four axes the labeling UI uses (per the PDF spec):
--      prerequisites          — TEXT[], free-form items per "what info is
--                               needed to enforce" (e.g. ['No information',
--                               'Bash', 'Repo'])
--      enforcement_mechanisms — TEXT[], items like 'linter','regex','llm','bash'
--      triggers               — TEXT[], items like 'session_init',
--                               'settings.json','verify_gate','post_exec'
--      ambiguity_level        — enum + ambiguity_notes (rationale, optional)
--    These are TEXT[] not enum[] because the items are open-ended free-form —
--    the labeling UI is a multi-line list, no enum lock-in.
--
-- FUTURE (deferred 2026-05-28): deterministic regex pre-classification. A regex
-- schema would match rule_text against patterns for rules that are
-- deterministically enforceable, auto-tag them 'enforced'/'solved' (rendered in
-- red in the UIs), and short-circuit the LLM judge for that subset. Decision for
-- now: detect-and-ignore — do NOT act on a match yet; revisit when the
-- enforcement_mechanisms taxonomy stabilizes.
-- -----------------------------------------------------------------------------

CREATE TABLE rule_llm_decision (
    id                      UUID PRIMARY KEY DEFAULT uuid(),
    rule_id                 UUID NOT NULL REFERENCES rule(id),
    judge_model             TEXT NOT NULL,
    judge_prompt_version    TEXT NOT NULL,
    triggered_by            judge_decision_trigger NOT NULL,
    prompt_messages         JSON NOT NULL,
    raw_response            TEXT NOT NULL,
    parse_ok                BOOLEAN NOT NULL,
    parse_error             TEXT,
    -- Parsed classification (NULL when parse_ok = false)
    prerequisites           TEXT[],
    enforcement_mechanisms  TEXT[],
    triggers                TEXT[],
    ambiguity_level         ambiguity_level,
    ambiguity_notes         TEXT,
    confidence              REAL CHECK (confidence BETWEEN 0 AND 1),
    rationale               TEXT,
    -- Ops metadata
    latency_ms              INTEGER,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX rule_llm_decision_rule_idx   ON rule_llm_decision (rule_id, created_at DESC);
CREATE INDEX rule_llm_decision_prompt_idx ON rule_llm_decision (judge_prompt_version);

-- -----------------------------------------------------------------------------
-- 6. EXTRACTION LABEL — phase 1 of labeling
--    "Did the extractor correctly identify this text as a rule?"
--    The labeler edits rule.rule_text/line_* directly (real-time writes), so
--    the label only records the verdict. PK (rule_id, labeler_id): each
--    labeler keeps their own decision per rule.
-- -----------------------------------------------------------------------------

CREATE TABLE rule_extraction_label (
    rule_id     UUID NOT NULL REFERENCES rule(id),
    labeler_id  UUID NOT NULL REFERENCES labeler(id),
    decision    label_decision NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, labeler_id)
);
CREATE INDEX rule_extraction_label_labeler_idx ON rule_extraction_label (labeler_id, updated_at DESC);

-- -----------------------------------------------------------------------------
-- 7. CLASSIFICATION LABEL — phase 2 of labeling
--    "Are the LLM judge's 4 axis values right for this rule?"
--    predicted_* is a frozen snapshot at row creation; corrected_* updates
--    real-time as the labeler edits (NULL = unchanged from prediction).
--    predicted_decision_id / predicted_snapshot_hash are NULLABLE: a rule can
--    be labeled with NO LLM prediction (hand-added, or judge never scored).
--    A non-null fabricated predicted_decision_id would violate the FK to
--    rule_llm_decision; a NULL FK is exempt. (DuckDB-WASM hangs the prepared
--    statement on such an FK violation rather than raising.)
-- -----------------------------------------------------------------------------

CREATE TABLE rule_classification_label (
    rule_id                  UUID NOT NULL REFERENCES rule(id),
    labeler_id               UUID NOT NULL REFERENCES labeler(id),
    predicted_decision_id    UUID REFERENCES rule_llm_decision(id),  -- NULL when the rule had no LLM prediction
    predicted_snapshot_hash  BLOB,                                   -- NULL when the rule had no LLM prediction
    decision                 label_decision NOT NULL DEFAULT 'skip',
    -- Frozen snapshot of the LLM prediction at row creation.
    predicted_prerequisites           TEXT[],
    predicted_enforcement_mechanisms  TEXT[],
    predicted_triggers                TEXT[],
    predicted_ambiguity_level         ambiguity_level,
    predicted_ambiguity_notes         TEXT,
    -- Labeler edits, real-time. NULL = unchanged from prediction.
    corrected_prerequisites           TEXT[],
    corrected_enforcement_mechanisms  TEXT[],
    corrected_triggers                TEXT[],
    corrected_ambiguity_level         ambiguity_level,
    corrected_ambiguity_notes         TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, labeler_id)
);
CREATE INDEX rule_classification_label_labeler_idx  ON rule_classification_label (labeler_id, updated_at DESC);
CREATE INDEX rule_classification_label_decision_idx ON rule_classification_label (predicted_decision_id);

-- -----------------------------------------------------------------------------
-- 8. Human annotations — free-text comments (rule & source block)
--    A labeler can leave one or more comments on a rule OR a source block.
--    Append-style threads (own UUID PK, many per (entity, labeler)). Reads
--    aren't filtered by labeler — everyone sees everyone's comments, attributed.
-- -----------------------------------------------------------------------------

CREATE TABLE rule_comment (
    id          UUID PRIMARY KEY DEFAULT uuid(),
    rule_id     UUID NOT NULL REFERENCES rule(id),
    labeler_id  UUID NOT NULL REFERENCES labeler(id),
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX rule_comment_rule_idx    ON rule_comment (rule_id, created_at);
CREATE INDEX rule_comment_labeler_idx ON rule_comment (labeler_id);

CREATE TABLE source_block_comment (
    id          UUID PRIMARY KEY DEFAULT uuid(),
    block_id    UUID NOT NULL REFERENCES source_block(id),
    labeler_id  UUID NOT NULL REFERENCES labeler(id),
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX source_block_comment_block_idx   ON source_block_comment (block_id, created_at);
CREATE INDEX source_block_comment_labeler_idx ON source_block_comment (labeler_id);

-- -----------------------------------------------------------------------------
-- 9. Ops / observability
-- -----------------------------------------------------------------------------

CREATE TABLE ingestion_run (
    id              UUID PRIMARY KEY DEFAULT uuid(),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          ingestion_status NOT NULL DEFAULT 'running',
    source_query    TEXT NOT NULL,
    projects_seen   INTEGER NOT NULL DEFAULT 0,
    files_fetched   INTEGER NOT NULL DEFAULT 0,
    rules_extracted INTEGER NOT NULL DEFAULT 0,
    rules_new       INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    crawler_version TEXT NOT NULL
);

CREATE SEQUENCE ingestion_event_id_seq START 1;
CREATE TABLE ingestion_event (
    id              BIGINT PRIMARY KEY DEFAULT nextval('ingestion_event_id_seq'),
    run_id          UUID NOT NULL REFERENCES ingestion_run(id),
    event_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    level           TEXT NOT NULL CHECK (level IN ('debug','info','warn','error')),
    message         TEXT NOT NULL,
    payload         JSON
);

-- -----------------------------------------------------------------------------
-- 10. Crawler-only ops (NOT part of the labeling contract)
--     Persists the continuous-mode toggle so the admin UI re-spawns the
--     crawler thread across restarts. The labeling platform ignores this table.
-- -----------------------------------------------------------------------------

CREATE TABLE crawl_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 11. Extraction-guidance document history (admin "Revise" tab)
--     The live guidance doc is a real file inside the repo
--     (guidance/guidance.md, writable-mounted); this table is its append-only
--     version log. Every manual save or approved feedback-revision inserts one
--     row holding the FULL document text at that point, so prior versions stay
--     viewable/restorable in the UI. "Current" = MAX(version_no) (it mirrors
--     the file). Crawler + labeling platform ignore this table.
-- -----------------------------------------------------------------------------

CREATE SEQUENCE guidance_version_no_seq START 1;
CREATE TABLE guidance_version (
    id          UUID PRIMARY KEY DEFAULT uuid(),
    version_no  INTEGER NOT NULL DEFAULT nextval('guidance_version_no_seq'),
    content     TEXT NOT NULL,
    summary     TEXT,
    source      TEXT NOT NULL DEFAULT 'manual_edit'
                CHECK (source IN ('seed','manual_edit','feedback_revision','restore')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX guidance_version_no_idx ON guidance_version (version_no DESC);
