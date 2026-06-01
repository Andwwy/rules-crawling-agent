"""Crawl pipeline (DuckDB edition, open-internet discovery).

Two entry points:

  1. CLI:
       python -m src.crawl              # one discovery pass
       python -m src.crawl --stats      # row counts

  2. Library (used by the admin UI thread runner):
       continuous_loop(state) — see admin_app.py `_start_continuous()`.

Discovery shape:
  Instead of a fixed `seeds.yaml` (which made every pass re-process the same
  three repos), each pass now queries GitHub's `/search/repositories` and
  `/search/code` endpoints with a handful of agent-related queries. Repos
  whose `source_project.last_crawled_at` is within SKIP_RECENT_HOURS are
  skipped so passes naturally explore new content as GitHub's trending sets
  shift.

State object (used by thread mode):
  - state.heartbeat   : float, mtime-style timestamp the UI bumps every rerun.
  - state.stop_event  : threading.Event(), set by the Stop button.
  - state.run_id      : UUID of the current ingestion_run row.
  - state.files_done / state.rules_new : cumulative counters across passes.

Error handling: each discovery query runs inside its own try/except so one
bad query (transient HTTP error, malformed search term) doesn't poison the
pass. Per-query errors are written as `ingestion_event(level='error', …)`
and the pass continues.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator

import click
import duckdb
import httpx

from .adapters.base import FetchedFile
from .adapters.claude_marketplace import (
    discover_marketplace,
    discover_solo_plugin,
    discover_solo_plugins_via_code_search,
    discover_via_code_search as discover_marketplaces_via_code_search,
    discover_via_topic_search as discover_marketplaces_via_topic_search,
)
from .adapters.github import (
    _code_search,
    _fetch_one,
    _search_repos,
    set_stop_event as set_github_stop_event,
)
from .config import (
    EXTRACTOR_MODEL,
    EXTRACTOR_PROMPT_VERSION,
    GUIDANCE_ADDENDUM_SETTING,
    JUDGE_ENABLED,
    JUDGE_MODEL,
)
from .cost import cost_usd
from .db import conn, get_setting, set_setting
from .embedder import embed_batched
from .extractor import extract
from .judge import Judge, JudgeDecision


CRAWLER_VERSION = "proto-0.4-llm-extract"

# Continuous-mode pass interval. The loop sleeps this long between passes.
# Set short for visible progress; ADR-001 §Decision uses an hourly Lambda in
# production. At <300 sec, a pass may take longer than the interval itself,
# in which case passes run back-to-back with no idle gap.
PASS_INTERVAL_SECONDS = 60

# How often the continuous loop wakes to check stop_event between passes.
POLL_INTERVAL_SECONDS = 5.0

# Don't re-crawl a repo whose source_project.last_crawled_at is within this
# window. Keeps each pass focused on NEW content as GitHub's trending set
# rotates, instead of repeating the same repos every minute.
SKIP_RECENT_HOURS = 24

# Per-query cap on how many repos to fetch from /search/repositories. Tune
# down to lower rate-limit pressure; tune up for more coverage per pass.
MAX_REPOS_PER_QUERY = 25
MAX_FILES_PER_CODE_QUERY = 25

# Discovery queries — what counts as "agent rule files across the open internet".
# Each entry is (kind, query, paths_or_none, source_kind):
#   - kind="repo": runs /search/repositories(q=query); for each returned repo,
#                  probe each path in `paths` and yield a FetchedFile if found.
#   - kind="code": runs /search/code(q=query); each result is already
#                  (repo, path) so `paths` is None.
# `sort:updated-desc` makes GitHub return the most recently active repos so
# the trending set rotates naturally as people push commits.
_DISCOVERY_QUERIES: list[tuple[str, str, tuple[str, ...] | None, str]] = [
    # Trending repos by topic — agent-coded projects in current development.
    ("repo", "topic:agents pushed:>2026-04-01 stars:>10 sort:updated",
     ("AGENTS.md", "CLAUDE.md", ".cursorrules"), "agents_md"),
    ("repo", "topic:agentic-ai stars:>20 sort:updated",
     ("AGENTS.md", "CLAUDE.md", ".cursorrules"), "agents_md"),
    ("repo", "topic:llm-agent stars:>10 sort:updated",
     ("AGENTS.md", "CLAUDE.md", ".cursorrules"), "agents_md"),
    ("repo", "topic:cursor-rules sort:updated", (".cursorrules",), "cursor_rules"),
    ("repo", "topic:claude-code sort:updated", ("CLAUDE.md", "AGENTS.md"), "claude_md"),
    ("repo", "topic:ai-agent pushed:>2026-04-01 stars:>50 sort:updated",
     ("AGENTS.md", "CLAUDE.md", ".cursorrules"), "agents_md"),
    # Direct file finds via Code Search — catches projects that don't tag the
    # topic but do have the canonical filename.
    ("code", "path:AGENTS.md stars:>50", None, "agents_md"),
    ("code", "path:CLAUDE.md stars:>50", None, "claude_md"),
    ("code", "path:.cursorrules stars:>20", None, "cursor_rules"),
    # Claude Code marketplaces and solo plugins — fully trending-driven (no
    # fixed seed list). `sort:updated` rotates the result set each pass; the
    # adapter applies its own per-plugin 24h skip on top of the per-repo skip
    # so individual plugins within an already-known marketplace also rotate.
    ("marketplace_code", "path:.claude-plugin/marketplace.json sort:updated",
     None, None),
    ("solo_plugin_code", "path:.claude-plugin/plugin.json stars:>3 sort:updated",
     None, None),
    ("marketplace_topic", "topic:claude-code-plugin sort:updated", None, None),
    ("marketplace_topic", "topic:claude-marketplace stars:>5 sort:updated", None, None),
    ("marketplace_topic", "topic:claude-skills sort:updated", None, None),
]


# -----------------------------------------------------------------------------
# CrawlState — the small object the admin thread shares with this module
# -----------------------------------------------------------------------------

@dataclasses.dataclass
class CrawlState:
    """Owned by admin_app.get_crawl_state(); passed into the loop."""
    heartbeat: float
    stop_event: threading.Event
    run_id: str | None = None
    status: str = "idle"
    error: str | None = None
    files_done: int = 0
    rules_new: int = 0
    last_message: str = ""
    # Session spending cap (USD). cap_usd=None means uncapped. spend_usd is the
    # running total since the continuous loop started (session scope); it's
    # mutated only by the worker thread and read by the Streamlit UI thread —
    # the GIL makes the float read/write safe, and the UI copies spend_by_model
    # before iterating. cap_tripped latches True once spend_usd >= cap_usd so the
    # loop hard-stops exactly once.
    cap_usd: float | None = None
    spend_usd: float = 0.0
    spend_by_model: dict[str, float] = dataclasses.field(default_factory=dict)
    cap_tripped: bool = False

    def beat(self) -> None:
        self.heartbeat = time.time()

    def reset_spend(self) -> None:
        """Zero the session ledger — called when the continuous loop (re)starts."""
        self.spend_usd = 0.0
        self.spend_by_model = {}
        self.cap_tripped = False

    def note_spend(self, model: str | None,
                   input_tokens: int | None, output_tokens: int | None) -> None:
        """Add one Agent call's estimated cost to the session total and latch
        the cap if it's been reached. Off-catalog models / missing token counts
        cost 0 (see cost.cost_usd)."""
        if not model:
            return
        amount = cost_usd(model, input_tokens, output_tokens)
        if amount <= 0:
            return
        self.spend_usd += amount
        self.spend_by_model[model] = self.spend_by_model.get(model, 0.0) + amount
        if self.cap_usd is not None and self.spend_usd >= self.cap_usd:
            self.cap_tripped = True


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _sha256(s: str) -> bytes:
    return hashlib.sha256(s.encode("utf-8")).digest()


def _event(c: duckdb.DuckDBPyConnection, run_id: str | None,
           level: str, message: str, payload: dict | None = None) -> None:
    if run_id is None:
        return
    c.execute(
        """
        INSERT INTO ingestion_event (id, run_id, level, message, payload)
        VALUES (nextval('ingestion_event_id_seq'), ?, ?, ?, ?)
        """,
        [run_id, level, message, json.dumps(payload) if payload else None],
    )


def _event_short(run_id: str | None, level: str, message: str,
                 payload: dict | None = None) -> None:
    """Write one ingestion_event on its OWN short-lived connection.

    Used by the discovery scan and the run_one_pass loop, neither of which now
    holds a long-lived connection — opening, writing one row, and closing keeps
    the writer locked for milliseconds instead of for the whole pass."""
    if run_id is None:
        return
    with conn() as c:
        _event(c, run_id, level, message, payload)


def _stop_requested(state: CrawlState | None) -> bool:
    return bool(state and state.stop_event.is_set())


# Kinds that are stored as evidence (manifest, hook config) but never run
# through the extractor — they're declarative JSON, no rule text to mine.
_NON_EXTRACTABLE_KINDS = frozenset({
    "claude_marketplace_manifest",
    "claude_plugin_manifest",
    "claude_plugin_hook_config",
})


def _maybe_yield(ff: FetchedFile, seen: set) -> Iterator[FetchedFile]:
    """Dedup wrapper used by marketplace-style discovery branches. Yields the
    FetchedFile iff (owner, repo, path) hasn't already been seen this pass."""
    key = (ff.project_owner, ff.project_name, ff.file_path)
    if key in seen:
        return
    seen.add(key)
    yield ff


