"""Claude Code marketplace adapter.

A "marketplace" is a Git repo with a `.claude-plugin/marketplace.json` file
listing plugins. Each plugin contributes instruction-bearing files
(commands/, agents/, skills/) that we treat as rules. Single-plugin repos
(no marketplace.json, just a `.claude-plugin/plugin.json`) are also supported.

Discovery is search-driven — no fixed marketplace list. Three modes, mirroring
the trending-rotation philosophy in `crawl.py`:

    - "marketplace_code_search":  /search/code on path:.claude-plugin/marketplace.json
    - "plugin_code_search":       /search/code on path:.claude-plugin/plugin.json
    - "marketplace_topic_search": /search/repositories on topic:claude-code-plugin etc.

Per-plugin trending: within a discovered marketplace, plugins are walked in
order of their own most-recent commit (most-recent first). A per-plugin
24h-fetched skip means already-fresh plugins drop out of the next pass.
"""

from __future__ import annotations

import fnmatch
import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator

import httpx

from .base import FetchedFile
from .github import GITHUB_API, _code_search, _fetch_one, _get, _search_repos


# Subdirectory → (file glob, source_kind) for plugin walks. fnmatch globs.
_PLUGIN_FILE_LAYOUT: list[tuple[str, str, str]] = [
    ("commands", "*.md",     "claude_plugin_command"),
    ("agents",   "*.md",     "claude_plugin_agent"),
    ("skills",   "*SKILL.md", "claude_plugin_skill"),
    ("hooks",    "hooks.json", "claude_plugin_hook_config"),
]

# How long to skip a plugin we already fetched, mirroring SKIP_RECENT_HOURS in
# crawl.py. Kept local so the adapter doesn't import from crawl (avoids cycle).
_PLUGIN_SKIP_HOURS = 24


# ---------------------------------------------------------------------------
# Marketplace manifest parsing
# ---------------------------------------------------------------------------

def _parse_manifest(content: str) -> list[dict]:
    """Tolerant parser for marketplace.json. Returns a list of plugin specs:

        {"name": <str>, "source_kind": "inline"|"external",
         "path": <repo-relative path>,           # for inline
         "owner": <owner>, "repo": <repo>,       # for external
         "subpath": <str|None>}                  # for external w/ subdir

    Handles three real-world shapes seen across early marketplaces:
        - plugins: ["./plugins/foo", ...]                       (bare strings)
        - plugins: [{name, source: "./plugins/foo"}, ...]       (inline string source)
        - plugins: [{name, source: "github.com/x/y"}, ...]      (external string)
        - plugins: [{name, source: {type, repo, path}}, ...]    (external structured)
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    raw_plugins = data.get("plugins") or []
    out: list[dict] = []
    for entry in raw_plugins:
        spec = _normalise_plugin_entry(entry)
        if spec:
            out.append(spec)
    return out


_GH_URL_RE = re.compile(
    r"^(?:https?://)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?(?:/(?P<sub>.+))?$"
)


def _normalise_plugin_entry(entry) -> dict | None:
    if isinstance(entry, str):
        return _resolve_source_string(name=None, source=entry)
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    src = entry.get("source") or entry.get("path") or entry.get("repository")
    if isinstance(src, str):
        spec = _resolve_source_string(name=name, source=src)
        return spec
    if isinstance(src, dict):
        # Structured external source: {type/source: "github", repo: "owner/repo", path: "subdir"}
        repo_field = src.get("repo") or src.get("repository") or ""
        m = re.match(r"^(?P<owner>[^/]+)/(?P<repo>[^/]+)$", repo_field)
        if m:
            return {
                "name": name or m.group("repo"),
                "source_kind": "external",
                "owner": m.group("owner"),
                "repo": m.group("repo"),
                "subpath": (src.get("path") or "").strip("/") or None,
            }
    return None


def _resolve_source_string(*, name: str | None, source: str) -> dict | None:
    """A source string is either an inline repo-relative path (./foo or foo/bar)
    or an external GitHub URL."""
    s = source.strip()
    if not s:
        return None
    m = _GH_URL_RE.match(s)
    if m:
        return {
            "name": name or m.group("repo"),
            "source_kind": "external",
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "subpath": (m.group("sub") or "").strip("/") or None,
        }
    # Treat anything else as a repo-relative path.
    inline_path = s.lstrip("./").rstrip("/")
    return {
        "name": name or inline_path.rsplit("/", 1)[-1],
        "source_kind": "inline",
        "path": inline_path,
    }


# ---------------------------------------------------------------------------
# Per-plugin recency: trending-first ordering + 24h skip
# ---------------------------------------------------------------------------

def _plugin_pushed_at(owner: str, repo: str, path: str | None) -> int:
    """Epoch seconds of the most recent commit touching `path` (or the repo
    root if path is None/empty). One /repos/.../commits call.

    Used to sort plugins within a marketplace before walking, so the freshest
    plugins surface first if rate-limit pressure cuts the pass short. Returns
    0 on any error so the plugin sorts last but still gets a chance.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits?per_page=1"
    if path:
        url += f"&path={urllib.parse.quote(path)}"
    try:
        items = _get(url).json()
    except (httpx.HTTPStatusError, httpx.RequestError):
        return 0
    if not items:
        return 0
    iso = items[0].get("commit", {}).get("committer", {}).get("date", "")
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return 0


