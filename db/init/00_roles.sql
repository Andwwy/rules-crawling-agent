-- Required by ../Design docs/schema.sql's RLS policies, which GRANT INSERT
-- on rule_human_label to the `labelers` role. Without these CREATE ROLE
-- statements the schema apply fails.
--
-- Filename starts with 00_ so this runs BEFORE the schema (Postgres init dir
-- runs files in lexicographic order).

CREATE ROLE labelers NOLOGIN;
CREATE ROLE service  NOLOGIN;

-- Grant the prototype admin user permission to act as either role for testing.
GRANT labelers TO rules_admin;
GRANT service  TO rules_admin;