def _is_recently_crawled(c: duckdb.DuckDBPyConnection,
                         owner: str, name: str,
                         hours: float = SKIP_RECENT_HOURS) -> bool:
    row = c.execute(
        """
        SELECT last_crawled_at
          FROM source_project
         WHERE host = 'github.com' AND owner = ? AND name = ?
        """,
        [owner, name],
    ).fetchone()
    if not row or row[0] is None:
        return False
    age_h = (datetime.now(timezone.utc) - row[0]).total_seconds() / 3600
    return age_h < hours


# -----------------------------------------------------------------------------
# Source-file plumbing
# -----------------------------------------------------------------------------

def _upsert_project(c: duckdb.DuckDBPyConnection, ff: FetchedFile) -> str:
    row = c.execute(
        "SELECT id FROM source_project WHERE canonical_url = ?",
        [ff.project_canonical_url],
    ).fetchone()
    if row:
        c.execute(
            "UPDATE source_project SET last_crawled_at = now() WHERE id = ?",
            [row[0]],
        )
        return row[0]
    c.execute(
        """
        INSERT INTO source_project (host, owner, name, canonical_url, last_crawled_at)
        VALUES (?, ?, ?, ?, now())
        RETURNING id
        """,
        [ff.project_host, ff.project_owner, ff.project_name, ff.project_canonical_url],
    )
    return c.fetchone()[0]


