"""Page 3 — Evidence Explorer. Browse, filter, and inspect every raw finding."""
from __future__ import annotations

import html
import pathlib
import sys

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, api_get_bytes, case_selector, fmt_dt, require_case,
    status_icon,
)

st.set_page_config(page_title="Evidence Explorer", page_icon="🔎", layout="wide")
st.title("🔎 Evidence Explorer")
st.caption("Every preserved finding for the case — filter, inspect, and view snapshots.")

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

AVATAR_KEYS = ("avatar_url", "profile_pic_url", "avatar", "picture", "image_url")


def _looks_like_url(value) -> bool:
    return isinstance(value, str) and value.startswith("http")


def render_enrichment(enrich: dict) -> None:
    """Render platform_enrichment: avatar image, phone intel, pHash, raw rest."""
    if not enrich:
        return
    avatar = next(
        (enrich[k] for k in AVATAR_KEYS if _looks_like_url(enrich.get(k))), None
    )
    if avatar:
        st.image(avatar, width=120, caption="avatar")
    flat = {k: v for k, v in enrich.items()
            if not isinstance(v, (dict, list)) and k not in AVATAR_KEYS}
    if flat:
        st.table(pd.DataFrame(list(flat.items()), columns=["field", "value"]))
    nested = {k: v for k, v in enrich.items() if isinstance(v, (dict, list))}
    if nested:
        st.json(nested, expanded=False)


# --- load ----------------------------------------------------------------
include_unavailable = st.toggle(
    "Include unavailable / blocked results", value=False,
    help="Show negative checks (account not found, rate-limited, etc.).",
)
data = api_get(f"/api/v1/evidence/{case_id}",
               params={"include_unavailable": include_unavailable})
if data is None:
    st.stop()

evidence = data.get("evidence", [])
if not evidence:
    st.info("No evidence yet. Run the pipeline from **Pipeline Status**.")
    st.stop()

# --- filters -------------------------------------------------------------
platforms = sorted({e["source_platform"] for e in evidence if e.get("source_platform")})
types = sorted({e["result_type"] for e in evidence if e.get("result_type")})
tools = sorted({e["tool_name"] for e in evidence if e.get("tool_name")})

f1, f2, f3 = st.columns(3)
sel_platforms = f1.multiselect("Platform", platforms)
sel_types = f2.multiselect("Result type", types)
sel_tools = f3.multiselect("Tool", tools)
search = st.text_input("Search value", placeholder="substring match on result value…")

filtered = [
    e for e in evidence
    if (not sel_platforms or e.get("source_platform") in sel_platforms)
    and (not sel_types or e.get("result_type") in sel_types)
    and (not sel_tools or e.get("tool_name") in sel_tools)
    and (not search or search.lower() in str(e.get("result_value", "")).lower())
]

preserved = sum(1 for e in filtered if e.get("timestamp_preserved"))
enriched = sum(1 for e in filtered if e.get("platform_enrichment"))
m1, m2, m3, m4 = st.columns(4)
m1.metric("Findings", len(filtered))
m2.metric("Preserved", preserved)
m3.metric("Enriched", enriched)
m4.metric("Platforms", len({e.get("source_platform") for e in filtered}))

tab_table, tab_detail = st.tabs(["Table", "Details · snapshots · enrichment"])

with tab_table:
    df = pd.DataFrame([
        {
            "tier": e.get("tool_tier"),
            "tool": e.get("tool_name"),
            "platform": e.get("source_platform"),
            "type": e.get("result_type"),
            "value": e.get("result_value"),
            "link": e.get("result_value") if _looks_like_url(e.get("result_value")) else None,
            "conf": round(e.get("confidence_raw") or 0, 2),
            "preserved": "✓" if e.get("timestamp_preserved") else "",
            "collected": fmt_dt(e.get("timestamp_collected")),
        }
        for e in filtered
    ])
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "link": st.column_config.LinkColumn("open", display_text="↗", width="small"),
            "value": st.column_config.TextColumn("value", width="large"),
        },
    )

with tab_detail:
    st.caption(f"Showing {len(filtered)} findings. Expand any to inspect.")
    for e in filtered:
        dot = status_icon("done" if e.get("timestamp_preserved") else "pending")
        title = (f"{dot} {e.get('tool_name')} · {e.get('source_platform')} · "
                 f"{e.get('result_type')}")
        with st.expander(title):
            value = e.get("result_value")
            if _looks_like_url(value):
                st.markdown(f"[{value}]({value})")
            else:
                st.code(str(value), language="text")

            c1, c2, c3 = st.columns(3)
            c1.metric("Confidence", round(e.get("confidence_raw") or 0, 2))
            c2.write(f"**Collected**\n\n{fmt_dt(e.get('timestamp_collected'))}")
            c3.write(f"**Preserved**\n\n{fmt_dt(e.get('timestamp_preserved'))}")

            if e.get("snapshot_hash"):
                st.caption(f"snapshot sha256 · `{e['snapshot_hash']}`")
            if e.get("wayback_ref"):
                st.markdown(f"[Wayback snapshot]({e['wayback_ref']})")
            if e.get("notes"):
                st.caption(e["notes"])

            if e.get("platform_enrichment"):
                st.markdown("**Enrichment**")
                render_enrichment(e["platform_enrichment"])

            if e.get("snapshot_ref"):
                if st.toggle("Preserved snapshot", key=f"snap_{e['evidence_id']}"):
                    snap = api_get_bytes(
                        f"/api/v1/evidence/{case_id}/snapshot/{e['evidence_id']}"
                    )
                    if snap:
                        st.download_button(
                            "Download snapshot (HTML)", data=snap,
                            file_name=f"{e['evidence_id']}_snapshot.html",
                            mime="text/html", key=f"dl_snap_{e['evidence_id']}",
                        )
                        srcdoc = html.escape(snap.decode("utf-8", errors="replace"))
                        components.html(
                            f'<iframe sandbox style="width:100%;height:520px;'
                            f'border:1px solid #ddd" srcdoc="{srcdoc}"></iframe>',
                            height=540,
                        )
                        st.caption("Scripts disabled · remote assets may still load.")
                    else:
                        st.caption("Preserved snapshot unavailable.")
