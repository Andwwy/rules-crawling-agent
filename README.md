# Rules-in-the-Wild — Crawling Agent

Crawls public GitHub for agent-rule files (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, …), extracts atomic rules with an LLM, embeds them, and writes them to DuckDB/MotherDuck. Ships a Streamlit admin UI at `http://localhost:8501`.

## Prerequisites

- **Docker** (Docker Desktop, or Engine + Compose v2) — assumed already installed and running.
- A `PERPLEXITY_API_KEY` (covers the extractor, judge, and embeddings). `GITHUB_TOKEN` is optional but recommended.

## Spin up (Docker)

```bash
cp .env.example .env       # fill in PERPLEXITY_API_KEY, optionally GITHUB_TOKEN
docker compose up -d       # builds the image + starts the admin UI
open http://localhost:8501
```

Then in the UI: **Crawl** tab → **Start continuous crawl**.

Useful commands:

```bash
docker compose logs -f admin                   # follow logs
docker compose up -d --force-recreate admin    # apply .env changes
docker compose down                            # stop (keeps the DuckDB volume)
docker compose down -v                         # stop + wipe the DuckDB volume
```

## Spin up (host, no Docker)

```bash
cp .env.example .env
set -a; source .env; set +a
uv venv && source .venv/bin/activate
uv pip install -r pyproject.toml
streamlit run src/admin_app.py
```

## MotherDuck (optional)

Set `MOTHERDUCK_TOKEN` (and optionally `MOTHERDUCK_DB`, default `rules_in_the_wild`) in `.env` to use the shared cloud DB instead of the local file. Get a token at <https://app.motherduck.com> → Settings → Service tokens.