def _upsert_rules_file(c: duckdb.DuckDBPyConnection, project_id: str,
                       ff: FetchedFile) -> tuple[str, bool]:
    raw_bytes = ff.raw_content.encode("utf-8")
    content_hash = _sha256(ff.raw_content)
    existing = c.execute(
        """
        SELECT id FROM rules_file
         WHERE project_id = ? AND path = ?
           AND (commit_sha = ? OR (commit_sha IS NULL AND ? IS NULL))
        """,
        [project_id, ff.file_path, ff.commit_sha, ff.commit_sha],
    ).fetchone()
    if existing:
        return existing[0], False
    c.execute(
        """
        INSERT INTO rules_file (project_id, path, kind, commit_sha,
                                raw_content, content_sha256, byte_size)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        [project_id, ff.file_path, ff.source_kind, ff.commit_sha,
         ff.raw_content, content_hash, len(raw_bytes)],
    )
    new_id = c.fetchone()[0]
    # New-commit-of-same-file used to flip the old row's is_current=FALSE.
    # `is_current` is no longer in the schema; old rows just remain as
    # historical records, distinguished from the new one by commit_sha.
    return new_id, True


def _snippet(lines: list[str], line_start: int, line_end: int, cap: int = 16000) -> str:
    """Verbatim source slice for a block, 1-indexed inclusive. Capped so a huge
    heading-section block can't bloat a row — raw_content already holds the full
    file, so the snippet is just a labeling-UI convenience."""
    text = "\n".join(lines[line_start - 1:line_end])
    if len(text) > cap:
        text = text[:cap] + "\n…[truncated]"
    return text


def _insert_decision_row(c: duckdb.DuckDBPyConnection, rule_id: str,
                         judge: Judge, d: JudgeDecision) -> None:
    """Append a precomputed JudgeDecision as a rule_llm_decision row (append-only
    history). The classify() call and its spend accounting already happened in
    _process_file's no-connection phase, so this is only the fast write half —
    the connection is held for milliseconds, never across the model call.

    A malformed model reply is stored with parse_ok=false, not dropped."""
    c.execute(
        """
        INSERT INTO rule_llm_decision
            (id, rule_id, judge_model, judge_prompt_version, triggered_by,
             prompt_messages, raw_response, parse_ok, parse_error,
             prerequisites, enforcement_mechanisms, triggers,
             ambiguity_level, ambiguity_notes, confidence, rationale,
             latency_ms, input_tokens, output_tokens)
        VALUES (uuid(), ?, ?, ?, 'initial',
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?)
        """,
        [rule_id, judge.model_id, judge.prompt_version,
         json.dumps(d.prompt_messages), d.raw_response, d.parse_ok, d.parse_error,
         d.prerequisites, d.enforcement_mechanisms, d.triggers,
         d.ambiguity_level, d.ambiguity_notes, d.confidence, d.rationale,
         d.latency_ms, d.input_tokens, d.output_tokens],
    )


def _process_file(ff: FetchedFile, run_id: str | None, state: CrawlState | None,
                  judge: Judge | None = None,
                  extractor_model: str | None = None,
                  guidance_addendum: str | None = None) -> int:
    """Ingest one fetched file. Returns count of NEW rules inserted.

    Connection discipline: this function opens a DB connection ONLY for its two
    short write bursts — (1) the project + rules_file upsert, and (2) the
    source_block + rule (+ optional decision) inserts. The slow work between
    them (the extractor LLM call, embeddings, and any judge LLM calls) runs with
    NO connection open, so the crawler never holds the writer across a
    multi-second model call. Each `with conn()` opens, writes, and closes fast.

    Write order matches the v0.4 schema's FKs: source_block first, then rule
    (rule.source_block_id → source_block.id), then rule_llm_decision. The
    extractor's output is frozen on rule.original_* / source_block.original_*;
    rule_text/line_* start equal to the originals and become labeler-editable."""
    # --- Phase 1: upsert project + rules_file (fast writes) ------------------
    with conn() as c:
        project_id = _upsert_project(c, ff)
        rules_file_id, is_new = _upsert_rules_file(c, project_id, ff)
    if state:
        state.files_done += 1
        state.last_message = f"{ff.project_owner}/{ff.project_name}:{ff.file_path}"
    if not is_new:
        return 0
    # Manifest / hook-config rows are stored as evidence only; nothing to extract.
    if ff.source_kind in _NON_EXTRACTABLE_KINDS:
        print(f"[crawl] {ff.project_owner}/{ff.project_name}:{ff.file_path}  (manifest, no extract)")
        return 0

    # --- Phase 2: extraction + embeddings + judging (NO connection held) -----
    extraction = extract(ff.raw_content, kind=ff.source_kind, model=extractor_model,
                         guidance_addendum=guidance_addendum)
    if state is not None:
        state.note_spend(extraction.model, extraction.input_tokens, extraction.output_tokens)
    if not extraction.rules:
        return 0
    lines = ff.raw_content.splitlines()
    vectors = embed_batched([r.text for r in extraction.rules])
    # Classify every rule up front so the whole file is judged even if the spend
    # cap latches mid-file — we never leave a half-judged file behind, and
    # run_one_pass hard-stops only at the next file boundary. These are model
    # calls, so no DB connection is held while they run. (judge=None when
    # JUDGE_ENABLED is False → decisions stay None and no decision rows written.)
    decisions: list[JudgeDecision | None]
    if judge is not None:
        decisions = []
        for er in extraction.rules:
            d = judge.classify(er.text, ff.source_kind)
            if state is not None:
                state.note_spend(judge.model_id, d.input_tokens, d.output_tokens)
            decisions.append(d)
    else:
        decisions = [None] * len(extraction.rules)

    # --- Phase 3: write burst — source_blocks + rules + decisions ------------
    # Insert only rule-bearing blocks — scaffolding blocks (a heading over pure
    # orientation text) carry nothing to label. Map each block's position in the
    # extractor's list → its freshly minted source_block UUID.
    referenced = sorted({r.block_index for r in extraction.rules if r.block_index is not None})
    file_inserted = 0
    with conn() as c:
        try:
            block_id_by_pos: dict[int, str] = {}
            for pos in referenced:
                blk = extraction.blocks[pos]
                block_id = c.execute(
                    """
                    INSERT INTO source_block
                        (rules_file_id, kind, line_start, line_end,
                         original_line_start, original_line_end, snippet)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    [rules_file_id, blk.kind, blk.line_start, blk.line_end,
                     blk.line_start, blk.line_end, _snippet(lines, blk.line_start, blk.line_end)],
                ).fetchone()[0]
                block_id_by_pos[pos] = block_id

            for er, vec, d in zip(extraction.rules, vectors, decisions):
                norm = _norm(er.text)
                rule_id = c.execute(
                    """
                    INSERT INTO rule
                        (rules_file_id, source_block_id, rule_text, line_start, line_end,
                         original_rule_text, original_line_start, original_line_end,
                         rule_text_norm, rule_text_sha256, section_anchor, embedding,
                         extractor_version, extraction_reason, rule_type, execution_guidance)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    [rules_file_id, block_id_by_pos.get(er.block_index),
                     er.text, er.line_start, er.line_end,
                     er.text, er.line_start, er.line_end,
                     norm, _sha256(norm), er.section_anchor, vec,
                     EXTRACTOR_PROMPT_VERSION, er.extraction_reason,
                     er.rule_type, er.execution_guidance],
                ).fetchone()[0]
                file_inserted += 1
                if d is not None:
                    _insert_decision_row(c, rule_id, judge, d)

            _event(
                c, run_id, "info",
                f"file_done: {ff.project_owner}/{ff.project_name} · {ff.file_path}",
                {"project": f"{ff.project_owner}/{ff.project_name}",
                 "path": ff.file_path,
                 "inserted": file_inserted},
            )
        except Exception:
            # Mid-file insert failure. Each statement autocommits, so the
            # source_block rows written above this point are already durable —
            # but the rules that would have referenced them may not be. Delete
            # any block for THIS file that ended up with no rule, so a failed
            # ingest never leaves an empty block behind, then re-raise so the
            # file is logged as failed exactly as before. The success path can't
            # orphan a block (every `referenced` block gets >=1 rule), so this
            # only does work when an insert actually raised.
            c.execute(
                "DELETE FROM source_block WHERE rules_file_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM rule r "
                "WHERE r.source_block_id = source_block.id)",
                [rules_file_id],
            )
            raise

    if state:
        state.rules_new += file_inserted
    print(f"[crawl] {ff.project_owner}/{ff.project_name}:{ff.file_path}  +{file_inserted} rules")
    return file_inserted


# -----------------------------------------------------------------------------
# Discovery — yields FetchedFile from the open internet
# -----------------------------------------------------------------------------

def _collect_query(kind: str, query: str, paths: tuple[str, ...] | None,
                   source_kind: str | None, seen: set[tuple[str, str, str]],
                   state: CrawlState | None) -> list[FetchedFile]:
    """Run ONE discovery query to completion under a single short-lived
    connection, returning the matched files. The connection is used only for the
    SKIP_RECENT_HOURS recency reads (the marketplace adapters take it for their
    per-plugin recency reads too) and is closed before the caller ingests any
    file — so the crawler holds no DB handle across the network-bound walk or the
    per-file LLM ingest that follows. Dedupe state (`seen`) is shared across
    queries and mutated in place."""
    out: list[FetchedFile] = []
    with conn() as c:
        if kind == "repo":
            for r in _search_repos(query, max_results=MAX_REPOS_PER_QUERY):
                if _stop_requested(state):
                    break
                owner = r["owner"]["login"]
                name = r["name"]
                if _is_recently_crawled(c, owner, name):
                    continue
                for path in (paths or ()):
                    key = (owner, name, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    ff = _fetch_one(owner, name, path, source_kind)
                    if ff:
                        out.append(ff)
        elif kind == "code":
            for ff in _code_search(query, source_kind, MAX_FILES_PER_CODE_QUERY):
                if _stop_requested(state):
                    break
                key = (ff.project_owner, ff.project_name, ff.file_path)
                if key in seen:
                    continue
                seen.add(key)
                if _is_recently_crawled(c, ff.project_owner, ff.project_name):
                    continue
                out.append(ff)
        elif kind == "marketplace_code":
            for ff in discover_marketplaces_via_code_search(c, query, MAX_FILES_PER_CODE_QUERY):
                if _stop_requested(state):
                    break
                out.extend(_maybe_yield(ff, seen))
        elif kind == "solo_plugin_code":
            for ff in discover_solo_plugins_via_code_search(c, query, MAX_FILES_PER_CODE_QUERY):
                if _stop_requested(state):
                    break
                out.extend(_maybe_yield(ff, seen))
        elif kind == "marketplace_topic":
            for ff in discover_marketplaces_via_topic_search(c, query, MAX_REPOS_PER_QUERY):
                if _stop_requested(state):
                    break
                out.extend(_maybe_yield(ff, seen))
        else:
            raise ValueError(f"unknown discovery kind: {kind}")
    return out


def _discover_open_internet(state: CrawlState | None,
                            run_id: str | None) -> Iterator[FetchedFile]:
    """Iterate the trending queries, yield candidate files. Skips repos crawled
    in the last SKIP_RECENT_HOURS and dedupes (owner, repo, path) within a pass.

    Each query is collected to completion under its own short-lived connection
    (_collect_query) and only THEN yielded, so no DB connection is ever held
    across the per-file ingest the consumer runs between yields. The only cost
    is bounded over-fetch: if the consumer stops mid-batch (cap/stop), the
    current query's already-collected files were fetched but not processed —
    later queries are never started."""
    seen: set[tuple[str, str, str]] = set()
    for kind, query, paths, source_kind in _DISCOVERY_QUERIES:
        if _stop_requested(state):
            return
        if state:
            state.last_message = f"discover ({kind}): {query[:60]}"
        _event_short(run_id, "info", f"discover_start ({kind}): {query}", {"query": query})
        try:
            batch = _collect_query(kind, query, paths, source_kind, seen, state)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"[discover] query {query!r} failed: {msg}", file=sys.stderr)
            traceback.print_exc()
            _event_short(
                run_id, "error",
                f"discover_failed ({kind}): {query[:80]} — {type(e).__name__}",
                {"query": query, "error": msg},
            )
            continue
        for ff in batch:
            if _stop_requested(state):
                return
            yield ff


# -----------------------------------------------------------------------------
# Run one pass
# -----------------------------------------------------------------------------

def _handle_cap_stop(run_id: str | None, state: CrawlState) -> None:
    """Hard-stop the crawl because the session spend cap was reached.

    Persists continuous_mode='off' so the admin auto-resume (admin_app boot)
    does NOT immediately relaunch the loop straight back into the same cap, sets
    stop_event so the continuous loop exits after this pass, logs a warn event,
    and mirrors the final session spend to crawl_settings for the UI. Uses its
    own short-lived connection — run_one_pass no longer holds one open."""
    cap = state.cap_usd if state.cap_usd is not None else 0.0
    with conn() as c:
        set_setting(c, "continuous_mode", "off")
        set_setting(c, "session_spend_usd", f"{state.spend_usd:.6f}")
        _event(
            c, run_id, "warn",
            f"spend_cap_reached: ${state.spend_usd:.4f} >= cap ${cap:.4f} — hard-stop",
            {"spend_usd": round(state.spend_usd, 6),
             "cap_usd": cap,
             "by_model": {k: round(v, 6) for k, v in state.spend_by_model.items()}},
        )
    state.stop_event.set()
    state.last_message = f"spend cap ${cap:.2f} reached — hard-stopped"
    print(f"[crawl] spend cap ${cap:.2f} reached at ${state.spend_usd:.4f} — hard-stop")


def run_one_pass(state: CrawlState | None = None) -> None:
    """One pass = one discovery sweep + per-file ingest. Creates its own
    ingestion_run row, updates progress, finalises status on exit.

    Connection lifetime: this function holds NO long-lived connection. It opens
    a short connection to create the run row + read per-pass settings, then runs
    discovery and ingest with each unit of work owning its own short-lived
    connection (discovery: _collect_query; per file: _process_file; events:
    _event_short; cap-stop: _handle_cap_stop), and finally opens one more short
    connection to finalise the run row. The writer is released between every
    file instead of being held for the whole multi-minute pass."""
    total_files = 0
    total_rules = 0
    final_status = "succeeded"
    final_error: str | None = None

    # --- Setup: short connection for the run row + per-pass settings ---------
    # The admin UI persists model/cap to crawl_settings; CLI / no-state runs
    # fall back to the config defaults and run uncapped.
    with conn() as c:
        run_id = c.execute(
            """
            INSERT INTO ingestion_run (status, source_query, crawler_version)
            VALUES ('running', ?, ?)
            RETURNING id
            """,
            ["open-internet:trending", CRAWLER_VERSION],
        ).fetchone()[0]
        ex_model = get_setting(c, "extractor_model", EXTRACTOR_MODEL) or EXTRACTOR_MODEL
        jd_model = get_setting(c, "judge_model", JUDGE_MODEL) or JUDGE_MODEL
        cap_raw = (get_setting(c, "spend_cap_usd", "") or "").strip()
        # Human-feedback guidance addendum (Revise flow). Read once per pass and
        # threaded into every file's extract() call so a mid-pass edit takes
        # effect on the next pass, not mid-pass.
        addendum = get_setting(c, GUIDANCE_ADDENDUM_SETTING, "") or ""
        # Build the judge once per pass. It reads the ambiguity_level enum at
        # init but does NOT retain the connection, so it stays valid after this
        # block closes. None when JUDGE_ENABLED is False — rules are still
        # stored, just without rule_llm_decision rows.
        judge = Judge(c, model=jd_model) if JUDGE_ENABLED else None
    # Connection closed — nothing below holds a DB handle across discovery/ingest.

    try:
        cap = float(cap_raw) if cap_raw else None
    except (TypeError, ValueError):
        cap = None
    if cap is not None and cap <= 0:
        cap = None
    if state is not None:
        state.run_id = run_id
        state.cap_usd = cap
    print(f"[crawl] run_id={run_id}")

    try:
        for ff in _discover_open_internet(state, run_id):
            if _stop_requested(state):
                final_status = "partial"
                final_error = "ui_stop_requested"
                _event_short(run_id, "warn", "watchdog_stop:ui_stop_requested")
                break
            # Spend cap latched on a prior file → hard-stop before fetching
            # (and paying to extract) another file.
            if state is not None and state.cap_tripped:
                _handle_cap_stop(run_id, state)
                final_status = "partial"
                final_error = "spend_cap_reached"
                break
            try:
                total_rules += _process_file(ff, run_id, state, judge=judge,
                                             extractor_model=ex_model,
                                             guidance_addendum=addendum)
                total_files += 1
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"[crawl] file {ff.project_owner}/{ff.project_name}:{ff.file_path} failed: {msg}", file=sys.stderr)
                _event_short(
                    run_id, "error",
                    f"file_failed: {ff.project_owner}/{ff.project_name} · {ff.file_path}",
                    {"project": f"{ff.project_owner}/{ff.project_name}",
                     "path": ff.file_path,
                     "error": msg},
                )
            # Cap may have latched while processing this file's rules.
            if state is not None and state.cap_tripped:
                _handle_cap_stop(run_id, state)
                final_status = "partial"
                final_error = "spend_cap_reached"
                break

        print(f"\n[crawl] inserted {total_rules} new rules across {total_files} files")
    except Exception as e:
        final_status = "failed"
        final_error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        # --- Finalize: short connection for the run-row UPDATE + spend mirror -
        with conn() as c:
            c.execute(
                """
                UPDATE ingestion_run
                   SET status          = ?,
                       finished_at     = now(),
                       files_fetched   = ?,
                       rules_extracted = ?,
                       rules_new       = ?,
                       error_message   = ?
                 WHERE id = ?
                """,
                [final_status, total_files, total_rules, total_rules, final_error, run_id],
            )
            if state is not None:
                # Mirror the running session spend to the DB so the UI (and any
                # post-hoc inspection) can read it across Streamlit reruns.
                set_setting(c, "session_spend_usd", f"{state.spend_usd:.6f}")
        if state is not None:
            state.status = final_status
            state.error = final_error


# -----------------------------------------------------------------------------
# Continuous mode (local version of ADR-001 §Decision)
# -----------------------------------------------------------------------------

def continuous_loop(state: CrawlState) -> None:
    """Run one discovery pass, sleep PASS_INTERVAL_SECONDS, repeat — until
    stop_event. Re-crawls of the same repo are cheap thanks to rules_file
    UNIQUE constraints + the SKIP_RECENT_HOURS short-circuit, so the loop is
    effectively idempotent. Laptop sleep is handled by the OS suspending the
    whole process; on wake the loop continues from its current iteration."""
    state.status = "running"
    state.error = None
    state.last_message = "continuous mode started"
    # Session-scoped spend cap: the ledger zeroes each time the worker (re)starts.
    state.reset_spend()

    # Wire the GitHub adapter's rate-limit pause to our stop event so a
    # multi-minute "wait for cap reset" doesn't make Stop unresponsive.
    set_github_stop_event(state.stop_event)
    try:
        _continuous_loop_inner(state)
    finally:
        set_github_stop_event(None)


def _continuous_loop_inner(state: CrawlState) -> None:
    last_pass_end: float = 0.0
    pass_n = 0
    while not state.stop_event.is_set():
        now = time.time()
        wait_remaining = (last_pass_end + PASS_INTERVAL_SECONDS) - now
        if pass_n == 0 or wait_remaining <= 0:
            pass_n += 1
            state.last_message = f"pass {pass_n} started"
            try:
                run_one_pass(state=state)
            except Exception as e:
                state.error = f"pass {pass_n}: {type(e).__name__}: {e}"
                traceback.print_exc()
            last_pass_end = time.time()
            secs = int(PASS_INTERVAL_SECONDS)
            label = f"{secs} s" if secs < 60 else f"{secs // 60} min"
            state.last_message = f"pass {pass_n} done — next pass in {label}"
        else:
            state.stop_event.wait(timeout=min(POLL_INTERVAL_SECONDS, wait_remaining))

    state.status = "stopped"
    if state.cap_tripped:
        cap = state.cap_usd if state.cap_usd is not None else 0.0
        state.last_message = f"spend cap ${cap:.2f} reached — hard-stopped"
    else:
        state.last_message = "stopped by user"


# -----------------------------------------------------------------------------
# Maintenance — clear extracted data (admin UI "Maintenance" tab)
# -----------------------------------------------------------------------------

# Tables wiped by clear_extracted_data(), in FK-safe delete order (children
# before parents — DuckDB checks FKs per statement, so a parent can't go first).
# labeler, crawl_settings, ingestion_run, and ingestion_event are deliberately
# PRESERVED: labeler IDENTITIES, ops settings (models / cap / guidance addendum /
# continuous toggle), and the crawl audit log all survive a corpus wipe. No
# preserved table references a wiped one, so deleting every row here violates no
# surviving FK.
_CLEAR_DELETE_ORDER: tuple[str, ...] = (
    "rule_classification_label",   # → rule, labeler, rule_llm_decision
    "rule_extraction_label",       # → rule, labeler
    "rule_comment",                # → rule, labeler
    "source_block_comment",        # → source_block, labeler
    "rule_llm_decision",           # → rule
    "rule",                        # → rules_file, source_block
    "source_block",                # → rules_file
    "rules_file",                  # → source_project
    "source_project",              # root
)


def clear_extracted_data() -> dict[str, int]:
    """Erase ALL extracted content + its human labels/comments, preserving
    labeler identities, crawl_settings, and the ingestion_run/ingestion_event
    audit log. Returns {table: rows_deleted} (the pre-delete counts).

    Each DELETE autocommits on its own (NOT one big transaction). DuckDB checks
    foreign keys eagerly but does NOT see transaction-local deletes of a
    referenced row, so wrapping the whole wipe in one BEGIN/COMMIT makes
    deleting a parent fail even though its child was already deleted earlier in
    the same transaction (its "over-eager constraint checking" limitation).
    Autocommitting each statement sidesteps that: every child delete is durable
    before its parent's FK check runs. Children precede parents in
    _CLEAR_DELETE_ORDER, so the wipe is FK-safe throughout — and because that
    order keeps the corpus FK-valid at every step, a mid-wipe failure just
    leaves fewer rows to clear and re-running finishes the job (idempotent)."""
    deleted: dict[str, int] = {}
    with conn() as c:
        for tbl in _CLEAR_DELETE_ORDER:
            deleted[tbl] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        for tbl in _CLEAR_DELETE_ORDER:
            c.execute(f"DELETE FROM {tbl}")
    return deleted


# -----------------------------------------------------------------------------
# Manual ingest — drag-and-drop queue (admin UI "Upload" tab)
# -----------------------------------------------------------------------------

# Synthetic provenance for hand-uploaded files. All uploads share one project so
# they're easy to find/clear; the per-file dedup key is (project, path,
# commit_sha) where commit_sha is a short content hash (see make_manual_fetched_file).
MANUAL_PROJECT_HOST = "manual"
MANUAL_PROJECT_OWNER = "upload"
MANUAL_PROJECT_NAME = "manual-uploads"
MANUAL_PROJECT_URL = "manual://upload/manual-uploads"

# Filename → rules_file_kind inference for uploads. Best-effort; the admin UI
# pre-selects this and lets the user override. Most specific suffix wins.
_MANUAL_KIND_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    ("claude.md", "claude_md"),
    ("agents.md", "agents_md"),
    ("copilot-instructions.md", "copilot_instructions"),
    ("conventions.md", "aider_conventions"),
    ("skill.md", "claude_plugin_skill"),
    (".cursorrules", "cursor_rules"),
    (".mdc", "cursor_rules"),
    (".windsurfrules", "windsurf_rules"),
    (".clinerules", "cline_rules"),
    ("llms.txt", "llms_txt"),
)


def infer_manual_kind(filename: str) -> str:
    """Best-effort rules_file_kind from a dropped filename; 'other' as fallback.
    The admin UI uses this to pre-select the per-file kind dropdown."""
    name = filename.rsplit("/", 1)[-1].lower()
    for needle, kind in _MANUAL_KIND_BY_SUFFIX:
        if name == needle or name.endswith(needle):
            return kind
    return "other"


def make_manual_fetched_file(filename: str, content: str, source_kind: str) -> FetchedFile:
    """Build a synthetic FetchedFile for one uploaded source file. commit_sha is
    a short content hash so re-uploading byte-identical content is a no-op
    (is_new=False, reported 'duplicate'), while an edited re-upload of the same
    filename ingests as a fresh rules_file row."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return FetchedFile(
        project_host=MANUAL_PROJECT_HOST,
        project_owner=MANUAL_PROJECT_OWNER,
        project_name=MANUAL_PROJECT_NAME,
        project_canonical_url=MANUAL_PROJECT_URL,
        file_path=filename,
        source_kind=source_kind,
        raw_content=content,
        commit_sha=digest,
    )


