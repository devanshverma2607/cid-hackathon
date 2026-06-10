"""Page 9 — Subject Dossier. The headline, investigator-facing profile.

Calls the consolidated dossier endpoint (`/api/v1/dossier/{case_id}`) and renders
the inferred identity, attributes (graded by confidence), behavioral fingerprint,
activity timeline, interests, explainable reasoning, and recommended actions.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, case_selector, conf_bar, conf_color, metric_card,
    require_case, risk_gauge, score_color, timeline_chart,
)

st.set_page_config(page_title="Subject Dossier", page_icon="🕵️", layout="wide")
st.title("🕵️ Subject Dossier")
st.caption(
    "An algorithmically inferred profile of the subject — graded by confidence. "
    "These are investigative leads requiring analyst corroboration, not established facts."
)

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

with st.spinner("Synthesising dossier…"):
    dossier = api_get(f"/api/v1/dossier/{case_id}", timeout=60)
if not dossier:
    st.stop()

profile = dossier.get("profile", {})
insights = dossier.get("insights", {})
persona = dossier.get("persona", {})
head = dossier.get("headline", {})
attrs = profile.get("attributes", {})
fp = profile.get("footprint", {})


def _first_val(items):
    for it in items or []:
        if isinstance(it, dict):
            return it.get("value")
        return it
    return None


# ---------------------------------------------------------------------------
# Hero card — who is this?
# ---------------------------------------------------------------------------
name = head.get("name") or _first_val(attrs.get("names")) or "Unidentified subject"
handle = profile.get("behavioral_fingerprint", {}).get("dominant_handle")
location = _first_val(attrs.get("locations"))
occupation = _first_val(attrs.get("occupation"))

hero_bits = []
if handle:
    hero_bits.append(f"@{handle}")
if location:
    hero_bits.append(location)
if occupation:
    hero_bits.append(occupation)
sub_line = "  ·  ".join(hero_bits) or "No distinguishing attributes inferred yet"

avatar = (attrs.get("avatar_urls") or [None])[0]
hcol1, hcol2 = st.columns([1, 5])
with hcol1:
    if avatar:
        st.image(avatar, width=110)
    else:
        st.markdown(
            "<div style='width:110px;height:110px;border-radius:12px;background:#1c2128;"
            "display:flex;align-items:center;justify-content:center;font-size:2.4rem;"
            "color:#3498db'>👤</div>",
            unsafe_allow_html=True,
        )
with hcol2:
    st.markdown(f"## {name}")
    st.markdown(f"<span style='color:#9aa3ad;font-size:1.05rem'>{sub_line}</span>",
                unsafe_allow_html=True)
    if profile.get("summary"):
        st.markdown(f"<p style='color:#cfd4da;margin-top:8px'>{profile['summary']}</p>",
                    unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# Headline metrics + gauges
# ---------------------------------------------------------------------------
g1, g2, g3 = st.columns([1, 1, 2])
with g1:
    st.plotly_chart(
        risk_gauge(fp.get("footprint_score", 0), fp.get("visibility", ""),
                   title="Footprint"),
        use_container_width=True,
    )
with g2:
    risk = insights.get("risk", {})
    st.plotly_chart(
        risk_gauge(risk.get("score", 0), risk.get("band", ""), title="Exposure"),
        use_container_width=True,
    )
with g3:
    mc1, mc2 = st.columns(2)
    with mc1:
        st.markdown(metric_card("Platforms", fp.get("platform_count", 0),
                                sub=f"{fp.get('confirmed_accounts', 0)} confirmed"),
                    unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        st.markdown(metric_card("Distinct personas", persona.get("persona_count", 0),
                                sub="identity clusters", color="#9b59b6"),
                    unsafe_allow_html=True)
    with mc2:
        comp = profile.get("profile_completeness", {})
        st.markdown(metric_card("Completeness", f"{comp.get('score', 0)}%",
                                sub="profile coverage",
                                color=score_color(100 - comp.get("score", 0))),
                    unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        tspan = profile.get("temporal", {}).get("active_span_days")
        st.markdown(metric_card("Active span",
                                f"{tspan} d" if tspan is not None else "—",
                                sub=profile.get("temporal", {}).get("active_era", ""),
                                color="#1abc9c"),
                    unsafe_allow_html=True)

st.divider()

# ---------------------------------------------------------------------------
# Inferred attributes (confidence-graded) + identity
# ---------------------------------------------------------------------------
acol, icol = st.columns([3, 2])

with acol:
    st.subheader("Inferred Attributes")

    def render_attr(label, items):
        if not items:
            return
        top = items[0] if isinstance(items, list) else items
        if isinstance(top, dict):
            st.markdown(
                conf_bar(label, top.get("confidence", "low"), value=str(top.get("value", ""))),
                unsafe_allow_html=True,
            )
            extra = [str(x.get("value", "")) for x in items[1:4] if isinstance(x, dict)]
            if extra:
                st.caption("also: " + ", ".join(extra))
        else:
            st.markdown(conf_bar(label, "low", value=str(top)), unsafe_allow_html=True)

    render_attr("Name", attrs.get("names"))
    render_attr("Location", attrs.get("locations"))
    render_attr("Occupation", attrs.get("occupation"))
    render_attr("Languages", attrs.get("languages"))
    render_attr("Affiliations", attrs.get("affiliations"))

    meta_bits = []
    if attrs.get("timezone"):
        meta_bits.append(f"🕓 {attrs['timezone']}")
    if attrs.get("phone_region"):
        meta_bits.append(f"📞 {attrs['phone_region']}")
    if attrs.get("websites"):
        meta_bits.append("🔗 " + ", ".join(attrs["websites"][:2]))
    if meta_bits:
        st.markdown("  ·  ".join(meta_bits))

    if attrs.get("bios"):
        with st.expander("Collected bios / headlines"):
            for b in attrs["bios"][:6]:
                st.markdown(f"> {b}")

with icol:
    st.subheader("Confirmed Identity")
    ident = profile.get("identity", {})
    for key, icon in (("emails", "✉️"), ("phones", "📱"), ("usernames", "👤")):
        vals = ident.get(key, [])
        if vals:
            st.markdown(f"**{icon} {key.capitalize()}**")
            for item in vals[:6]:
                val = item.get("value") if isinstance(item, dict) else item
                obs = item.get("observations") if isinstance(item, dict) else None
                suffix = f"  ·  seen {obs}×" if obs and obs > 1 else ""
                st.markdown(f"- `{val}`{suffix}")
    if ident.get("verified_on"):
        st.caption("Verified on: " + ", ".join(ident["verified_on"][:8]))

st.divider()

# ---------------------------------------------------------------------------
# Behavioral fingerprint
# ---------------------------------------------------------------------------
bf = profile.get("behavioral_fingerprint", {})
if bf:
    st.subheader("Behavioral Fingerprint")
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Handle consistency", f"{round(bf.get('handle_consistency', 0) * 100)}%")
    b2.metric("Distinct handles", bf.get("distinct_handles", 0))
    b3.metric("Cross-platform", f"{bf.get('cross_platform_consistency', 0)}%")
    b4.metric("Avatar reuse", bf.get("avatar_reuse_count", 0))

    traits = []
    if bf.get("uses_numeric_suffix"):
        traits.append("appends numbers")
    if bf.get("uses_leet"):
        traits.append("uses leetspeak")
    if bf.get("preferred_separator"):
        traits.append(f"separator `{bf['preferred_separator']}`")
    if traits:
        st.markdown("**Naming style:** " + " · ".join(traits))
    for note in bf.get("style_notes", [])[:5]:
        st.markdown(f"- {note}")
    st.divider()

# ---------------------------------------------------------------------------
# Activity timeline + interests
# ---------------------------------------------------------------------------
tcol, intcol = st.columns([3, 2])
with tcol:
    st.subheader("Activity Timeline")
    temporal = profile.get("temporal", {})
    events = []
    for c in temporal.get("creation_timeline", []) or []:
        events.append({"date": c.get("date"), "label": c.get("platform", ""),
                       "kind": "account created"})
    if temporal.get("earliest_activity"):
        events.append({"date": temporal["earliest_activity"], "label": "earliest",
                       "kind": "activity"})
    if temporal.get("latest_activity"):
        events.append({"date": temporal["latest_activity"], "label": "latest",
                       "kind": "activity"})
    fig = timeline_chart(events)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No dated activity could be inferred from the evidence.")

with intcol:
    st.subheader("Interests")
    interests = profile.get("interests", {})
    primary = interests.get("primary_interest")
    tops = interests.get("top_interests", [])
    if primary:
        st.markdown(f"**Primary interest:** {primary}")
    elif tops:
        st.markdown(f"**Top areas:** {', '.join(tops)}")
    cats = interests.get("categories", [])
    if cats:
        st.dataframe(
            pd.DataFrame([
                {"category": c.get("label", c.get("category")),
                 "platforms": c.get("platform_count")}
                if isinstance(c, dict) else {"category": c, "platforms": ""}
                for c in cats
            ]),
            use_container_width=True, hide_index=True,
        )
    ts = interests.get("tech_sophistication", {})
    if ts.get("level"):
        st.markdown(f"**Tech sophistication:** `{ts['level']}`")
        st.caption(ts.get("rationale", ""))

st.divider()

# ---------------------------------------------------------------------------
# Explainable reasoning + recommended actions
# ---------------------------------------------------------------------------
rcol, accol = st.columns(2)
with rcol:
    st.subheader("Reasoning & Evidence Basis")
    reasoning = profile.get("reasoning", [])
    if not reasoning:
        st.caption("No inferences with sufficient support.")
    for r in reasoning[:10]:
        color = conf_color(r.get("confidence", "low"))
        st.markdown(
            f"<div style='border-left:3px solid {color};padding:4px 10px;margin:6px 0'>"
            f"<b>{r.get('claim', '')}</b> "
            f"<span style='color:{color};font-size:0.75rem'>"
            f"({str(r.get('confidence', '')).upper()})</span><br>"
            f"<span style='color:#8a909a;font-size:0.78rem'>"
            f"{'; '.join(str(x) for x in (r.get('evidence') or [])[:3])}</span></div>",
            unsafe_allow_html=True,
        )

with accol:
    st.subheader("Recommended Actions")
    actions = insights.get("recommended_actions", [])
    if not actions:
        st.caption("No specific actions recommended.")
    _PRIO = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    for a in actions[:12]:
        st.markdown(
            f"{_PRIO.get(a.get('priority'), '⚪')} **{a.get('action', '')}**  "
            f"<span style='color:#777;font-size:0.72rem'>"
            f"[{str(a.get('category', '')).replace('_', ' ')}]</span>",
            unsafe_allow_html=True,
        )
        st.caption(a.get("rationale", ""))

# ---------------------------------------------------------------------------
# Completeness footer
# ---------------------------------------------------------------------------
comp = profile.get("profile_completeness", {})
if comp.get("missing"):
    st.divider()
    st.caption(
        f"**Profile completeness {comp.get('score', 0)}%** — "
        f"known: {', '.join(comp.get('known', [])) or '—'}  ·  "
        f"missing: {', '.join(comp.get('missing', []))}"
    )
