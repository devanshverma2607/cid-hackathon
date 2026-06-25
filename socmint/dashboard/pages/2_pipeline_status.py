"""Page 2 — Pipeline Status. Live Tier 1-4 tool execution view with a running
scan timer that auto-refreshes while a scan is active and freezes on completion."""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime

import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, case_selector, fmt_dt, require_case, status_icon,
)

st.set_page_config(page_title="Pipeline Status", page_icon="⚙️", layout="wide")
st.title("⚙️ Pipeline Status")
st.caption("Watch Tier 1-4 tools execute. The scan timer ticks live and stops when the run completes.")

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

_PHASE_STYLE = {
    "queued":    ("#5b6776", "QUEUED",    "⏳"),
    "sweeping":  ("#3498db", "SCANNING",  "🔵"),
    "analysing": ("#9b59b6", "ANALYSING", "🧠"),
    "pivoting":  ("#8e44ad", "PIVOTING",  "🔀"),
    "complete":  ("#2ecc71", "COMPLETE",  "🟢"),
    "idle":      ("#95a5a6", "IDLE",      "⚪"),
    "failed":    ("#e74c3c", "FAILED",    "🔴"),
}


def fmt_duration(seconds: float) -> str:
    """Human duration: 1h 02m 03s / 4m 12s / 9.4s."""
    seconds = max(0.0, float(seconds or 0))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s"


def tool_icon(tool: dict, active: bool) -> str:
    """Status glyph for a tool cell — 'pending' animates only while scanning."""
    status = tool.get("status")
    if status == "pending":
        return "🔵" if active else "⚪"
    return status_icon(status)


# Cadence is driven by the last observed scan state (cached in session). Any
# active phase (queued/sweeping/analysing/pivoting) reports state == "running".
#
# The fragment ALWAYS keeps ticking — fast (1s) while a scan is live, slow (5s)
# when it looks idle/complete — instead of stopping dead on the first
# non-"running" reading. This is the key robustness fix: a scan legitimately has
# quiet gaps (a slow Tier-2 tool like enola runs ~90s producing nothing, the
# correlation watchdog can defer the post-sweep stage, pivots spin up between
# hops). With a hard stop, one such gap froze the whole view at a premature
# "complete" and it never recovered. With an always-on slow heartbeat the view
# self-heals: if work resumes, the next poll sees it and escalates back to 1s.
FAST_POLL = 1.0
SLOW_POLL = 5.0

top = st.columns([2, 1, 1])
live = top[0].toggle("Live mode — auto-refresh while scanning", value=True)
active_hint = st.session_state.get("pipeline_active", True)
interval = (FAST_POLL if active_hint else SLOW_POLL) if live else None
if top[1].button("🔄 Refresh", use_container_width=True):
    st.rerun()


