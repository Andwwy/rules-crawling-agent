# Rules-in-the-Wild — Local Prototype

A fully local prototype that ingests real agent-rule files from public GitHub and turns them into labeling-ready rows for the labeling platform. It validates two specs end-to-end: the two-pass segmentation/extraction in [`extraction_guide.md`](../../../extraction_guide.md) and the v0.4 DuckDB schema in [`db/duckdb_schema.sql`](db/duckdb_schema.sql) (kept aligned with the labeling platform's `schema-v0.4.duckdb.sql`).

Everything runs on your laptop. No AWS, no Supabase, no public URL. You bring **one** API key — `PERPLEXITY_API_KEY` — and an optional GitHub token. Perplexity covers all three LLM keypaths through one key: the **extractor** and the **judge** (both `anthropic/claude-haiku-4-5` via the Agent API, passthrough pricing) and 1024-d **embeddings** (`pplx-embed-v1-0.6b`, int8-quantized, ~$0.004 per million tokens). Data lives in a single DuckDB file inside a named volume (or in MotherDuck — see below); `docker compose down -v` wipes the local file clean.

The Haiku **judge ships disabled** (`JUDGE_ENABLED=False` in `src/config.py`) so you can iterate on extraction + embedding without burning judge tokens. The **extractor is always on** — it's one Haiku call per file and the only way rules get produced. Flip the judge on once the extraction output looks right. Production (per `../../Design docs/DESIGN.md`) swaps the Perplexity passthrough for the Anthropic SDK directly and `pplx-embed-v1-0.6b` for `voyage-3` (also 1024-d, so the schema column doesn't move) — see the swap notes in `src/config.py` / `src/embedder.py`.

## What this prototype does

1. **Discovers** trending agent projects on public GitHub each pass — `topic:agents`, `topic:agentic-ai`, `topic:cursor-rules`, `topic:claude-code` via `/search/repositories`, plus direct `path:AGENTS.md`, `path:CLAUDE.md`, `path:.cursorrules` via `/search/code`. Each repo is probed for the canonical filenames; repos crawled in the past 24 h are skipped, so passes naturally explore new content as GitHub's trending set rotates.
2. **Segments + extracts** each source file in a single two-pass LLM call (per `extraction_guide.md`): Pass 1 splits the file into `source_block`s (heading sections, paragraphs, lists, fenced code); Pass 2 emits atomic, normalized `rule`s — compound rules split, gray-zone cases resolved, each carrying an `extraction_reason` and a back-reference to its source block. Only rule-bearing blocks are persisted. Static (no-execution) quality signals the extractor can compute on each rule — imperative force, concrete grounding, intra-file dedup, hierarchy compliance, reference validity against the live repo — and how they relate to trajectory-driven counters like ACE's helpful/harmful are documented in [`Design docs/RESEARCH-static-rule-quality-metrics.md`](Design%20docs/RESEARCH-static-rule-quality-metrics.md).
3. **Embeds** each extracted rule (1024-d via Perplexity `pplx-embed-v1-0.6b`) and stores the vector on `rule.embedding` for later similarity/dedup work. (Clustering itself is not part of this prototype.)
4. (Optional, off by default) **Classifies** each rule on the 4-axis taxonomy via Claude Haiku — `prerequisites` / `enforcement_mechanisms` / `triggers` (free-form `TEXT[]`) plus an `ambiguity_level` enum and `ambiguity_notes`. Every call is logged to `rule_llm_decision` (prompt, raw response, parse status, latency, tokens), so bad parses are captured rather than dropped.
5. **Writes** everything to the v0.4 DuckDB/MotherDuck schema the labeling platform reads. The crawler owns table creation (`db/duckdb_schema.sql` is applied idempotently on first connect); the labeling platform connects to the same DB and only reads/edits rows.
6. **Serves** a Streamlit admin UI at `http://localhost:8501` to start/stop the continuous crawler, browse the extracted corpus (rules + their source blocks + judge axes), and watch row counts.

## Quick start — Docker (recommended)