@dataclasses.dataclass
class ManualIngestItem:
    """One uploaded file's outcome. status ∈ {ingested, empty, manifest,
    duplicate, failed}."""
    filename: str
    source_kind: str
    status: str
    rules_inserted: int = 0
    error: str | None = None


@dataclasses.dataclass
class ManualIngestResult:
    run_id: str | None
    items: list[ManualIngestItem]
    total_rules: int
    spend_usd: float


def _fetched_is_duplicate(ff: FetchedFile) -> bool:
    """True if this exact (project canonical_url, path, content hash) was already
    ingested — lets the UI report 'duplicate' distinctly from 'new but no rules'
    (_process_file returns 0 for both). Keyed on ff.project_canonical_url so it
    works for both manual uploads (the synthetic MANUAL_PROJECT_URL) and links
    (which dedupe against whatever the crawler has already stored, e.g. a GitHub
    repo we've crawled before)."""
    with conn() as c:
        row = c.execute(
            """
            SELECT 1
              FROM rules_file rf
              JOIN source_project sp ON sp.id = rf.project_id
             WHERE sp.canonical_url = ? AND rf.path = ?
               AND (rf.commit_sha = ? OR (rf.commit_sha IS NULL AND ? IS NULL))
            """,
            [ff.project_canonical_url, ff.file_path, ff.commit_sha, ff.commit_sha],
        ).fetchone()
    return row is not None


