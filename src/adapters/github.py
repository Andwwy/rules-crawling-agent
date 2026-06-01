"""GitHub adapter — four modes:
    - file:        fetch one file at owner/repo/path
    - file_list:   fetch each (owner, repo, path) tuple from a list
    - tree:        walk a directory under path, yielding files matching glob
    - code_search: run a GitHub Code Search query, yield each file hit

Uses the GitHub REST API (no clone, no PyGithub). Honors GITHUB_TOKEN for
rate-limit headroom. The `code_search` mode REQUIRES a token in practice
because the anon Search API budget is 10 req/min.
"""

import base64
import fnmatch
import os
import threading
import time
import urllib.parse
from typing import Iterator

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .base import Adapter, FetchedFile


GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Rate-limit tracker
#
# Every response from api.github.com carries headers that tell us exactly how
# much budget we have left in the relevant bucket. We record those after every
# request and pause BEFORE the next request if the bucket is exhausted, so we
# never hit a 403. Authenticated caps as of 2026:
#   - core:    5,000 req/hr  (most endpoints, /repos/.../contents, /git/trees)
#   - search:    30 req/min  (/search/code, /search/repositories)
#   - graphql: 5,000 points/hr  (/graphql)
# Rate-limit headers are present on EVERY response, not just rate-limited ones,
# so the tracker auto-corrects after each call.
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_rate_buckets: dict[str, dict] = {}
# Set by the crawler thread so a multi-minute pause can be interrupted by Stop.
_external_stop_event: threading.Event | None = None
# Live "we're currently inside _maybe_pause_for_rate" indicator. Read by the
# admin UI so it can show a distinct 'waiting limit' status (vs 'running' or
# 'stopped'). Lives under _rate_lock so reads/writes are atomic.
_pause_active: bool = False
_pause_bucket: str | None = None
_pause_resets_at: int = 0


def set_stop_event(event: threading.Event | None) -> None:
    """Crawler thread calls this with its stop_event so the rate-limit pause
    can be cancelled when the user clicks Stop. None disables interruption."""
    global _external_stop_event
    _external_stop_event = event


def get_rate_state() -> dict[str, dict]:
    """Snapshot of all known buckets for the admin UI. Returns
    `{bucket: {limit, remaining, reset_at, checked_at}}`."""
    with _rate_lock:
        return {k: dict(v) for k, v in _rate_buckets.items()}


def is_paused() -> tuple[bool, str | None, int]:
    """Returns (active, bucket, resets_at) describing whether the rate-limit
    pause is currently sleeping a caller. `active=True` means a thread is
    blocked right now inside _maybe_pause_for_rate. Used by the admin UI to
    distinguish 'waiting limit' from 'running' / 'stopped'."""
    with _rate_lock:
        return _pause_active, _pause_bucket, _pause_resets_at


def _classify_bucket(url: str) -> str:
    if "/search/" in url:
        return "search"
    if "/graphql" in url:
        return "graphql"
    return "core"


def _record_rate_limit(r: httpx.Response, default_bucket: str) -> None:
    h = r.headers
    bucket = h.get("X-RateLimit-Resource", default_bucket)
    try:
        limit = int(h.get("X-RateLimit-Limit", 0))
        remaining = int(h.get("X-RateLimit-Remaining", 0))
        reset_at = int(h.get("X-RateLimit-Reset", 0))
    except (TypeError, ValueError):
        return
    # Secondary rate-limits (bursts) come back as 403 with Retry-After;
    # synthesize a bucket entry that forces a pause.
    if r.status_code == 403 and "retry-after" in h:
        try:
            retry_after = int(h["retry-after"])
        except ValueError:
            retry_after = 30
        reset_at = int(time.time()) + retry_after
        remaining = 0
    with _rate_lock:
        _rate_buckets[bucket] = {
            "limit": limit,
            "remaining": remaining,
            "reset_at": reset_at,
            "checked_at": int(time.time()),
        }


def _maybe_pause_for_rate(bucket: str, min_remaining: int = 1) -> None:
    """If the bucket has fewer than `min_remaining` requests left, sleep until
    reset_at. Sleeps in 5-second chunks so the crawler's stop_event (if set
    via set_stop_event) can interrupt the pause within ~5 s.

    While pausing, sets _pause_active / _pause_bucket / _pause_resets_at so
    the admin UI can show a distinct 'waiting limit' status."""
    global _pause_active, _pause_bucket, _pause_resets_at
    with _rate_lock:
        entry = _rate_buckets.get(bucket)
    if not entry or entry["remaining"] >= min_remaining:
        return
    reset_at = entry["reset_at"]
    print(
        f"[github] rate-limit pause: {bucket} bucket at "
        f"{entry['remaining']}/{entry['limit']}, sleeping until reset "
        f"(+{max(0, reset_at - int(time.time()))}s)"
    )
    with _rate_lock:
        _pause_active = True
        _pause_bucket = bucket
        _pause_resets_at = reset_at
    try:
        while True:
            delta = reset_at - int(time.time())
            if delta <= 0:
                return
            chunk = min(5, delta + 1)
            if _external_stop_event is not None and _external_stop_event.wait(timeout=chunk):
                print("[github] rate-limit pause interrupted by stop")
                return
            if _external_stop_event is None:
                time.sleep(chunk)
    finally:
        with _rate_lock:
            _pause_active = False
            _pause_bucket = None
            _pause_resets_at = 0


