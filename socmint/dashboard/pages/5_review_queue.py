"""Page 5 — Review Queue. Adjudicate MEDIUM-confidence identity links."""
from __future__ import annotations

import pathlib
import sys

import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, api_post, case_selector, require_case, tier_badge,
)

st.set_page_config(page_title="Review Queue", page_icon="✅", layout="wide")
st.title("✅ Review Queue")
st.caption("MEDIUM-confidence links require an analyst decision before reporting.")

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

analyst_id = st.text_input("Your Analyst ID", value=st.session_state.get("analyst_id", ""))
if analyst_id:
    st.session_state["analyst_id"] = analyst_id

data = api_get(f"/api/v1/evidence/{case_id}/review-queue")
if data is None:
    st.stop()

queue = data.get("queue", [])
st.metric("Awaiting review", len(queue))

if not queue:
    st.success("No links awaiting review.")
    st.stop()

for item in queue:
    with st.container(border=True):
        top = st.columns([5, 1])
        top[0].markdown(
            f"**{item['account_a']}** ({item['platform_a']})  ⇄  "
            f"**{item['account_b']}** ({item['platform_b']})"
        )
        top[1].markdown(tier_badge(item.get("confidence_tier", "MEDIUM")),
                        unsafe_allow_html=True)

        score = float(item.get("confidence_score") or 0)
        st.progress(min(score, 100) / 100.0,
                    text=f"Confidence {score:.0f} · {item.get('signal_count', 0)} signals")
        with st.expander("Signal breakdown"):
            st.json(item.get("signal_breakdown", {}))

        note = st.text_input("Note", key=f"note_{item['link_id']}")
        c1, c2, c3 = st.columns(3)
        for col, decision, label in (
            (c1, "CONFIRMED", "Confirm"),
            (c2, "REJECTED", "Reject"),
            (c3, "FLAG_UNCERTAIN", "Flag"),
        ):
            if col.button(label, key=f"{decision}_{item['link_id']}",
                          use_container_width=True):
                if not analyst_id:
                    st.warning("Enter your Analyst ID first.")
                else:
                    resp = api_post(
                        f"/api/v1/evidence/review/{item['link_id']}",
                        json={"decision": decision, "analyst_id": analyst_id, "note": note},
                    )
                    if resp is not None and resp.ok:
                        st.success(f"Recorded: {decision}")
                        st.rerun()
                    elif resp is not None:
                        st.error(f"Failed: {resp.text}")