> **Requires Docker.** It's the only thing you need to install — Python, DuckDB, and every dependency are baked into the image. This assumes Docker is already installed and running (Docker Desktop on macOS/Windows, or Docker Engine + the Compose v2 plugin on Linux).

```bash
cp .env.example .env       # fill in PERPLEXITY_API_KEY, optionally GITHUB_TOKEN
docker compose up -d       # builds the image, starts admin (Streamlit + DuckDB)
open http://localhost:8501 # the admin UI
```

That's the whole local server. The first `docker compose up` builds the image (~1 min); later starts are instant. Follow logs with `docker compose logs -f admin`.

> **After editing `.env`** — `docker compose up -d --force-recreate admin`. docker-compose reads `.env` at container *create* time only; a plain `restart` keeps the old env.

Inside the container the DuckDB file lives at `/data/rules.duckdb` in the `crawling-agent_duckdata` named volume. `docker cp rules_admin_ui:/data/rules.duckdb ./rules.duckdb` copies it to host.

Then in the browser: open the **Crawl** tab → click **Start continuous crawl**. A preflight pings the embedder, the extractor, the judge (if enabled), and GitHub `/rate_limit`, and reports timings. On success the crawler enters continuous mode: a background thread re-runs the discovery queries every 60 seconds, accumulating new files and rules into the same DuckDB file. The `source_project.last_crawled_at`-based 24 h skip prevents re-fetching the same repos each minute. Progress shows in the **Corpus** panel.

## Quick start — host (no Docker)

Prefer not to use Docker? Run the same code straight from a Python venv:

```bash
cp .env.example .env                 # fill in PERPLEXITY_API_KEY, optionally GITHUB_TOKEN
set -a; source .env; set +a          # load keys into the shell
uv venv && source .venv/bin/activate # or: python -m venv .venv && source .venv/bin/activate
uv pip install -r pyproject.toml     # or: pip install -e .
streamlit run src/admin_app.py
open http://localhost:8501
```

The DuckDB file lands at `./data/rules.duckdb` (host-relative). `./data/` is created on first run. Browse the file with any local DuckDB tool: `duckdb ./data/rules.duckdb`, DBeaver, or your editor's DuckDB plugin.

## MotherDuck (shared DB with the labeling platform)

To point the crawler and the labeling platform at the **same** cloud database, set `MOTHERDUCK_TOKEN` (and optionally `MOTHERDUCK_DB`, default `rules_in_the_wild`) in `.env`. When set, both the crawler and the admin connect to MotherDuck instead of the local `/data/rules.duckdb` file. The crawler still applies `db/duckdb_schema.sql` on first connect, so the labeling platform sees v0.4 tables without doing any DDL itself. Get a service token at <https://app.motherduck.com> → Settings → Service tokens.

**Continuous mode is persistent.** Clicking Start writes `continuous_mode = on` into a `crawl_settings` table. When the admin process restarts (Ctrl-C, `docker compose restart`, machine reboot), Streamlit reads that setting and re-spawns the thread — but the re-spawn fires when the admin **script next runs**, which is on the first browser visit after restart, not at process boot. In practice: restart admin, open the tab, the crawl resumes. The production architecture in `../../Design docs/ADR-001-24-7-crawler-architecture.md` uses a separate worker container precisely so this isn't tab-dependent; for a single-laptop prototype it's the right trade-off. Clicking **Stop** flips the setting back to `off`.

**What stops vs. doesn't stop the crawl:**
- **Tab close**: no effect. The crawler is a server, not a tab-bound thing.
- **Laptop sleep / close**: OS suspends the whole process; on wake, the thread continues from where it was. No state is lost: `rules_file UNIQUE (project_id, path, commit_sha)` plus the `is_new` gate means each file is extracted **exactly once**, so a re-run never re-extracts or duplicates rules. (The `rule` and `source_block` span indexes are intentionally **non-unique** — multiple rules legitimately share one line range after a compound split — so idempotency rests on the file-level gate, not a row-level UNIQUE.)
- **Admin restart**: setting persists; thread re-spawns on next boot.
- **Stop button**: sets `stop_event`, thread exits at the next pass-interval check. Setting flipped to `off` so the crawler doesn't auto-restart.

