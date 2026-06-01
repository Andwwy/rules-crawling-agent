"""Streamlit admin UI (DuckDB + threaded crawler edition).

The crawler runs as a Python thread inside this same Streamlit process, not
as a subprocess: DuckDB doesn't support multi-process writers to one file,
and the thread model lets us share an in-memory heartbeat + threading.Event
stop signal cleanly. `st.cache_resource` keeps the CrawlState singleton
across Streamlit reruns and across browser tab opens.

Watchdog model: every Streamlit rerun bumps `state.heartbeat = time.time()`.
The auto-refresh ticks every 5 s while a crawl is running, so closing the
laptop / browser tab freezes the timestamp; the crawler thread sees the
staleness between files and exits with status='partial'.
"""

import difflib
import html
import threading
import time
from datetime import datetime

import pandas as pd
import pytz
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# Display timezone for all human-facing timestamps. DuckDB stores TIMESTAMPTZ
# in UTC; we convert on the way out. `America/New_York` handles DST automatically
# (EST in winter, EDT in summer).
_DISPLAY_TZ = pytz.timezone("America/New_York")


def _fmt_local(dt: datetime | None, fmt: str = "%H:%M:%S %Z") -> str:
    """Convert a UTC datetime from DuckDB to Eastern Time for display."""
    if dt is None:
        return ""
    return dt.astimezone(_DISPLAY_TZ).strftime(fmt)

from src import api_smoke
from src.adapters.github import get_rate_state, is_paused
from src.config import EXTRACTOR_MODEL, JUDGE_MODEL
from src.cost import AGENT_MODELS, MODEL_BY_ID, PRICING_AS_OF, model_label
from src.crawl import (
    _CLEAR_DELETE_ORDER,
    CrawlState,
    clear_extracted_data,
    continuous_loop,
    infer_manual_kind,
    ingest_links,
    ingest_manual_files,
)
from src.db import conn, enum_values, get_setting, set_setting
from src.guidance_store import (
    current_version_no,
    ensure_seeded,
    list_versions,
    read_guidance,
    save_guidance,
)
from src.revise import gather_feedback, propose_revision


st.set_page_config(page_title="Rules-in-the-Wild — Prototype", layout="wide")