@st.fragment(run_every=interval)
def render_status() -> None:
    status = api_get(f"/api/v1/pipeline/status/{case_id}")
    if status is None:
        return

    state = status.get("state", "idle")
    phase = status.get("phase", "idle")
    active = state == "running"
    color, short, glyph = _PHASE_STYLE.get(phase, _PHASE_STYLE["idle"])
    elapsed = status.get("elapsed_seconds", 0)

    # --- live scan banner ---------------------------------------------------
    badge = (
        f"<span style='background:{color};color:#0b0b0b;padding:4px 14px;"
        f"border-radius:14px;font-weight:700;font-size:0.95rem'>{glyph} {short}</span>"
    )
    spinner = " ⏱️" if active else ""
    duration_word = "Elapsed" if active else "Total scan duration"
    if active:
        at = status.get("active_tool")
        tail = f" · currently <b>{at}</b>" if at else (
            f" · last activity {fmt_dt(status.get('last_activity_at'))}"
            if status.get("last_activity_at") else " · awaiting first results"
        )
    else:
        fin = status.get("finished_at") or status.get("last_activity_at")
        tail = f" · finished {fmt_dt(fin)}" if fin else ""
    st.markdown(
        f"{badge}&nbsp;&nbsp;<span style='font-size:1.6rem;font-weight:700'>"
        f"{fmt_duration(elapsed)}</span>{spinner}"
        f"<br><span style='color:#9aa3ad;font-size:0.9rem'>"
        f"{status.get('phase_label', short)}</span>"
        f"<br><span style='color:#888;font-size:0.85rem'>{duration_word}"
        f" · started {fmt_dt(status.get('started_at'))}{tail}</span>",
        unsafe_allow_html=True,
    )

    # --- progress bar -------------------------------------------------------
    done = status.get("tools_done", 0)
    skipped = status.get("tools_skipped", 0)
    pending = status.get("tools_pending", 0)
    total = status.get("tools_total", 0)
    pct = 1.0 if state == "complete" else float(status.get("progress", 0.0))
    bar_label = (f"{done} reported · {skipped} ran-empty · {pending} pending  "
                 f"(of {total} expected tools)")
    st.progress(min(1.0, pct), text=bar_label)

    # --- headline metrics ---------------------------------------------------
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total hits", status.get("total_hits", 0))
    m2.metric("Preserved", status.get("preservation_complete", 0))
    m3.metric("HIGH links", status.get("high_confidence_links", 0))
    m4.metric("Tools reported", f"{done + skipped}/{total}")
    m5.metric("Pivot hops", status.get("pivot_hops", 0),
              help="Recursive expansion rounds the engine ran on discovered identifiers.")

    refreshed = datetime.now().strftime("%H:%M:%S")
    corr = "✓ correlation complete" if status.get("correlation_complete") else "correlation pending"
    cadence = ""
    if live:
        cadence = "  ·  polling every 1s" if active else "  ·  watching every 5s"
    st.caption(f"refreshed {refreshed} · {corr}{cadence}")

    # --- per-tier tool grid -------------------------------------------------
    for tier in ("tier1", "tier2", "tier3"):
        tools = status.get(tier, [])
        # Show tools relevant to this case (applicable to its seeds, or that ran);
        # mute the rest into a small footnote so the grid is honest, not noisy.
        shown = [t for t in tools if t.get("applicable") or t.get("status") in ("done", "skipped")]
        muted = [t for t in tools if t not in shown]
        if not shown and not muted:
            continue
        tdone = sum(1 for t in shown if t.get("status") == "done")
        st.subheader(f"Tier {tier[-1]}  ·  {tdone}/{len(shown)} with hits")
        if shown:
            cols = st.columns(4)
            for i, tool in enumerate(shown):
                with cols[i % 4]:
                    st.markdown(
                        f"{tool_icon(tool, active)} **{tool.get('tool')}**  \n"
                        f"<span style='color:#9aa3ad;font-size:0.82rem'>"
                        f"{tool.get('status')} · {tool.get('hits', 0)} hits</span>",
                        unsafe_allow_html=True,
                    )
        if muted:
            st.caption("Not applicable to this case's seed type: "
                       + ", ".join(t.get("tool") for t in muted))

    # --- Tier 4 triggered enrichment ----------------------------------------
    tier4 = status.get("tier4", [])
    fired = [t for t in tier4 if t.get("triggered") or t.get("status") in ("done", "skipped")]
    e_fired = status.get("enrichment_triggered", 0)
    e_done = status.get("enrichment_done", 0)
    st.subheader(f"Tier 4 · triggered enrichment  ·  {e_fired} fired / {e_done} with hits")
    if fired:
        cols = st.columns(4)
        for i, tool in enumerate(fired):
            with cols[i % 4]:
                st.markdown(
                    f"{tool_icon(tool, active)} **{tool.get('tool')}**  \n"
                    f"<span style='color:#9aa3ad;font-size:0.82rem'>"
                    f"{tool.get('status')} · {tool.get('hits', 0)} hits</span>",
                    unsafe_allow_html=True,
                )
    else:
        st.caption("Tier 4 tools fire automatically once a profile or domain is "
                   "confirmed — none triggered yet.")

    # --- SDM (Social Depth Module) ------------------------------------------
    tier5 = status.get("tier5", [])
    sdm_fired = [t for t in tier5 if t.get("status") in ("done", "skipped", "pending")]
    if sdm_fired:
        sdm_done = sum(1 for t in sdm_fired if t.get("status") == "done")
        st.subheader(f"Social Depth Module  ·  {sdm_done}/{len(sdm_fired)} complete")
        cols = st.columns(4)
        for i, tool in enumerate(sdm_fired):
            with cols[i % 4]:
                st.markdown(
                    f"{tool_icon(tool, active)} **{tool.get('tool', '').replace('sdm_', '')}**  \n"
                    f"<span style='color:#9aa3ad;font-size:0.82rem'>"
                    f"{tool.get('status')} · {tool.get('hits', 0)} hits</span>",
                    unsafe_allow_html=True,
                )

    # --- lifecycle: keep the cached active-hint in sync and escalate the poll
    # cadence on a transition. The fragment never stops (it falls back to the
    # slow 5s heartbeat when not active), so the view can always recover if work
    # resumes after appearing complete — it just needs to flip the cadence.
    if live and active != st.session_state.get("pipeline_active", None):
        st.session_state["pipeline_active"] = active
        st.rerun()


render_status()
