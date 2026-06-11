"""Page 4 — Identity Graph. SAME_AS correlations plus pivot DISCOVERED edges."""
from __future__ import annotations

import pathlib
import sys

import networkx as nx
import plotly.graph_objects as go
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    KIND_COLORS, active_case_id, api_get, case_selector, kind_color, require_case,
    tier_color,
)

st.set_page_config(page_title="Identity Graph", page_icon="🕸️", layout="wide")
st.title("🕸️ Identity Graph")
st.caption("Correlated identities (SAME_AS) and the pivots that discovered them.")

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

c1, c2 = st.columns([1, 1])
include_pivots = c1.toggle("Show pivot edges (sources → identifiers)", value=True)
max_nodes = c2.slider("Max links", min_value=10, max_value=200, value=50, step=10)
color_by_community = st.toggle(
    "Colour accounts by community (Louvain / label propagation)", value=False,
    help="Sub-clusters within the identity graph — tightly-knit groups of accounts.",
)

graph = api_get(f"/api/v1/graph/{case_id}",
                params={"include_pivots": include_pivots, "max_nodes": max_nodes})
if graph is None:
    st.stop()

nodes = graph.get("nodes", [])
edges = graph.get("edges", [])
if not nodes:
    # The graph only contains HIGH/MEDIUM SAME_AS links + discovery pivots. A
    # case can still have many LOW-confidence links (and resolved personas) that
    # are intentionally not promoted to the graph — explain that rather than
    # implying nothing was found.
    st.info(
        "No HIGH/MEDIUM-confidence correlations to plot yet. The identity graph "
        "shows only strongly-linked accounts (and discovery pivots)."
    )
    status = api_get(f"/api/v1/reports/status/{case_id}") or {}
    n_links = status.get("identity_links", 0)
    if n_links:
        st.caption(
            f"This case has **{n_links}** identity link(s), but they are all "
            "below the graph threshold. Review them in **Review Queue**, or see "
            "clustered accounts in **Identity Resolution**."
        )
        gc1, gc2 = st.columns(2)
        gc1.page_link("pages/5_review_queue.py", label="Open Review Queue →", icon="✅")
        gc2.page_link("pages/7_persona_resolution.py",
                      label="Open Identity Resolution →", icon="🧬")
    else:
        st.caption("Run the pipeline from **Pipeline Status** to collect evidence first.")
    st.stop()

kinds_present = sorted({n.get("kind", "account") for n in nodes})
tiers_present = sorted({e.get("tier", "LOW") for e in edges})

g1, g2 = st.columns(2)
sel_kinds = g1.multiselect("Node types", kinds_present, default=kinds_present)
sel_tiers = g2.multiselect("Edge tiers", tiers_present, default=tiers_present)

fnodes = [n for n in nodes if n.get("kind", "account") in sel_kinds]
node_ids = {n["id"] for n in fnodes}
fedges = [
    e for e in edges
    if e.get("tier", "LOW") in sel_tiers
    and e["source"] in node_ids and e["target"] in node_ids
]

if not fnodes:
    st.warning("No nodes match the current filters.")
    st.stop()

# --- layout --------------------------------------------------------------
g = nx.Graph()
for n in fnodes:
    g.add_node(n["id"])
for e in fedges:
    g.add_edge(e["source"], e["target"])
pos = nx.spring_layout(g, seed=42, k=0.6)

# --- edge traces grouped by tier (colour + legend) -----------------------
edge_traces = []
by_tier: dict[str, list] = {}
for e in fedges:
    by_tier.setdefault(e.get("tier", "LOW"), []).append(e)
for tier, group in by_tier.items():
    xs, ys = [], []
    for e in group:
        if e["source"] not in pos or e["target"] not in pos:
            continue
        x0, y0 = pos[e["source"]]
        x1, y1 = pos[e["target"]]
        xs += [x0, x1, None]
        ys += [y0, y1, None]
    dash = "dot" if tier == "PIVOT" else "solid"
    edge_traces.append(go.Scatter(
        x=xs, y=ys, mode="lines", name=f"{tier} edge",
        line=dict(width=2, color=tier_color(tier), dash=dash),
        hoverinfo="none",
    ))