# -----------------------------------------------------------------------------
# Link ingest — paste a URL instead of dropping a file (admin UI "Upload" tab)
# -----------------------------------------------------------------------------
# Four shapes are accepted, resolved to one-or-more FetchedFiles that flow
# through the very same _process_file pipeline as uploads and the crawler:
#   • a GitHub *file* URL  (blob/ or raw/)        → that one file
#   • a GitHub *repo* URL                         → probe canonical rule files,
#       OR (if it's a Claude marketplace/plugin)    walk the plugin manifest(s)
#   • a Claude marketplace (repo w/ marketplace.json) → manifest + every plugin file
#   • any other http(s) URL                       → fetched as raw text

def _link_display(ff: FetchedFile) -> str:
    """Compact provenance label for one resolved link file (UI + run events)."""
    base = (f"{ff.project_owner}/{ff.project_name}"
            if ff.project_host == "github.com" else ff.project_host)
    return f"{base} · {ff.file_path}" if ff.file_path else base


# Canonical rules files probed when a *bare* GitHub repo URL is given (no
# marketplace/plugin manifest). Mirrors the crawler's per-repo file set.
_LINK_REPO_PROBE: tuple[tuple[str, str], ...] = (
    ("AGENTS.md", "agents_md"),
    ("CLAUDE.md", "claude_md"),
    (".cursorrules", "cursor_rules"),
    (".windsurfrules", "windsurf_rules"),
    (".clinerules", "cline_rules"),
    (".github/copilot-instructions.md", "copilot_instructions"),
    ("CONVENTIONS.md", "aider_conventions"),
    ("llms.txt", "llms_txt"),
)