def _plugin_recently_fetched(c, project_canonical_url: str, plugin_root: str | None) -> bool:
    """True if any rules_file under this plugin was fetched within
    _PLUGIN_SKIP_HOURS. Plugin granularity, regardless of marketplace.
    `plugin_root` is the path prefix inside the project; None means "the
    whole project counts as the plugin" (external-source case).
    """
    if plugin_root:
        path_filter = "AND rf.path LIKE ? || '/%'"
        params = [project_canonical_url, plugin_root]
    else:
        path_filter = ""
        params = [project_canonical_url]
    row = c.execute(
        f"""
        SELECT max(rf.fetched_at)
          FROM rules_file rf
          JOIN source_project sp ON sp.id = rf.project_id
         WHERE sp.canonical_url = ?
           {path_filter}
        """,
        params,
    ).fetchone()
    if not row or not row[0]:
        return False
    age_h = (datetime.now(timezone.utc) - row[0]).total_seconds() / 3600
    return age_h < _PLUGIN_SKIP_HOURS


# ---------------------------------------------------------------------------
# Repo tree walking — one tree fetch per plugin
# ---------------------------------------------------------------------------

def _list_blobs(owner: str, repo: str) -> list[dict]:
    """Recursive tree at the default branch. One Git Trees API call.
    Returns the raw blob entries; caller filters by path prefix and glob.
    """
    try:
        meta = _get(f"{GITHUB_API}/repos/{owner}/{repo}").json()
        default_branch = meta["default_branch"]
        branch = _get(f"{GITHUB_API}/repos/{owner}/{repo}/branches/{default_branch}").json()
        tree_sha = branch["commit"]["commit"]["tree"]["sha"]
        tree = _get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1").json()
    except (httpx.HTTPStatusError, httpx.RequestError, KeyError):
        return []
    if tree.get("truncated"):
        print(f"[claude_marketplace] WARNING: tree for {owner}/{repo} truncated")
    return [e for e in tree.get("tree", []) if e.get("type") == "blob"]


def _walk_plugin_files(
    owner: str,
    repo: str,
    plugin_root: str | None,
    canonical_url: str,
) -> Iterator[FetchedFile]:
    """Walk one plugin's commands/, agents/, skills/, hooks/. Single tree
    fetch + one contents-API fetch per matched file. Sets
    project_canonical_url on each FetchedFile so cross-marketplace dedup
    works (external plugins share a canonical_url regardless of which
    marketplace pointed at them).
    """
    blobs = _list_blobs(owner, repo)
    if not blobs:
        return
    prefix = (plugin_root.rstrip("/") + "/") if plugin_root else ""
    for subdir, glob, source_kind in _PLUGIN_FILE_LAYOUT:
        wanted_prefix = f"{prefix}{subdir}/"
        for entry in blobs:
            path = entry["path"]
            if not path.startswith(wanted_prefix):
                continue
            # fnmatch on the basename — globs in _PLUGIN_FILE_LAYOUT match the
            # filename only, e.g. *SKILL.md catches skills/<name>/SKILL.md.
            if not fnmatch.fnmatch(path.rsplit("/", 1)[-1], glob):
                continue
            ff = _fetch_one(owner, repo, path, source_kind)
            if ff is None:
                continue
            # Rewrite canonical_url so external plugins listed in multiple
            # marketplaces still resolve to one source_project.
            ff.project_canonical_url = canonical_url
            yield ff


# ---------------------------------------------------------------------------
# Top-level discover() — yields all FetchedFiles for one marketplace
# ---------------------------------------------------------------------------

def discover_marketplace(c, owner: str, repo: str) -> Iterator[FetchedFile]:
    """Discover and walk one marketplace. Yields:
      1. the marketplace manifest itself (kind='claude_marketplace_manifest')
      2. each plugin's manifest where present (kind='claude_plugin_manifest')
      3. each plugin's command/agent/skill/hook files

    `c` is a DuckDB connection used for the per-plugin recency skip. None is
    accepted (skip is disabled), but production callers should pass one.
    """
    manifest_ff = _fetch_one(owner, repo, ".claude-plugin/marketplace.json",
                             "claude_marketplace_manifest")
    if manifest_ff is None:
        return
    yield manifest_ff

    plugin_specs = _parse_manifest(manifest_ff.raw_content)
    # Trending-first ordering inside the marketplace. One extra commits-API
    # call per plugin; cheap, and means the freshest plugins land first if a
    # pass gets rate-limited mid-walk.
    enriched: list[tuple[int, dict]] = []
    for spec in plugin_specs:
        if spec["source_kind"] == "inline":
            ts = _plugin_pushed_at(owner, repo, spec["path"])
        else:
            ts = _plugin_pushed_at(spec["owner"], spec["repo"], spec.get("subpath"))
        enriched.append((ts, spec))
    enriched.sort(key=lambda x: -x[0])

    for _, spec in enriched:
        yield from _walk_one_plugin(c, marketplace_owner=owner,
                                    marketplace_repo=repo, spec=spec)