# --- single node trace, coloured per-kind --------------------------------
_COMMUNITY_PALETTE = [
    "#3498db", "#e67e22", "#9b59b6", "#1abc9c", "#e84393", "#f39c12",
    "#2ecc71", "#e74c3c", "#16a085", "#d35400", "#2980b9", "#8e44ad",
]


def _node_color(n: dict) -> str:
    if color_by_community and n.get("community") is not None:
        return _COMMUNITY_PALETTE[int(n["community"]) % len(_COMMUNITY_PALETTE)]
    return kind_color(n.get("kind", "account"))


node_trace = go.Scatter(
    x=[pos[n["id"]][0] for n in fnodes],
    y=[pos[n["id"]][1] for n in fnodes],
    mode="markers+text",
    text=[n.get("label", n["id"]) for n in fnodes],
    textposition="top center",
    hovertext=[
        f"{n.get('label')}<br>{n.get('kind')} · {n.get('platform')}"
        + (f"<br>community {n.get('community')}" if n.get("community") is not None else "")
        for n in fnodes
    ],
    hoverinfo="text",
    marker=dict(
        size=18,
        color=[_node_color(n) for n in fnodes],
        line=dict(width=2, color="#ffffff"),
    ),
    showlegend=False,
)

fig = go.Figure(data=edge_traces + [node_trace])
fig.update_layout(
    height=640, margin=dict(l=10, r=10, t=10, b=10),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0),
    xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
    yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
)
st.plotly_chart(fig, use_container_width=True)

legend = "  ".join(
    f"<span style='color:{c};font-weight:600'>●</span> {k}"
    for k, c in KIND_COLORS.items() if k in kinds_present
)
st.markdown(legend, unsafe_allow_html=True)

# --- community structure -------------------------------------------------
with st.expander("🧩 Community structure", expanded=False):
    st.caption(
        "Communities are tightly-knit sub-clusters of linked accounts (Louvain "
        "via Neo4j GDS, or a label-propagation fallback). They can refine a single "
        "persona into work / personal / alias groupings."
    )
    if st.button("Detect communities"):
        comm = api_get(f"/api/v1/graph/{case_id}/communities")
        if comm and comm.get("community_count"):
            st.markdown(
                f"**{comm['community_count']}** communit"
                f"{'y' if comm['community_count'] == 1 else 'ies'} "
                f"detected via `{comm.get('method')}`."
            )
            for sm in comm.get("summaries", []):
                st.markdown(
                    f"- Community **{sm['community_id']}** · {sm['size']} account(s)"
                )
                with st.container():
                    st.caption(", ".join(sm.get("members", [])[:10]))
            st.caption("Re-enable 'Colour accounts by community' above to map them.")
        elif comm is not None:
            st.info("No multi-account community structure found for this case yet.")

# --- node inspector ------------------------------------------------------
st.subheader("Inspect a node")
labels = {n["id"]: n.get("label", n["id"]) for n in fnodes}
pick = st.selectbox(
    "Node", [""] + [n["id"] for n in fnodes],
    format_func=lambda i: labels.get(i, "— pick a node —") if i else "— pick a node —",
)
if pick:
    node = next(n for n in fnodes if n["id"] == pick)
    d1, d2 = st.columns([2, 1])
    with d1:
        st.markdown(f"**{node.get('label')}**")
        st.write({
            "kind": node.get("kind"),
            "platform": node.get("platform"),
            "confidence": node.get("confidence"),
        })
        if node.get("url"):
            st.markdown(f"[Open profile]({node['url']})")
    with d2:
        neighbours = [
            e for e in fedges if e["source"] == pick or e["target"] == pick
        ]
        st.metric("Connections", len(neighbours))
    if neighbours:
        st.markdown("**Edges**")
        for e in neighbours:
            other = e["target"] if e["source"] == pick else e["source"]
            via = e.get("via_tool") or e.get("kind")
            st.markdown(
                f"- {labels.get(other, other)} · "
                f"`{e.get('tier')}` · {via}"
            )