# github.com/<owner>/<repo>/(blob|raw)/<ref>/<path...>  → one file
_GH_FILE_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/"
    r"(?:blob|raw)/(?P<ref>[^/\s]+)/(?P<path>[^\s?#]+)")
# raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>  → one file
_GH_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/"
    r"(?P<ref>[^/\s]+)/(?P<path>[^\s?#]+)")
# github.com/<owner>/<repo>[/...]  → the repo (anything after owner/repo ignored)
_GH_REPO_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/\s#?]+)/(?P<repo>[^/\s#?]+)")


def classify_link(url: str) -> tuple[str, dict]:
    """Pure URL router — NO network. Returns (kind, parts) where kind is one of
    'github_file' | 'github_repo' | 'generic' | 'invalid'. Most specific match
    wins (a /blob/ URL is a file, not a repo)."""
    url = url.strip()
    m = _GH_FILE_RE.match(url) or _GH_RAW_RE.match(url)
    if m:
        return "github_file", {"owner": m["owner"],
                               "repo": m["repo"].removesuffix(".git"),
                               "path": m["path"]}
    m = _GH_REPO_RE.match(url)
    if m:
        return "github_repo", {"owner": m["owner"],
                               "repo": m["repo"].removesuffix(".git")}
    if re.match(r"^https?://", url):
        return "generic", {"url": url}
    return "invalid", {"reason": "expected an http(s):// URL"}