def discover_solo_plugin(c, owner: str, repo: str) -> Iterator[FetchedFile]:
    """For single-plugin repos (no marketplace.json, just plugin.json at
    .claude-plugin/plugin.json or repo root). The whole repo IS the plugin."""
    canonical = f"https://github.com/{owner}/{repo}"
    if c is not None and _plugin_recently_fetched(c, canonical, plugin_root=None):
        return
    manifest_ff = _fetch_one(owner, repo, ".claude-plugin/plugin.json",
                             "claude_plugin_manifest")
    if manifest_ff is not None:
        yield manifest_ff
    yield from _walk_plugin_files(owner, repo, plugin_root=None,
                                  canonical_url=canonical)


def _walk_one_plugin(c, marketplace_owner: str, marketplace_repo: str,
                     spec: dict) -> Iterator[FetchedFile]:
    if spec["source_kind"] == "inline":
        plugin_root = spec["path"]
        canonical = f"https://github.com/{marketplace_owner}/{marketplace_repo}"
        if c is not None and _plugin_recently_fetched(c, canonical, plugin_root):
            return
        # Optional plugin.json under the inline path
        plug_manifest = _fetch_one(marketplace_owner, marketplace_repo,
                                   f"{plugin_root}/.claude-plugin/plugin.json",
                                   "claude_plugin_manifest")
        if plug_manifest is not None:
            plug_manifest.project_canonical_url = canonical
            yield plug_manifest
        yield from _walk_plugin_files(marketplace_owner, marketplace_repo,
                                      plugin_root, canonical_url=canonical)
        return

    # external
    ext_owner = spec["owner"]
    ext_repo = spec["repo"]
    subpath = spec.get("subpath")
    canonical = f"https://github.com/{ext_owner}/{ext_repo}"
    if c is not None and _plugin_recently_fetched(c, canonical, subpath):
        return
    # plugin.json may live at <subpath>/.claude-plugin/plugin.json or repo root
    manifest_path = (f"{subpath}/" if subpath else "") + ".claude-plugin/plugin.json"
    plug_manifest = _fetch_one(ext_owner, ext_repo, manifest_path, "claude_plugin_manifest")
    if plug_manifest is not None:
        plug_manifest.project_canonical_url = canonical
        yield plug_manifest
    yield from _walk_plugin_files(ext_owner, ext_repo, subpath, canonical_url=canonical)


# ---------------------------------------------------------------------------
# Trending discovery — three search modes, all rotation-based
# ---------------------------------------------------------------------------

def discover_via_code_search(c, query: str, max_results: int) -> Iterator[FetchedFile]:
    """GitHub Code Search for marketplace.json files. Each hit is the manifest
    file in some repo; we extract (owner, repo) tuples and walk each one.
    Dedupes within a single call so the same marketplace isn't walked twice
    if Code Search returns multiple paths from it."""
    seen: set[tuple[str, str]] = set()
    for ff in _code_search(query, "claude_marketplace_manifest", max_results):
        key = (ff.project_owner, ff.project_name)
        if key in seen:
            continue
        seen.add(key)
        yield from discover_marketplace(c, ff.project_owner, ff.project_name)


def discover_solo_plugins_via_code_search(c, query: str,
                                          max_results: int) -> Iterator[FetchedFile]:
    """GitHub Code Search for plugin.json files. Walks each as a solo plugin."""
    seen: set[tuple[str, str]] = set()
    for ff in _code_search(query, "claude_plugin_manifest", max_results):
        key = (ff.project_owner, ff.project_name)
        if key in seen:
            continue
        seen.add(key)
        yield from discover_solo_plugin(c, ff.project_owner, ff.project_name)


def discover_via_topic_search(c, query: str, max_results: int) -> Iterator[FetchedFile]:
    """GitHub /search/repositories with a topic filter. For each repo, probe
    both .claude-plugin/marketplace.json (marketplace) and .claude-plugin/
    plugin.json (solo plugin); whichever exists drives the walk."""
    for item in _search_repos(query, max_results=max_results):
        owner = item["owner"]["login"]
        repo = item["name"]
        # Try marketplace first (a marketplace yields more rules per pass).
        m_ff = _fetch_one(owner, repo, ".claude-plugin/marketplace.json",
                          "claude_marketplace_manifest")
        if m_ff is not None:
            yield m_ff
            for spec in _parse_manifest(m_ff.raw_content):
                yield from _walk_one_plugin(c, owner, repo, spec)
            continue
        # Otherwise treat as a solo plugin.
        yield from discover_solo_plugin(c, owner, repo)
