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

_STATE_BADGE = {
    "running": ("#3498db", "🔵 SCANNING"),
    "complete": ("#2ecc71", "🟢 COMPLETE"),
    "idle": ("#95a5a6", "⚪ IDLE"),
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


col_a, col_b = st.columns([1, 1])
live = col_a.toggle("Live mode — auto-refresh while scanning", value=True)

# Cadence is driven by the last observed scan state (cached in session so the
# fragment can escalate to a full rerun to start/stop the 1s timer).
polling = st.session_state.get("pipeline_running", True)
interval = 1.0 if (live and polling) else None

if not live:
    if col_b.button("🔄 Refresh now", use_container_width=True):
        st.rerun()


@st.fragment(run_every=interval)
def render_status() -> None:
    status = api_get(f"/api/v1/pipeline/status/{case_id}")
    if status is None:
        return

    state = status.get("state", "idle")
    color, label = _STATE_BADGE.get(state, _STATE_BADGE["idle"])
    elapsed = status.get("elapsed_seconds", 0)

    # --- live scan banner ---------------------------------------------------
    badge = (
        f"<span style='background:{color};color:#0b0b0b;padding:4px 14px;"
        f"border-radius:14px;font-weight:700;font-size:0.95rem'>{label}</span>"
    )
    duration_word = "Elapsed" if state == "running" else "Total scan duration"
    spinner = " ⏱️" if state == "running" else ""
    if state == "running":
        tail = (f" · last activity {fmt_dt(status.get('last_activity_at'))}"
                if status.get("last_activity_at") else "")
    else:
        tail = (f" · finished {fmt_dt(status.get('last_activity_at'))}"
                if status.get("last_activity_at") else "")
    st.markdown(
        f"{badge}&nbsp;&nbsp;<span style='font-size:1.6rem;font-weight:700'>"
        f"{fmt_duration(elapsed)}</span>{spinner}"
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
    bar_label = f"{done} reported · {skipped} skipped · {pending} pending  (of {total} tools)"
    st.progress(min(1.0, pct), text=bar_label)

    # --- headline metrics ---------------------------------------------------
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Hits", status.get("total_hits", 0))
    m2.metric("Preserved", status.get("preservation_complete", 0))
    m3.metric("HIGH Links", status.get("high_confidence_links", 0))
    m4.metric("Tools reported", f"{done}/{total}")

    st.caption(f"refreshed {datetime.now().strftime('%H:%M:%S')}"
               + ("  ·  polling every 1s" if state == "running" and live else ""))

    # --- per-tier tool grid -------------------------------------------------
    for tier in ("tier1", "tier2", "tier3", "tier4"):
        tools = status.get(tier, [])
        if not tools:
            continue
        tdone = sum(1 for t in tools if t.get("status") == "done")
        st.subheader(f"Tier {tier[-1]}  ·  {tdone}/{len(tools)} done")
        cols = st.columns(4)
        for i, tool in enumerate(tools):
            with cols[i % 4]:
                st.markdown(
                    f"{status_icon(tool.get('status'))} **{tool.get('tool')}**  \n"
                    f"{tool.get('status')} · {tool.get('hits', 0)} hits"
                )

    # --- lifecycle: keep session state in sync; escalate to a full rerun on a
    # running<->stopped transition so the 1s timer starts/stops cleanly.
    is_running = state == "running"
    if live and is_running != st.session_state.get("pipeline_running", None):
        st.session_state["pipeline_running"] = is_running
        st.rerun()


render_status()