**Token note.** GitHub's `/search/repositories` and `/search/code` endpoints both require authentication for any meaningful rate budget — 60 anonymous req/hr is essentially useless. Without `GITHUB_TOKEN`, the preflight warns that `code_search` discovery seeds will be skipped (hand-picked + tree seeds still work). With a token: 5 000 core req/hr + 30 search req/min, comfortable for the prototype's ~9 queries × 25 repos × 3 paths per pass.

## CLI mode (host only)

DuckDB is single-process-write, so the CLI lives outside Docker — run it from the host venv with the admin stopped. Same code, same database file (just at `./data/rules.duckdb` instead of inside the volume).

```bash
docker compose stop admin              # release the writer lock
source .venv/bin/activate              # the host venv from Quick start
set -a; source .env; set +a            # load PERPLEXITY_API_KEY / GITHUB_TOKEN

python -m src.crawl                    # one discovery pass
python -m src.crawl --stats            # row counts

docker compose start admin             # bring the UI back when done
```

If you run the host venv against the Docker volume's DuckDB file, point `DUCKDB_PATH` at it: `DUCKDB_PATH=/var/lib/docker/volumes/prototype_duckdata/_data/rules.duckdb python -m src.crawl --stats`. On macOS that path is inside the Docker VM, so the easiest path is `docker cp rules_admin_ui:/data/rules.duckdb ./data/rules.duckdb` first, then run the CLI on the local copy. (With `MOTHERDUCK_TOKEN` set, the CLI talks to MotherDuck and the writer-lock dance doesn't apply.)

## Browsing the corpus

After your first ingest, open `http://localhost:8501` → **Browse** tab. Each rule shows its normalized `rule_text`, the **Why extracted** reason from the extractor, the source-block snippet it came from, and — when the judge is enabled — the four taxonomy axes and ambiguity notes. Filter by source kind, project, free text, and (judge on) `ambiguity_level`. The **Stats** tab prints live row counts per table.

## Layout

```
crawling-agent/
├── docker-compose.yml         # admin service only — Streamlit + crawler thread + DuckDB
├── Dockerfile                 # python:3.12-slim + duckdb wheels
├── pyproject.toml             # uv-managed deps (no torch, no postgres libs)
├── .env.example
├── db/
│   └── duckdb_schema.sql      # v0.4 DuckDB schema applied on first connect (crawler owns DDL)
└── src/
    ├── config.py              # models + prompt versions + JUDGE_ENABLED + EMBEDDING_MODEL
    ├── db.py                  # DuckDB/MotherDuck connection helper + enum_values() + setting helpers
    ├── llm.py                 # shared Perplexity Agent client + tolerant JSON parsing (extractor + judge)
    ├── adapters/
    │   ├── base.py            # FetchedFile dataclass + Adapter Protocol
    │   └── github.py          # _fetch_one, _search_repos, _code_search, GitHubAdapter
    ├── extractor.py           # LLM two-pass: file → source_blocks + atomic normalized rules
    ├── embedder.py            # Perplexity /v1/embeddings client (1024-d int8)
    ├── judge.py               # 4-axis taxonomy classifier (skipped when JUDGE_ENABLED=False)
    ├── api_smoke.py           # preflight: embedder + extractor + judge + GitHub /rate_limit
    ├── crawl.py               # discovery + ingest; _DISCOVERY_QUERIES, run_one_pass, continuous_loop
    └── admin_app.py           # Streamlit UI: Crawl / Browse / Stats tabs
```

## Tearing it down

Host mode:

```bash
# Ctrl-C the streamlit process. To start fresh:
rm -rf ./data
```

Docker mode:

```bash
docker compose down          # stop containers; DuckDB file in the duckdata volume survives
docker compose down -v       # stop containers + wipe the DuckDB file (clean slate)
```
