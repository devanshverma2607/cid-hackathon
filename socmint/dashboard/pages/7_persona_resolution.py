"""Page 7 — Identity Resolution. Cluster a case's scattered accounts into
confidence-scored human personas (the "how many people am I looking at?" view)."""
from __future__ import annotations

import pathlib
import sys

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, case_selector, fmt_dt, require_case, short_id,
    tier_badge, tier_color,
)

st.set_page_config(page_title="Identity Resolution", page_icon="🧬", layout="wide")
st.title("🧬 Identity Resolution")
st.caption(
    "Fuses every collected signal — shared emails, phones, reused handles, "
    "matching profile photos, linked URLs — to resolve scattered platform "
    "accounts into distinct **human personas**."
)

# Distinct colour per persona for the cluster map.
PERSONA_PALETTE = [
    "#3498db", "#e67e22", "#9b59b6", "#1abc9c", "#e84393",
    "#f39c12", "#2ecc71", "#e74c3c", "#16a085", "#d35400",
    "#2980b9", "#8e44ad",
]


def persona_color(idx: int) -> str:
    return PERSONA_PALETTE[idx % len(PERSONA_PALETTE)]


case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

if st.sidebar.button("🔄 Recompute personas", use_container_width=True):
    st.rerun()

data = api_get(f"/api/v1/persona/{case_id}")
if data is None:
    st.stop()

account_count = data.get("account_count", 0)
personas = data.get("personas", [])
edges = data.get("edges", [])
multi = [p for p in personas if p.get("account_count", 0) > 1]
singletons = [p for p in personas if p.get("account_count", 0) <= 1]

if account_count == 0:
    st.info("No positive evidence yet. Run the pipeline for this case first.")
    st.stop()

# --- headline metrics ----------------------------------------------------
m1, m2, m3, m4 = st.columns(4)
m1.metric("Accounts analysed", account_count)
m2.metric("Resolved personas", len(multi), help="Clusters of ≥2 linked accounts")
m3.metric("Unlinked accounts", len(singletons))
high = sum(1 for p in multi if p.get("confidence_tier") == "HIGH")
m4.metric("High-confidence", high)

if not multi:
    st.success(
        f"Analysed {account_count} accounts — none could be linked into a "
        "multi-account persona yet. Each appears to be an isolated presence."
    )

# --- persona id → colour / label maps for the cluster map ----------------
persona_color_of: dict[str, str] = {}
acct_to_persona: dict[str, str] = {}
acct_label: dict[str, str] = {}
for idx, p in enumerate(multi):
    persona_color_of[p["persona_id"]] = persona_color(idx)
    for acc in p.get("accounts", []):
        acct_to_persona[acc["id"]] = p["persona_id"]
        acct_label[acc["id"]] = acc.get("platform") or acc.get("label") or acc["id"]

# --- persona cards -------------------------------------------------------
for idx, p in enumerate(multi):
    color = persona_color(idx)
    tier = p.get("confidence_tier", "LOW")
    pivot = p.get("pivot_identifier") or {}
    with st.container(border=True):
        head_l, head_r = st.columns([3, 1])
        with head_l:
            st.markdown(
                f"<h3 style='margin:0'>"
                f"<span style='color:{color}'>●</span> {p['persona_id']} "
                f"&nbsp;{tier_badge(tier)} "
                f"<span style='color:#888;font-size:0.9rem'>score {p.get('score', 0)}</span>"
                f"</h3>",
                unsafe_allow_html=True,
            )
            st.write(p.get("explanation", ""))
        with head_r:
            st.metric("Accounts", p.get("account_count", 0))
            st.metric("Platforms", p.get("platform_count", 0))

        if pivot:
            st.markdown(
                f"<div style='background:#1e1e2e;border-left:4px solid {color};"
                f"padding:8px 12px;border-radius:4px;margin:6px 0'>"
                f"🔑 <b>Pivot {pivot.get('kind')}</b>: "
                f"<code>{pivot.get('value')}</code> — the identifier that holds "
                f"this persona together.</div>",
                unsafe_allow_html=True,
            )

        # linking signals as chips
        signals = p.get("linking_signals", [])
        if signals:
            chips = " ".join(
                f"<span style='background:#2d2d3d;color:#ddd;padding:3px 10px;"
                f"border-radius:12px;font-size:0.8rem;margin-right:6px'>"
                f"{s['label']} · {s['count']}</span>"
                for s in signals
            )
            st.markdown("**Why these accounts link**", help="Signals fused across the cluster")
            st.markdown(chips, unsafe_allow_html=True)

        # shared identifiers
        shared = p.get("shared_identifiers", {})
        share_bits = []
        for kind, vals in (("emails", shared.get("emails")),
                           ("usernames", shared.get("usernames")),
                           ("phones", shared.get("phones"))):
            if vals:
                share_bits.append(f"**{kind}:** " + ", ".join(f"`{v}`" for v in vals))
        if share_bits:
            st.markdown("&nbsp;&nbsp;".join(share_bits))

        tl = p.get("timeline", {})
        st.caption(
            f"First seen {fmt_dt(tl.get('first_seen'))} · "
            f"last seen {fmt_dt(tl.get('last_seen'))}"
        )

        with st.expander(f"Member accounts ({p.get('account_count', 0)})"):
            rows = []
            for acc in p.get("accounts", []):
                rows.append({
                    "Platform": acc.get("platform"),
                    "Account": acc.get("label"),
                    "Usernames": ", ".join(acc.get("usernames", [])),
                    "Emails": ", ".join(acc.get("emails", [])),
                    "Tools": ", ".join(acc.get("tools", [])),
                    "URL": acc.get("url") or "",
                })
            df = pd.DataFrame(rows)
            st.dataframe(
                df, use_container_width=True, hide_index=True,
                column_config={"URL": st.column_config.LinkColumn("URL", display_text="open")},
            )

