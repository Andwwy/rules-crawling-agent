-- Prototype-only table for the cluster-threshold calibration session described
-- in ../Design docs/DESIGN.md §8. NOT in the canonical schema — it lives here
-- because production has no use for it once the threshold is locked in.

CREATE TABLE IF NOT EXISTS calibration_label (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_a_id       UUID NOT NULL REFERENCES rule(id) ON DELETE CASCADE,
    rule_b_id       UUID NOT NULL REFERENCES rule(id) ON DELETE CASCADE,
    cosine          REAL NOT NULL CHECK (cosine BETWEEN 0 AND 1),
    is_same_rule    BOOLEAN NOT NULL,         -- the human's call
    labeled_by      TEXT NOT NULL,            -- free-text (you, basically)
    labeled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_a_id, rule_b_id, labeled_by)
);

CREATE INDEX calibration_cosine_idx ON calibration_label (cosine);