# ---------------------------------------------------------------------------
# Styling — restrained design-engineering touches (Emil Kowalski's principles,
# applied within Streamlit's constraints). The goal is invisible correctness:
#   • buttons acknowledge a press (scale 0.97 on :active) with a strong ease-out
#     curve and a sub-300ms duration — instant feedback, no sluggishness;
#   • all motion is gated behind prefers-reduced-motion so it never fights
#     someone who's asked the OS for stillness;
#   • metric numbers use tabular figures so they don't jitter as they tick;
#   • a calm section-lead style for one-line explanations under headers.
# No keyframes, no scale-from-zero, no animating layout — only transform/opacity.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @media (prefers-reduced-motion: no-preference) {
      .stButton button, .stDownloadButton button, .stFormSubmitButton button {
        transition: transform 160ms cubic-bezier(0.23, 1, 0.32, 1);
      }
      .stButton button:active, .stDownloadButton button:active,
      .stFormSubmitButton button:active {
        transform: scale(0.97);
      }
    }
    [data-testid="stMetricValue"], [data-testid="stMetric"] { font-variant-numeric: tabular-nums; }
    .section-lead { color: #6b7280; font-size: 0.9rem; line-height: 1.45;
                    margin: -0.35rem 0 0.9rem; max-width: 60rem; }
    .danger-head { color: #b42318; font-weight: 600; letter-spacing: 0.01em; }
    /* Cursor-style line diff for proposed guidance revisions. Green = added,
       red = removed; long unchanged runs collapse to a quiet marker. */
    .gdiff { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: 0.8rem; line-height: 1.5; border: 1px solid #e5e7eb;
             border-radius: 8px; overflow: auto; max-height: 34rem; padding: 0.4rem 0; }
    .gdiff .ln { padding: 0 0.6rem; white-space: pre-wrap; word-break: break-word; }
    .gdiff .add { background: #e6ffec; }
    .gdiff .del { background: #ffebe9; }
    .gdiff .eq { color: #1f2328; }
    .gdiff .mk { user-select: none; opacity: 0.45; }
    .gdiff .skip { color: #6b7280; font-style: italic; padding: 0.15rem 0.6rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Shared crawl state (one instance for the whole Streamlit process)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_crawl_state() -> CrawlState:
    return CrawlState(
        heartbeat=time.time(),
        stop_event=threading.Event(),
        run_id=None,
        status="idle",
    )


@st.cache_resource
def get_thread_handle() -> dict:
    """Holds the running thread so we can poll is_alive() across reruns."""
    return {"thread": None}


def _start_continuous(state: CrawlState, handle: dict) -> None:
    """Persist `continuous_mode = on` and spawn the loop thread. Idempotent:
    if a thread is already alive we just refresh the setting."""
    with conn() as c:
        set_setting(c, "continuous_mode", "on")
    if handle.get("thread") and handle["thread"].is_alive():
        return
    state.stop_event = threading.Event()
    state.beat()
    state.status = "running"
    state.error = None
    state.last_message = "starting continuous mode"

    t = threading.Thread(
        target=continuous_loop, args=(state,),
        daemon=True, name="crawler-continuous",
    )
    t.start()
    handle["thread"] = t


def _stop_continuous(state: CrawlState) -> None:
    """Persist `continuous_mode = off` and signal the loop to exit. The thread
    will finish its current pass-iteration check and exit cleanly."""
    with conn() as c:
        set_setting(c, "continuous_mode", "off")
    state.stop_event.set()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_crawl, tab_upload, tab_revise, tab_maint = st.tabs(
    ["Crawl", "Upload", "Revise", "Maintenance"]
)

state = get_crawl_state()
handle = get_thread_handle()
is_running = handle["thread"] is not None and handle["thread"].is_alive()

# Auto-start on boot: if the persisted setting says continuous mode was on,
# and no thread is alive in this process yet, spawn it. This is what makes
# the crawl pick up where it left off across `docker compose restart admin`,
# Streamlit reloads, Ctrl-C, container OOM, etc.
if not is_running:
    with conn() as _c:
        _continuous_persisted = get_setting(_c, "continuous_mode", "off") == "on"
    if _continuous_persisted:
        _start_continuous(state, handle)
        is_running = True


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------
with tab_crawl:
    st.header("Crawl the open internet")
    from src.crawl import PASS_INTERVAL_SECONDS, SKIP_RECENT_HOURS
    _secs = int(PASS_INTERVAL_SECONDS)
    _interval = f"{_secs} s" if _secs < 60 else f"{_secs // 60} min"
    st.caption(
        "Discovers trending agent projects across public GitHub via the search API — "
        "`/search/repositories` on topics like `agents`, `agentic-ai`, `cursor-rules`, "
        "`claude-code`, plus direct `path:AGENTS.md` / `CLAUDE.md` / `.cursorrules` "
        f"file searches. Re-runs every {_interval}; repos crawled in the past "
        f"{SKIP_RECENT_HOURS} h are skipped so each pass focuses on new content. "
        "Closing this tab does not stop the crawl; closing or sleeping the laptop "
        "suspends the process and it picks up where it left off on wake. The "
        "continuous-mode toggle is persisted in DuckDB, so it also resumes across "
        "admin restarts."
    )

    # ---- Models ----
    # Separate extractor / judge model pickers (Perplexity Agent API passthrough).
    # Persisted to crawl_settings; the crawl loop reads them per pass. Locked
    # while running — set them before Start.
    with conn() as _sc:
        _ex_model = get_setting(_sc, "extractor_model", EXTRACTOR_MODEL) or EXTRACTOR_MODEL
        _jd_model = get_setting(_sc, "judge_model", JUDGE_MODEL) or JUDGE_MODEL
        _cap_raw = (get_setting(_sc, "spend_cap_usd", "") or "").strip()
        _spend_mirror = (get_setting(_sc, "session_spend_usd", "") or "").strip()
    try:
        _cap_val = float(_cap_raw) if _cap_raw else 0.0
    except ValueError:
        _cap_val = 0.0

    _ids = [m.id for m in AGENT_MODELS]

    def _model_fmt(mid: str) -> str:
        m = MODEL_BY_ID.get(mid)
        return f"{m.label}  ·  ${m.input_per_mtok:g}/${m.output_per_mtok:g} per Mtok" if m else mid

    with st.expander("Models", expanded=not is_running):
        cc1, cc2 = st.columns(2)
        with cc1:
            sel_ex = st.selectbox(
                "Extractor model", _ids,
                index=_ids.index(_ex_model) if _ex_model in _ids else 0,
                format_func=_model_fmt, disabled=is_running,
                key="sel_extractor_model",
                help="Model for the always-on per-file rule extractor.",
            )
        with cc2:
            sel_jd = st.selectbox(
                "Judge model", _ids,
                index=_ids.index(_jd_model) if _jd_model in _ids else 0,
                format_func=_model_fmt, disabled=is_running,
                key="sel_judge_model",
                help="Model for the 4-axis taxonomy judge (one call per rule).",
            )
        # Persist model edits (only when stopped — inputs lock while running).
        if not is_running:
            with conn() as _wc:
                if sel_ex != _ex_model:
                    set_setting(_wc, "extractor_model", sel_ex)
                if sel_jd != _jd_model:
                    set_setting(_wc, "judge_model", sel_jd)

    # ---- Spending ----
    # Judge-agent-style budget meter: an always-visible Spent / Cap / % panel so
    # the live token cost stays in view during and after a run. Session-scoped —
    # state.spend_usd accumulates from worker Start and resets each session
    # (mirrored to crawl_settings.session_spend_usd so the last total is still
    # readable after a process restart). The cap persists across sessions in
    # crawl_settings.spend_cap_usd and HARD-STOPS the crawl when reached.
    st.divider()
    st.subheader("Spending")

    _spent = float(state.spend_usd or 0.0)
    if _spent == 0.0 and not is_running and _spend_mirror:
        # Fresh process (or post-reset): fall back to the persisted mirror so the
        # last session's total still shows until the next Start zeroes it.
        try:
            _spent = float(_spend_mirror)
        except ValueError:
            pass
    _pct = (_spent / _cap_val * 100.0) if _cap_val > 0 else 0.0

    sp1, sp2, sp3 = st.columns([2, 1, 1])
    sp1.metric("Spent (session)", f"${_spent:.4f}",
               help="Extractor + judge token cost since the worker started this session.")
    sp2.metric("Cap", f"${_cap_val:.2f}" if _cap_val > 0 else "—")
    sp3.metric("% of cap", f"{_pct:.1f}%" if _cap_val > 0 else "—",
               delta=None if _pct < 80 else f"{_pct - 80:.0f}% past warn")
    st.progress(min(1.0, _pct / 100.0) if _cap_val > 0 else 0.0)

    sc1, sc2 = st.columns([3, 1])
    sel_cap = sc1.number_input(
        "Spending cap — USD this session (0 = no cap)",
        min_value=0.0, value=_cap_val, step=0.50, format="%.2f",
        disabled=is_running, key="sel_spend_cap",
        help=("The crawl HARD-STOPS when session spend reaches this. Locked while "
              f"running — set it before Start. Prices per {PRICING_AS_OF}; "
              "estimate only."),
    )
    if sc2.button("Reset spend", use_container_width=True, disabled=is_running,
                  help="Clear the session spend counter now (it also auto-resets each Start)."):
        state.reset_spend()
        with conn() as _rc:
            set_setting(_rc, "session_spend_usd", "0")
        st.rerun()
    # Persist cap edits (only when stopped — input is locked while running).
    if not is_running:
        with conn() as _wc:
            _cap_store = "" if sel_cap <= 0 else f"{sel_cap:.2f}"
            if _cap_store != _cap_raw:
                set_setting(_wc, "spend_cap_usd", _cap_store)

    # Per-model breakdown + hard-stop banner. Copy the dict — the worker thread
    # mutates spend_by_model while this UI thread reads it.
    _by_model = dict(state.spend_by_model)
    if _by_model:
        _parts = " · ".join(f"{model_label(k)} ${v:.4f}" for k, v in _by_model.items())
        st.caption(f"By model — {_parts}")
    if state.cap_tripped:
        _cap_disp = state.cap_usd or _cap_val or 0
        if is_running:
            # Cap latched but the worker thread is still alive — it's finishing
            # the file in flight before halting (no new files fetched).
            st.warning(
                f"⛔ Spending cap of ${_cap_disp:.2f} reached at ${_spent:.4f} — "
                "finishing the current file, then stopping. No new files will be "
                "fetched."
            )
        else:
            st.error(
                f"⛔ Spending cap of ${_cap_disp:.2f} reached — crawl hard-stopped "
                f"at ${_spent:.4f}. Raise the cap and click Start to resume."
            )

    if is_running:
        st.button("Stop", on_click=_stop_continuous, args=(state,), use_container_width=True)
        st.info(state.last_message or "Continuous crawl running.")
        # Bump heartbeat each rerun (used only for in-UI 'last seen' display
        # now — no longer aborts the worker). Auto-refresh so the panel below
        # ticks while a pass is mid-flight.
        state.beat()
        st_autorefresh(interval=5000, key="crawl_refresh")
    else:
        if st.button("Start continuous crawl", type="primary", use_container_width=True):
            with st.spinner("Pre-flight: testing embedder, extractor, judge, GitHub…"):
                result = api_smoke.run(extractor_model=sel_ex, judge_model=sel_jd)
            if not result.ok:
                st.error("Pre-flight failed — not starting crawl.")
                for e in result.errors:
                    st.error(f"  • {e}")
            else:
                judge_part = f"judge {result.judge_ms:.0f} ms · " if result.judge_ms is not None else "judge disabled · "
                st.success(
                    f"Pre-flight OK · embedder {result.embedder_ms:.0f} ms · "
                    f"extractor {result.extractor_ms:.0f} ms · "
                    f"{judge_part}"
                    f"GitHub {result.github_ms:.0f} ms ({result.github_remaining} req left)"
                )
                for w in result.warnings:
                    st.warning(f"  • {w}")
                _start_continuous(state, handle)
                st.rerun()

    # Cumulative dashboard — corpus totals + worker status.
    st.divider()
    st.subheader("Corpus")

    with conn() as c:
        files, rules = c.execute(
            """
            SELECT
                (SELECT count(*) FROM rules_file),
                (SELECT count(*) FROM rule)
            """
        ).fetchone()
        last = c.execute(
            """
            SELECT id, COALESCE(finished_at, started_at), error_message
              FROM ingestion_run
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
    last_run_id = last[0] if last else None
    last_activity = last[1] if last else None
    last_pass_error = last[2] if last else None

    def _format_ago(when):
        if when is None:
            return "—"
        delta = (datetime.now(_DISPLAY_TZ) - when.astimezone(_DISPLAY_TZ)).total_seconds()
        if delta < 5:
            return "just now"
        if delta < 90:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"

    # Status:
    #   - 🟡 stopping         : spend cap latched; the worker is finishing the
    #                           current file, then halts (no new files fetched)
    #   - 🟢 running          : thread alive and actively making API calls
    #   - 🟡 waiting limit    : thread alive but blocked in _maybe_pause_for_rate
    #   - ⚪ stopped          : thread not alive
    paused_now, paused_bucket, paused_resets_at = is_paused()
    if is_running and state.cap_tripped:
        status_metric = "🟡 stopping"
        status_help = "spend cap reached — finishing the current file, then halting"
    elif is_running and paused_now:
        wait_left = max(0, paused_resets_at - int(time.time()))
        wait_str = f"{wait_left // 60}m {wait_left % 60}s" if wait_left >= 60 else f"{wait_left}s"
        status_metric = "🟡 waiting limit"
        status_help = f"{paused_bucket} bucket exhausted; resumes in {wait_str}"
    elif is_running:
        status_metric = "🟢 running"
        status_help = None
    else:
        status_metric = "⚪ stopped"
        status_help = None

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Status", status_metric, help=status_help)
    m2.metric("Files", int(files or 0))
    m3.metric("Rules", int(rules or 0))
    m4.metric("Last activity", _format_ago(last_activity))

    # Below the metrics: a small caption with what the worker is doing right
    # now (when running) OR what the most recent pass produced (when stopped).
    # The spend meter + per-model breakdown + hard-stop banner live in the
    # always-visible "Spending" section above.
    if is_running and state.last_message:
        st.caption(f"Working on: `{state.last_message}`")
    if last_pass_error and not is_running:
        st.caption(f"Note from last pass: {last_pass_error}")

    # ---- GitHub API budget ----
    # Updated after every GitHub request via response headers. When a bucket
    # hits zero, the crawler pauses until reset_at; we render a yellow banner
    # with the countdown so the user knows what's happening.
    rate = get_rate_state()
    if rate:
        # Show core + search side-by-side. Order them deterministically.
        ordered = sorted(rate.items(), key=lambda kv: kv[0])
        cols = st.columns(len(ordered))
        now_ts = int(time.time())
        any_exhausted = False
        for col, (bucket, entry) in zip(cols, ordered):
            remaining = entry["remaining"]
            limit = entry["limit"]
            reset_in = max(0, entry["reset_at"] - now_ts)
            if reset_in >= 60:
                reset_str = f"{reset_in // 60}m {reset_in % 60}s"
            else:
                reset_str = f"{reset_in}s"
            if remaining == 0 and reset_in > 0:
                any_exhausted = True
            col.metric(
                f"GitHub {bucket}",
                f"{remaining}/{limit}",
                help=f"Resets in {reset_str}",
            )
            col.caption(f"Resets in {reset_str}")
        if paused_now:
            wait_left = max(0, paused_resets_at - int(time.time()))
            wait_str = f"{wait_left // 60}m {wait_left % 60}s" if wait_left >= 60 else f"{wait_left}s"
            st.warning(
                f"🟡 Waiting on GitHub limit reset — `{paused_bucket}` bucket "
                f"exhausted, resumes in {wait_str}. Stop is responsive during the pause."
            )
        elif any_exhausted:
            st.caption(
                "A GitHub bucket is at zero but no thread is currently paused — "
                "the next API call will trigger a pause."
            )

    # Recent events (still scoped to the most recent run row for diagnostics).
    if last_run_id:
        with conn() as c:
            events = c.execute(
                """
                SELECT event_at, level, message
                  FROM ingestion_event
                 WHERE run_id = ?
                 ORDER BY event_at DESC
                 LIMIT 15
                """,
                [last_run_id],
            ).fetchall()
        if events:
            with st.expander("Recent events (latest pass)", expanded=False):
                for event_at, level, message in events:
                    st.text(f"{_fmt_local(event_at)}  [{level}]  {message}")


# ---------------------------------------------------------------------------
# Upload — drag-and-drop queue for manually-picked source files
# ---------------------------------------------------------------------------
_UPLOAD_BADGE = {
    "ingested": "🟢", "empty": "⚪", "manifest": "📦",
    "duplicate": "🟡", "failed": "🔴",
}

with tab_upload:
    st.header("Upload sources")
    st.markdown(
        '<p class="section-lead">Feed agent-rule sources through the same extractor '
        'the crawler uses — by dropping files or by pasting links. Re-ingesting '
        'identical content is skipped; an edited re-upload is ingested fresh.</p>',
        unsafe_allow_html=True,
    )

    with conn() as _uc:
        _kind_options = enum_values(_uc, "rules_file_kind")

    # --- From files -----------------------------------------------------------
    st.subheader("From files")
    st.markdown(
        '<p class="section-lead">The kind is inferred from each filename — override '
        'it before processing if the guess is wrong.</p>',
        unsafe_allow_html=True,
    )

    uploads = st.file_uploader(
        "Drop files here",
        accept_multiple_files=True,
        key="manual_uploads",
        help="CLAUDE.md · AGENTS.md · .cursorrules · .mdc · SKILL.md · llms.txt — any text source.",
    )

    if uploads:
        st.caption(f"{len(uploads)} file(s) queued — confirm each kind, then process.")
        chosen_kinds: dict[str, str] = {}
        for up in uploads:
            inferred = infer_manual_kind(up.name)
            kc1, kc2 = st.columns([3, 2])
            kc1.markdown(f"`{up.name}`  ·  {up.size:,} B")
            _idx = _kind_options.index(inferred) if inferred in _kind_options else (
                _kind_options.index("other") if "other" in _kind_options else 0)
            chosen_kinds[up.name] = kc2.selectbox(
                "kind", _kind_options, index=_idx,
                key=f"kind_{up.name}", label_visibility="collapsed",
            )
        if st.button("Process queue", type="primary", use_container_width=True,
                     key="process_uploads"):
            batch = []
            for up in uploads:
                text = up.getvalue().decode("utf-8", errors="replace")
                batch.append((up.name, text, chosen_kinds[up.name]))
            with st.spinner(f"Extracting {len(batch)} file(s)…"):
                st.session_state["last_upload_result"] = ingest_manual_files(batch)
            st.rerun()
    else:
        st.info("No files queued yet — drop one or more files above.")

    # --- From links -----------------------------------------------------------
    st.subheader("From links")
    st.markdown(
        '<p class="section-lead">Paste a GitHub repo or file URL, a Claude '
        'marketplace / plugin repo, or any raw text URL — one per line. A repo is '
        'probed for its canonical rule files (or walked as a marketplace); the kind '
        'is inferred per resolved file.</p>',
        unsafe_allow_html=True,
    )

    link_text = st.text_area(
        "Paste URLs — one per line",
        key="manual_links",
        height=120,
        placeholder=(
            "https://github.com/owner/repo\n"
            "https://github.com/owner/repo/blob/main/AGENTS.md\n"
            "https://github.com/owner/claude-marketplace\n"
            "https://example.com/llms.txt"
        ),
        help="GitHub repo/file, a Claude marketplace or plugin repo, or any raw text URL.",
    )
    _seen_links: set[str] = set()
    link_urls: list[str] = []
    for _ln in link_text.splitlines():
        _ln = _ln.strip()
        if _ln and _ln not in _seen_links:
            _seen_links.add(_ln)
            link_urls.append(_ln)
    if link_urls:
        st.caption(f"{len(link_urls)} link(s) ready.")
    if st.button("Fetch & process links", type="primary", use_container_width=True,
                 key="process_links", disabled=not link_urls):
        with st.spinner(f"Resolving + extracting {len(link_urls)} link(s)…"):
            st.session_state["last_upload_result"] = ingest_links(link_urls)
        st.rerun()

    # --- Shared result panel (last files-or-links batch) ----------------------
    _res = st.session_state.get("last_upload_result")
    if _res is not None:
        st.divider()
        st.subheader("Last batch")
        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("Sources", len(_res.items))
        rc2.metric("New rules", _res.total_rules)
        rc3.metric("Est. spend", f"${_res.spend_usd:.4f}")
        for it in _res.items:
            line = f"{_UPLOAD_BADGE.get(it.status, '•')} `{it.filename}` · **{it.status}**"
            if it.status == "ingested":
                line += f" · +{it.rules_inserted} rules"
            if it.error:
                line += f" · {it.error}"
            st.markdown(line)
        st.caption(
            "🟢 ingested · ⚪ no rules found · 📦 manifest (stored, not extracted) · "
            "🟡 duplicate (already ingested) · 🔴 failed"
        )


# ---------------------------------------------------------------------------
# Revise — edit the live guidance document, or fold labeler feedback into a
# model-proposed revision (reviewed as a diff, versioned on every save).
# ---------------------------------------------------------------------------

def _render_guidance_diff(old: str, new: str, context: int = 3) -> str:
    """Cursor-style green/red line diff as a self-contained HTML block.

    Added lines get a green background, removed lines red; runs of unchanged
    lines longer than 2*context collapse to a quiet "⋯ N unchanged ⋯" marker so
    a small edit in a long document stays readable. All text is HTML-escaped."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    def _row(cls: str, mark: str, text: str) -> str:
        return (f'<div class="ln {cls}"><span class="mk">{mark} </span>'
                f'{html.escape(text) if text else "&nbsp;"}</div>')

    rows: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            block = new_lines[j1:j2]
            if len(block) > 2 * context + 1:
                rows += [_row("eq", "&nbsp;", ln) for ln in block[:context]]
                rows.append(f'<div class="skip">⋯ {len(block) - 2 * context} '
                            f'unchanged ⋯</div>')
                rows += [_row("eq", "&nbsp;", ln) for ln in block[-context:]]
            else:
                rows += [_row("eq", "&nbsp;", ln) for ln in block]
        elif tag == "delete":
            rows += [_row("del", "−", ln) for ln in old_lines[i1:i2]]
        elif tag == "insert":
            rows += [_row("add", "+", ln) for ln in new_lines[j1:j2]]
        elif tag == "replace":
            rows += [_row("del", "−", ln) for ln in old_lines[i1:i2]]
            rows += [_row("add", "+", ln) for ln in new_lines[j1:j2]]
    if not rows:
        return '<div class="gdiff"><div class="skip">No changes.</div></div>'
    return f'<div class="gdiff">{"".join(rows)}</div>'


with tab_revise:
    st.header("Revise extraction guidance")
    st.markdown(
        '<p class="section-lead">The live extraction-guidance document. Edit it '
        'directly, or let the model fold in labeler feedback — rules humans added '
        'and comments on existing rules — into a proposed revision you '
        'review as a diff before it’s saved. Every save is versioned; older '
        'versions stay viewable and restorable below.</p>',
        unsafe_allow_html=True,
    )

    # Backfill version 1 from the seeded file on a fresh DB (idempotent, cheap).
    ensure_seeded()
    with conn() as _rc:
        _doc = read_guidance()
        _rev_model = get_setting(_rc, "extractor_model", EXTRACTOR_MODEL) or EXTRACTOR_MODEL
        _fb_counts = gather_feedback(_rc).counts
        _versions = list_versions(_rc)
        _cur_no = current_version_no(_rc)

    if not _doc.strip():
        st.warning(
            "No guidance document found at `guidance/guidance.md`. It seeds from "
            "`Design docs/extraction.md`; check the writable `./guidance` mount."
        )

    # ---- Live document editor ------------------------------------------------
    st.subheader("Current document")
    _cap = f"Version {_cur_no}" if _cur_no is not None else "Unversioned"
    st.caption(f"{_cap} · {len(_doc.splitlines())} lines · live file "
               "`guidance/guidance.md`")
    # The editor key embeds the version number so a save/restore (which bumps the
    # version) re-seeds the textarea with the new content instead of holding on
    # to stale widget state from the previous version.
    _edited = st.text_area(
        "Guidance document", value=_doc, height=420,
        key=f"guidance_editor_{_cur_no}", label_visibility="collapsed",
    )
    _dirty = _edited != _doc
    e1, e2, _ecol = st.columns([1, 1, 2])
    if e1.button("Save changes", type="primary", use_container_width=True,
                 disabled=not _dirty, key="save_guidance_edit"):
        save_guidance(_edited, "Manual edit in admin UI", "manual_edit")
        st.session_state.pop("revision_proposal", None)
        st.toast("Saved a new version.")
        st.rerun()
    e2.download_button(
        "Download .md", data=_edited, file_name="guidance.md",
        mime="text/markdown", use_container_width=True, key="dl_guidance",
    )
    if _dirty:
        st.caption("Unsaved changes — Save to create a new version, or reload to discard.")

    # ---- Feedback-driven revision --------------------------------------------
    st.divider()
    st.subheader("Revise from labeler feedback")
    _fb_total = sum(_fb_counts.values())
    _fb_labels = {
        "hand_added": "Hand-added rules",
        "rule_comments": "Rule comments",
    }
    _fcols = st.columns(len(_fb_counts))
    for _col, (_k, _v) in zip(_fcols, _fb_counts.items()):
        _col.metric(_fb_labels.get(_k, _k.replace("_", " ").title()), _v)
    if _fb_total == 0:
        st.caption("No labeler feedback yet — hand-added rules and comments on rules "
                   "show up here once labelers review the output.")

    if st.button(
        "Propose update from feedback", type="primary", use_container_width=True,
        disabled=_fb_total == 0, key="propose_revision",
        help="Sends the current document + a feedback digest to the model and asks "
             "for a minimally-edited revision. Costs one model call.",
    ):
        with st.spinner("Reading feedback and asking the model…"):
            st.session_state["revision_proposal"] = propose_revision(
                model=_rev_model, current_doc=_doc
            )
        st.rerun()

    _prop = st.session_state.get("revision_proposal")
    if _prop is not None:
        st.subheader("Proposed revision")
        if not _prop.parse_ok:
            st.error(f"Model reply didn't parse: {_prop.parse_error}")
            with st.expander("Raw response"):
                st.code(_prop.raw_response or "(empty)")
            if st.button("Dismiss", key="dismiss_revision"):
                st.session_state.pop("revision_proposal", None)
                st.rerun()
        elif _prop.new_content.strip() == _doc.strip():
            if _prop.summary:
                st.markdown(f"**Summary** — {_prop.summary}")
            st.info("The model judged the feedback too thin to warrant a change — "
                    "the document is unchanged.")
            if st.button("Dismiss", key="dismiss_revision"):
                st.session_state.pop("revision_proposal", None)
                st.rerun()
        else:
            if _prop.summary:
                st.markdown(f"**Summary** — {_prop.summary}")
            st.markdown(_render_guidance_diff(_doc, _prop.new_content),
                        unsafe_allow_html=True)
            a1, a2, _acol = st.columns([1, 1, 2])
            if a1.button("Approve & save", type="primary",
                         use_container_width=True, key="approve_revision"):
                save_guidance(_prop.new_content,
                              _prop.summary or "Feedback revision",
                              "feedback_revision")
                st.session_state.pop("revision_proposal", None)
                st.toast("Saved the revised document.")
                st.rerun()
            if a2.button("Discard", use_container_width=True, key="discard_revision"):
                st.session_state.pop("revision_proposal", None)
                st.rerun()
        _it, _ot = _prop.input_tokens or 0, _prop.output_tokens or 0
        st.caption(f"Revision call · {model_label(_prop.model)} · "
                   f"in {_it} / out {_ot} tokens")

    # ---- Version history -----------------------------------------------------
    st.divider()
    st.subheader(f"Version history ({len(_versions)})")
    if not _versions:
        st.caption("No saved versions yet.")
    for _ver in _versions:
        _is_cur = _ver.version_no == _cur_no
        _hdr = (f"v{_ver.version_no} · {_ver.source.replace('_', ' ')} · "
                f"{_fmt_local(_ver.created_at, '%Y-%m-%d %H:%M')}"
                + ("  ·  current" if _is_cur else ""))
        with st.expander(_hdr, expanded=False):
            if _ver.summary:
                st.markdown(f"_{_ver.summary}_")
            st.code(_ver.content, language="markdown")
            if not _is_cur and st.button(
                f"Restore v{_ver.version_no}", key=f"restore_{_ver.version_no}",
                help="Saves this version's content as a new current version.",
            ):
                save_guidance(_ver.content, f"Restored from v{_ver.version_no}",
                              "restore")
                st.session_state.pop("revision_proposal", None)
                st.toast(f"Restored v{_ver.version_no} as a new version.")
                st.rerun()


# ---------------------------------------------------------------------------
# Maintenance — row counts + danger zone (clear extracted data)
# ---------------------------------------------------------------------------
with tab_maint:
    st.header("Maintenance")

    # ---- Row counts (original Stats functionality) ----
    st.subheader("Row counts")
    if (_cleared_n := st.session_state.pop("clear_done", None)) is not None:
        st.success(
            f"Cleared {_cleared_n:,} rows. Labeler identities, settings, and the crawl "
            "audit log were preserved."
        )
    with conn() as c:
        rows = []
        for tbl in ("source_project", "rules_file", "source_block", "rule",
                    "rule_llm_decision", "rule_extraction_label",
                    "rule_classification_label", "rule_comment",
                    "source_block_comment", "labeler",
                    "ingestion_run", "ingestion_event"):
            count = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            rows.append({"table": tbl, "count": count})
        st.table(pd.DataFrame(rows))

    # ---- Danger zone: clear all extracted data ----
    st.divider()
    st.markdown('<p class="danger-head">Danger zone</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="section-lead">Erase every extracted rule and everything tied to it — '
        'projects, files, source blocks, judge decisions, extraction &amp; classification '
        'labels, and comments. <strong>Labeler identities, settings (models, spend cap, '
        'guidance addendum), and the crawl run/event history are preserved.</strong> '
        'This cannot be undone.</p>',
        unsafe_allow_html=True,
    )

    with conn() as c:
        _wipe = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in _CLEAR_DELETE_ORDER}
    _wipe_total = sum(_wipe.values())

    if _wipe_total == 0:
        st.caption("Nothing to clear — the corpus is already empty.")
    else:
        st.caption("Will delete — " + " · ".join(
            f"{t} **{n:,}**" for t, n in _wipe.items() if n))

    if is_running:
        st.warning(
            "Stop the crawl before clearing — wiping the corpus during an active crawl "
            "would race the writer."
        )

    _confirm = st.text_input(
        'Type DELETE to confirm', key="clear_confirm",
        disabled=is_running or _wipe_total == 0,
        placeholder="DELETE",
    )
    if st.button("Clear all extracted data", type="primary",
                 disabled=is_running or _wipe_total == 0 or _confirm != "DELETE",
                 key="clear_button"):
        with st.spinner("Clearing…"):
            _deleted = clear_extracted_data()
        st.session_state["clear_done"] = sum(_deleted.values())
        st.session_state.pop("clear_confirm", None)
        st.rerun()