# --- identity cluster map ------------------------------------------------
cluster_edges = [
    e for e in edges
    if e["source"] in acct_to_persona and e["target"] in acct_to_persona
]
if multi and cluster_edges:
    st.subheader("🗺️ Identity cluster map")
    st.caption(
        "Each colour is one resolved persona. Solid lines are hard links "
        "(merge the accounts); dotted lines are softer corroborating signals."
    )

    g = nx.Graph()
    for acc_id in acct_to_persona:
        g.add_node(acc_id)
    for e in cluster_edges:
        g.add_edge(e["source"], e["target"])
    pos = nx.spring_layout(g, seed=42, k=0.7)

    # edges: solid (hard/merge) vs dotted (soft)
    hard_x, hard_y, soft_x, soft_y = [], [], [], []
    for e in cluster_edges:
        if e["source"] not in pos or e["target"] not in pos:
            continue
        x0, y0 = pos[e["source"]]
        x1, y1 = pos[e["target"]]
        if e.get("hard") or e.get("merges"):
            hard_x += [x0, x1, None]
            hard_y += [y0, y1, None]
        else:
            soft_x += [x0, x1, None]
            soft_y += [y0, y1, None]

    edge_traces = [
        go.Scatter(x=soft_x, y=soft_y, mode="lines", name="soft signal",
                   line=dict(width=1, color="#888", dash="dot"), hoverinfo="none"),
        go.Scatter(x=hard_x, y=hard_y, mode="lines", name="hard link",
                   line=dict(width=2, color="#bbb"), hoverinfo="none"),
    ]

    node_ids = list(g.nodes())
    node_trace = go.Scatter(
        x=[pos[n][0] for n in node_ids],
        y=[pos[n][1] for n in node_ids],
        mode="markers+text",
        text=[acct_label.get(n, n) for n in node_ids],
        textposition="top center",
        hovertext=[f"{acct_label.get(n, n)}<br>{acct_to_persona.get(n)}" for n in node_ids],
        hoverinfo="text",
        marker=dict(
            size=20,
            color=[persona_color_of.get(acct_to_persona.get(n), "#888") for n in node_ids],
            line=dict(width=2, color="#ffffff"),
        ),
        showlegend=False,
    )

    fig = go.Figure(data=edge_traces + [node_trace])
    fig.update_layout(
        height=620, margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
    )
    st.plotly_chart(fig, use_container_width=True)

    legend = "  ".join(
        f"<span style='color:{persona_color_of[p['persona_id']]};font-weight:600'>●</span> "
        f"{p['persona_id']} ({p['account_count']})"
        for p in multi
    )
    st.markdown(legend, unsafe_allow_html=True)

# --- singletons ----------------------------------------------------------
if singletons:
    with st.expander(f"Unlinked accounts ({len(singletons)})"):
        rows = []
        for p in singletons:
            acc = (p.get("accounts") or [{}])[0]
            rows.append({
                "Platform": acc.get("platform"),
                "Account": acc.get("label"),
                "Usernames": ", ".join(acc.get("usernames", [])),
                "Emails": ", ".join(acc.get("emails", [])),
                "Tools": ", ".join(acc.get("tools", [])),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.caption(f"Case {short_id(case_id)} · {account_count} accounts · "
           f"{len(multi)} personas · {len(singletons)} unlinked")
