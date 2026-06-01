"""Storage for the extraction-guidance document edited in the admin UI's
"Revise" tab.

Two-part design (see docker-compose.yml + db/duckdb_schema.sql):
  • The LIVE document is a real file inside the repo at ``guidance/guidance.md``
    (writable-mounted into the container). It's the source of truth for "current"
    and is git-diffable / editable outside the app.
  • Every save or approved revision also appends the full new text to the
    ``guidance_version`` table — an append-only history so prior versions stay
    viewable / restorable from the UI.

The file is seeded from ``Design docs/extraction.md`` (copied to
``guidance/guidance.md``). ``ensure_seeded()`` backfills version 1 from the file
the first time history is empty, so the timeline starts at the seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .db import PROJECT_ROOT, conn

# Live document path. PROJECT_ROOT resolves to /app in the container and the repo
# root on the host; guidance/ is writable-mounted in both.
GUIDANCE_DIR = PROJECT_ROOT / "guidance"
GUIDANCE_PATH = GUIDANCE_DIR / "guidance.md"

# Mirrors the CHECK constraint on guidance_version.source.
VALID_SOURCES = ("seed", "manual_edit", "feedback_revision", "restore")


@dataclass
class GuidanceVersion:
    version_no: int
    content: str
    summary: str | None
    source: str
    created_at: datetime


# -----------------------------------------------------------------------------
# Live file
# -----------------------------------------------------------------------------

def read_guidance() -> str:
    """Current live guidance text (the file). Empty string if the file is
    missing — the UI treats that as "not seeded yet"."""
    try:
        return GUIDANCE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write_file(content: str) -> None:
    GUIDANCE_DIR.mkdir(parents=True, exist_ok=True)
    GUIDANCE_PATH.write_text(content, encoding="utf-8")


# -----------------------------------------------------------------------------
# DB version history
# -----------------------------------------------------------------------------

def _insert_version(c, content: str, summary: str | None, source: str) -> int:
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid guidance source {source!r}")
    return c.execute(
        """
        INSERT INTO guidance_version (content, summary, source)
        VALUES (?, ?, ?)
        RETURNING version_no
        """,
        [content, summary, source],
    ).fetchone()[0]


def version_count(c) -> int:
    return c.execute("SELECT count(*) FROM guidance_version").fetchone()[0]


def current_version_no(c) -> int | None:
    row = c.execute("SELECT max(version_no) FROM guidance_version").fetchone()
    return row[0] if row and row[0] is not None else None


def list_versions(c, limit: int = 100) -> list[GuidanceVersion]:
    rows = c.execute(
        """
        SELECT version_no, content, summary, source, created_at
          FROM guidance_version
         ORDER BY version_no DESC
         LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [GuidanceVersion(*r) for r in rows]


def get_version(c, version_no: int) -> GuidanceVersion | None:
    row = c.execute(
        """
        SELECT version_no, content, summary, source, created_at
          FROM guidance_version
         WHERE version_no = ?
        """,
        [version_no],
    ).fetchone()
    return GuidanceVersion(*row) if row else None


# -----------------------------------------------------------------------------
# Mutations
# -----------------------------------------------------------------------------

def save_guidance(content: str, summary: str | None, source: str) -> int:
    """Persist a new guidance version: overwrite the live file AND append the
    full text to DB history. File is written first so the UI (which reads the
    file for "current") always reflects exactly what was saved. Returns the new
    version_no."""
    _write_file(content)
    with conn() as c:
        return _insert_version(c, content, summary, source)


def ensure_seeded() -> None:
    """Backfill version 1 from the live file when the history table is empty
    (fresh DB whose guidance.md was seeded from extraction.md). Idempotent and
    cheap — safe to call on every Revise-tab render. No-op if the file is empty
    (nothing to seed) or history already exists."""
    text = read_guidance()
    if not text.strip():
        return
    with conn() as c:
        if version_count(c) == 0:
            _insert_version(c, text, "Seeded from Design docs/extraction.md", "seed")