def resolve_link(url: str) -> tuple[list[FetchedFile], str]:
    """Resolve a pasted URL to zero-or-more FetchedFiles + a human note. Makes
    network calls. Empty list + note means "nothing ingestable found" (the note
    explains why); the caller surfaces it as an 'empty'/'failed' item."""
    kind, parts = classify_link(url)
    if kind == "invalid":
        return [], parts["reason"]
    if kind == "github_file":
        ff = _fetch_one(parts["owner"], parts["repo"], parts["path"],
                        infer_manual_kind(parts["path"]))
        if ff is None:
            return [], (f"GitHub file not found (or >1MB): "
                        f"{parts['owner']}/{parts['repo']}/{parts['path']}")
        return [ff], f"GitHub file {parts['owner']}/{parts['repo']}/{parts['path']}"
    if kind == "github_repo":
        return _resolve_github_repo(parts["owner"], parts["repo"])
    return _resolve_generic_url(parts["url"])


def _resolve_github_repo(owner: str, repo: str) -> tuple[list[FetchedFile], str]:
    """A bare repo URL. First check whether it's a Claude marketplace or solo
    plugin (cheap manifest probe gates the walk); otherwise probe the canonical
    rules files. c=None on the discover_* calls disables the crawler's 24h
    per-plugin recency skip — a manual pick should always fetch."""
    mkt = list(discover_marketplace(None, owner, repo))
    if mkt:  # discover_marketplace yields the manifest first iff it exists
        return mkt, f"Claude marketplace {owner}/{repo} — {len(mkt)} file(s)"
    # Solo plugin: gate on an explicit plugin.json probe so a plain repo that
    # merely has a commands/ dir isn't misread as a plugin.
    if _fetch_one(owner, repo, ".claude-plugin/plugin.json",
                  "claude_plugin_manifest") is not None:
        plug = list(discover_solo_plugin(None, owner, repo))
        return plug, f"Claude plugin {owner}/{repo} — {len(plug)} file(s)"
    files: list[FetchedFile] = []
    for path, kind in _LINK_REPO_PROBE:
        ff = _fetch_one(owner, repo, path, kind)
        if ff is not None:
            files.append(ff)
    return files, f"GitHub repo {owner}/{repo} — {len(files)} rule file(s)"