def _headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github+json", "User-Agent": "rules-prototype/0.1"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# Retry transient transport errors only. HTTP status errors (4xx/5xx) bubble up
# as httpx.HTTPStatusError so _fetch_one can return None on 404 without wasting
# three exponential-backoff retries on a permanent miss.
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=10),
    retry=retry_if_exception_type(httpx.RequestError),
    reraise=True,
)
def _get(url: str) -> httpx.Response:
    bucket = _classify_bucket(url)
    # Pause BEFORE the request if we're already at zero; the tracker is
    # updated from every prior response, so this pre-flight is free when
    # we have budget.
    _maybe_pause_for_rate(bucket)
    r = httpx.get(url, headers=_headers(), timeout=30)
    # Record after the call so the next caller (and the UI) sees fresh state.
    _record_rate_limit(r, default_bucket=bucket)
    r.raise_for_status()
    return r


def _fetch_one(owner: str, repo: str, path: str, source_kind: str) -> FetchedFile | None:
    """Returns None if the file doesn't exist or is too large to fetch via the contents API."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    try:
        r = _get(url)
    except httpx.HTTPStatusError:
        return None
    data = r.json()
    if isinstance(data, list) or data.get("type") != "file":
        return None
    if not data.get("content"):
        return None  # > 1MB; would need the blob API
    raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    sha = data.get("sha")
    return FetchedFile(
        project_host="github.com",
        project_owner=owner,
        project_name=repo,
        project_canonical_url=f"https://github.com/{owner}/{repo}",
        file_path=path,
        source_kind=source_kind,
        commit_sha=sha,
        raw_content=raw,
        snapshot_url=f"https://github.com/{owner}/{repo}",
    )


def _walk_tree(owner: str, repo: str, root_path: str, glob: str, source_kind: str) -> Iterator[FetchedFile]:
    """Uses the Git Trees API with recursive=1. Cheaper than walking contents/."""
    # First, look up the default branch SHA.
    repo_meta = _get(f"{GITHUB_API}/repos/{owner}/{repo}").json()
    default_branch = repo_meta["default_branch"]
    branch_meta = _get(f"{GITHUB_API}/repos/{owner}/{repo}/branches/{default_branch}").json()
    tree_sha = branch_meta["commit"]["commit"]["tree"]["sha"]
    tree = _get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1").json()
    if tree.get("truncated"):
        print(f"[github] WARNING: tree for {owner}/{repo} truncated; consider narrower seed")

    for entry in tree.get("tree", []):
        if entry["type"] != "blob":
            continue
        path = entry["path"]
        if root_path and not path.startswith(root_path.rstrip("/") + "/") and path != root_path:
            continue
        if not fnmatch.fnmatch(path, glob):
            continue
        ff = _fetch_one(owner, repo, path, source_kind)
        if ff:
            yield ff


def _search_repos(query: str, max_results: int = 30) -> list[dict]:
    """Call /search/repositories. Returns up to max_results raw repo items.

    Use sort/order/pushed filters in the query string to control ranking,
    e.g. "topic:agents stars:>10 pushed:>2026-04-01 sort:updated".

    Skips silently on 401 (bad/missing token), 403 (rate limit), 422 (bad query).
    Search has its own budget — 30 req/min authenticated.
    """
    per_page = min(100, max_results)
    url = (
        f"{GITHUB_API}/search/repositories"
        f"?q={urllib.parse.quote(query)}&per_page={per_page}"
    )
    try:
        r = _get(url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403, 422):
            print(f"[github-repo-search] {status} on '{query}' — skipping")
            return []
        raise
    return r.json().get("items", [])[:max_results]


def _code_search(query: str, source_kind: str, max_results: int) -> Iterator[FetchedFile]:
    """GitHub Code Search → file fetches.

    Search has its own rate budget (10/min anon, 30/min authed) and a hard
    cap of 1000 results across pagination. We bail early on 403 (rate limit
    or insufficient scope) so the rest of the crawl can continue.
    """
    per_page = 30
    fetched = 0
    page = 1
    while fetched < max_results:
        url = (
            f"{GITHUB_API}/search/code"
            f"?q={urllib.parse.quote(query)}&per_page={per_page}&page={page}"
        )
        try:
            r = _get(url)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # 401 = no/bad token (Code Search requires auth), 403 = rate-limited,
            # 422 = bad query. All non-fatal: skip this seed, continue the crawl.
            if status in (401, 403, 422):
                print(f"[github-search] {status} on '{query}' page {page} — skipping search seed")
                return
            raise
        items = r.json().get("items", [])
        if not items:
            return
        for item in items:
            if fetched >= max_results:
                return
            owner = item["repository"]["owner"]["login"]
            name = item["repository"]["name"]
            path = item["path"]
            ff = _fetch_one(owner, name, path, source_kind)
            if ff:
                fetched += 1
                yield ff
        if len(items) < per_page:
            return
        page += 1


class GitHubAdapter(Adapter):
    def __init__(self, seed_entry: dict):
        self.entry = seed_entry

    def discover(self) -> Iterator[FetchedFile]:
        e = self.entry
        mode = e["mode"]
        kind = e["source_kind"]
        if mode == "file":
            ff = _fetch_one(e["owner"], e["repo"], e["path"], kind)
            if ff:
                yield ff
        elif mode == "file_list":
            for t in e["targets"]:
                ff = _fetch_one(t["owner"], t["repo"], t["path"], kind)
                if ff:
                    yield ff
        elif mode == "tree":
            yield from _walk_tree(e["owner"], e["repo"], e.get("path", ""), e["glob"], kind)
        elif mode == "code_search":
            yield from _code_search(e["query"], kind, int(e.get("max_results", 30)))
        else:
            raise ValueError(f"unknown adapter mode: {mode}")
