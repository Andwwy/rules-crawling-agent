"""DuckDB connection helper.

Two backends, picked automatically by env var:

  - **MotherDuck cloud** when `MOTHERDUCK_TOKEN` is set. Connection string is
    `md:<MOTHERDUCK_DB>` (defaults to `rules_in_the_wild`). The token is
    picked up from the env var by the DuckDB client; we also re-export it as
    lowercase `motherduck_token` in docker-compose because some client paths
    look there. Multi-process writes are fine — MotherDuck handles concurrency
    server-side — so the "thread-only crawler" workaround is no longer strictly
    required, just inherited.
  - **Local file** at `/data/rules.duckdb` (or `DUCKDB_PATH`) when no token.
    Single-process-write per DuckDB's normal file-locking rules.

Schema bootstrapping: on first connect in this process we run
`db/duckdb_schema.sql` idempotently — every statement tolerates an "already
exists" error, so the same script bootstraps a fresh DB and is a no-op on a
fully-applied one. Add new tables to that file, not here. Identical for both
backends.
"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb


# Project root is two levels up from this file (src/db.py → src → root). This
# resolves to /app inside the container (Docker mounts the repo at /app) and
# to the actual repo path when running on the host — same code, both modes.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "duckdb_schema.sql"

# Local-file fallback path (still honoured when no MotherDuck token).
DB_PATH = Path(os.environ.get("DUCKDB_PATH", str(PROJECT_ROOT / "data" / "rules.duckdb")))

# MotherDuck wiring. Token is read by the duckdb client from the env at
# connect-time; we don't pass it as a connect arg.
USING_MOTHERDUCK = bool(os.environ.get("MOTHERDUCK_TOKEN"))
MOTHERDUCK_DB = os.environ.get("MOTHERDUCK_DB", "rules_in_the_wild")

# Reentrant: `_new_conn` may take the lock and then call `_ensure_schema`,
# which itself takes the lock. RLock lets the same thread re-acquire safely.
_init_lock = threading.RLock()
_initialised = False


def _connect_target() -> str:
    """Return the DuckDB connect string for the active backend."""
    if USING_MOTHERDUCK:
        return f"md:{MOTHERDUCK_DB}"
    return str(DB_PATH)


def _ensure_motherduck_db_exists() -> None:
    """MotherDuck refuses to attach to `md:{NAME}` if NAME doesn't exist yet.
    Connect to the catalog endpoint (`md:`), run CREATE DATABASE IF NOT EXISTS,
    and close. Only runs when USING_MOTHERDUCK."""
    if not USING_MOTHERDUCK:
        return
    root = duckdb.connect("md:")
    try:
        root.execute(f"CREATE DATABASE IF NOT EXISTS {MOTHERDUCK_DB}")
    finally:
        root.close()


def _split_sql(sql: str) -> list[str]:
    """Strip `--` line comments, then split on `;`. Required because the
    schema's comment lines occasionally contain a `;` (e.g. "in-process; lives
    in ...") that would otherwise break a naive split."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def _apply_schema_idempotent(rw: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Apply a multi-statement schema script, statement-by-statement, treating
    'already exists' / 'duplicate' errors as a no-op. Recovers cleanly from a
    partial apply (e.g. when an earlier run died mid-schema)."""
    for stmt in _split_sql(sql):
        try:
            rw.execute(stmt)
        except duckdb.Error as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate" in msg:
                continue  # idempotent: type/table/sequence/index already there
            raise


def _bootstrap_schema() -> None:
    """Apply `duckdb_schema.sql` idempotently. Safe on fresh, fully-applied,
    and partial-state DBs alike — every statement swallows "already exists"
    errors. Runs once per process; new tables go in the .sql file, not here.
    """
    global _initialised
    with _init_lock:
        if _initialised:
            return
        _ensure_motherduck_db_exists()
        target = _connect_target()
        rw = duckdb.connect(target)
        try:
            _apply_schema_idempotent(rw, SCHEMA_PATH.read_text())
            _initialised = True
        finally:
            rw.close()


def get_setting(c: duckdb.DuckDBPyConnection, key: str, default: str | None = None) -> str | None:
    row = c.execute("SELECT value FROM crawl_settings WHERE key = ?", [key]).fetchone()
    return row[0] if row else default


def set_setting(c: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    # DuckDB UPSERT: ON CONFLICT (key) DO UPDATE.
    c.execute(
        """
        INSERT INTO crawl_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
        """,
        [key, value],
    )


def _new_conn(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """The current schema doesn't use HNSW; `array_cosine_similarity` is a
    core DuckDB built-in and needs no extension. Re-add `LOAD vss` here if we
    ever bring HNSW back."""
    if not USING_MOTHERDUCK:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _bootstrap_schema()
    target = _connect_target()
    # MotherDuck doesn't accept read_only=True via the URL; pass it only for
    # the local-file backend.
    if USING_MOTHERDUCK:
        return duckdb.connect(target)
    return duckdb.connect(target, read_only=read_only)


@contextmanager
def conn(read_only: bool = False):
    """Use as: `with conn() as c: ...`  Commits implicitly on DuckDB (autocommit).
    The caller may still wrap multiple statements in BEGIN/COMMIT if needed."""
    c = _new_conn(read_only=read_only)
    try:
        yield c
    finally:
        c.close()


def enum_values(c: duckdb.DuckDBPyConnection, type_name: str) -> list[str]:
    """Mirror of the Postgres helper: read ENUM labels at runtime so the prompt
    and the UI filters stay schema-driven. DuckDB exposes ENUM members via
    `enum_range(NULL::TYPE)` returning a single-element list-of-strings row."""
    row = c.execute(f"SELECT enum_range(NULL::{type_name})").fetchone()
    if not row or not row[0]:
        return []
    return list(row[0])