def _resolve_generic_url(url: str) -> tuple[list[FetchedFile], str]:
    """Any other http(s) URL: fetch the body as raw text and treat it as one
    source file. commit_sha is a content hash so re-fetching identical content
    dedupes."""
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "rules-prototype/0.1"})
        r.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        return [], f"fetch failed ({type(e).__name__})"
    content = r.text
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "link"
    path = parsed.path.lstrip("/") or "index"
    filename = path.rsplit("/", 1)[-1] or host
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    ff = FetchedFile(
        project_host=host,
        project_owner=host,
        project_name=(path.split("/", 1)[0] if "/" in path else host),
        project_canonical_url=f"{parsed.scheme or 'https'}://{host}",
        file_path=path,
        source_kind=infer_manual_kind(filename),
        raw_content=content,
        commit_sha=digest,
    )
    return [ff], f"link {host}/{path}"


def _ingest_fetched_batch(fetched: list[tuple[FetchedFile, str]],
                          run_label: str,
                          pre_items: list[ManualIngestItem] | None = None,
                          ) -> ManualIngestResult:
    """Shared core for both manual uploads and link ingest: run a batch of
    already-resolved FetchedFiles through the SAME pipeline the crawler uses
    (_process_file → extract/embed/judge → DB write).

    `fetched` is a list of (FetchedFile, display_label) pairs — the label is the
    human name shown per row (a filename for uploads, an owner/repo·path for
    links). `pre_items` are outcomes computed *before* the pipeline (e.g. links
    that resolved to nothing) and are surfaced ahead of the processed rows.

    One ingestion_run is created for the batch; the configured extractor/judge
    model + guidance addendum are read from crawl_settings. Returns per-item
    outcomes and the batch's estimated spend. Synchronous (the UI shows a
    spinner) — batches are small and user-initiated, so there's no spend cap and
    no stop-event wiring."""
    items: list[ManualIngestItem] = list(pre_items or [])
    total = 0
    # Throwaway state purely to capture this batch's spend without touching the
    # crawler's session ledger or the persisted spend mirror.
    work_state = CrawlState(heartbeat=time.time(), stop_event=threading.Event())
    work_state.reset_spend()

    with conn() as c:
        run_id = c.execute(
            """
            INSERT INTO ingestion_run (status, source_query, crawler_version)
            VALUES ('running', ?, ?)
            RETURNING id
            """,
            [run_label, CRAWLER_VERSION],
        ).fetchone()[0]
        ex_model = get_setting(c, "extractor_model", EXTRACTOR_MODEL) or EXTRACTOR_MODEL
        jd_model = get_setting(c, "judge_model", JUDGE_MODEL) or JUDGE_MODEL
        addendum = get_setting(c, GUIDANCE_ADDENDUM_SETTING, "") or ""
        judge = Judge(c, model=jd_model) if JUDGE_ENABLED else None

    final_status = "succeeded"
    try:
        for ff, label in fetched:
            if _fetched_is_duplicate(ff):
                items.append(ManualIngestItem(label, ff.source_kind, "duplicate"))
                _event_short(run_id, "info", f"manual_duplicate: {label}",
                             {"path": ff.file_path})
                continue
            try:
                inserted = _process_file(ff, run_id, work_state, judge=judge,
                                         extractor_model=ex_model,
                                         guidance_addendum=addendum)
                if ff.source_kind in _NON_EXTRACTABLE_KINDS:
                    status = "manifest"
                elif inserted > 0:
                    status = "ingested"
                else:
                    status = "empty"
                items.append(ManualIngestItem(label, ff.source_kind, status,
                                              rules_inserted=inserted))
                total += inserted
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                items.append(ManualIngestItem(label, ff.source_kind, "failed", error=msg))
                _event_short(run_id, "error", f"manual_file_failed: {label}",
                             {"path": ff.file_path, "error": msg})
    except Exception as e:
        final_status = "failed"
        print(f"[manual] batch failed: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        with conn() as c:
            c.execute(
                """
                UPDATE ingestion_run
                   SET status = ?, finished_at = now(),
                       files_fetched = ?, rules_extracted = ?, rules_new = ?
                 WHERE id = ?
                """,
                [final_status, len(items), total, total, run_id],
            )

    return ManualIngestResult(run_id=run_id, items=items, total_rules=total,
                              spend_usd=work_state.spend_usd)


def ingest_manual_files(files: list[tuple[str, str, str]],
                        run_label: str = "manual-upload") -> ManualIngestResult:
    """Ingest a batch of hand-uploaded source files. `files` is a list of
    (filename, content, source_kind) triples — source_kind is a rules_file_kind
    enum value (the UI infers + lets the user override it). Thin wrapper over
    _ingest_fetched_batch; the per-row label is the filename."""
    fetched = [(make_manual_fetched_file(fn, content, kind), fn)
               for fn, content, kind in files]
    return _ingest_fetched_batch(fetched, run_label)


def ingest_links(urls: list[str],
                 run_label: str = "manual-link") -> ManualIngestResult:
    """Ingest a batch of pasted URLs. Each URL is resolved (see resolve_link) to
    zero-or-more FetchedFiles — a GitHub file/repo, a Claude marketplace/plugin,
    or any raw http(s) page — then run through the shared pipeline. URLs that
    resolve to nothing (404, non-repo, fetch error) become 'empty'/'failed' rows
    so the user sees why. Network calls happen here, before the DB batch."""
    fetched: list[tuple[FetchedFile, str]] = []
    pre: list[ManualIngestItem] = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        try:
            files, note = resolve_link(url)
        except Exception as e:
            pre.append(ManualIngestItem(url, "other", "failed",
                                        error=f"{type(e).__name__}: {e}"))
            continue
        if not files:
            pre.append(ManualIngestItem(url, "other", "empty", error=note))
            continue
        for ff in files:
            fetched.append((ff, _link_display(ff)))
    return _ingest_fetched_batch(fetched, run_label, pre_items=pre)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

@click.command()
@click.option("--stats", "do_stats", is_flag=True, help="Print row counts and exit.")
def main(do_stats: bool) -> None:
    if do_stats:
        with conn(read_only=True) as c:
            for tbl in ("source_project", "rules_file", "source_block", "rule",
                        "rule_llm_decision", "rule_extraction_label",
                        "rule_classification_label", "rule_comment",
                        "source_block_comment", "labeler",
                        "ingestion_run", "ingestion_event"):
                count = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                print(f"{tbl:26s} {count:>8d}")
        return

    # Default: run one discovery pass.
    run_one_pass(state=None)


if __name__ == "__main__":
    main()
