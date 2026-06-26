"""SOCMINT Analyst Dashboard — Streamlit multi-page entry point.

Run: streamlit run dashboard/app.py
Pages live in dashboard/pages/ and are auto-discovered by Streamlit.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pandas as pd
import requests
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, case_selector, fetch_cases, fmt_dt, get_api_base,
    logout, require_auth, short_id,
)

st.set_page_config(page_title="SOCMINT Suspect Profiling", page_icon="🛰️", layout="wide")
st.session_state.setdefault("api_base_url", os.environ.get("API_BASE_URL", "http://api:8000"))

# --- Pick up JWT from URL (Google OAuth callback lands here) ----------------
_token_from_url = st.query_params.get("token", "")
if _token_from_url and not st.session_state.get("auth_token"):
    try:
        _me = requests.get(
            f"{get_api_base()}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {_token_from_url}"},
            timeout=10,
        )
        if _me.status_code == 200:
            st.session_state["auth_token"] = _token_from_url
            st.session_state["user"] = _me.json()
    except Exception:  # noqa: BLE001
        pass
    st.query_params.clear()
# --- Auth guard — redirect to login if no token ----------------------------
require_auth()

def fetch_health() -> dict:
    """Poll the API health endpoint; never raise to the UI."""
    try:
        resp = requests.get(f"{get_api_base()}/api/v1/health", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "services": {"api": str(exc)}}


st.title("🛰️ SOCMINT — Suspect Profiling System")
st.caption("Lawful OSINT pipeline · discover → correlate → preserve → report")

with st.sidebar:
    # --- User info & logout ------------------------------------------------
    user = st.session_state.get("user", {})
    user_display = user.get("full_name") or user.get("username", "Analyst")
    user_role = user.get("role", "analyst")
    st.markdown(
        f"👤 **{user_display}**  \n"
        f"<span style='color:#8a909a;font-size:0.8rem'>{user_role}</span>",
        unsafe_allow_html=True,
    )
    if st.button("🚪 Logout", use_container_width=True):
        logout()
    st.divider()

    st.header("System Status")
    health = fetch_health()
    badge = {"healthy": "🟢", "degraded": "🟡", "unreachable": "🔴"}.get(
        health.get("status"), "⚪"
    )
    st.markdown(f"**API:** {badge} {health.get('status')}")
    for service, state in health.get("services", {}).items():
        icon = "🟢" if state == "up" else "🔴"
        st.markdown(f"{icon} **{service}** — {state}")
    if st.button("Refresh"):
        fetch_cases.clear()
        st.rerun()
    st.divider()
    st.header("Active Case")
    case_selector(sidebar=True)

# --- portfolio stats -----------------------------------------------------
cases = fetch_cases()
total_ev = sum(c.get("evidence_count", 0) for c in cases)
total_ln = sum(c.get("link_count", 0) for c in cases)
s1, s2, s3 = st.columns(3)
s1.metric("Cases", len(cases))
s2.metric("Evidence Units", total_ev)
s3.metric("Identity Links", total_ln)

# --- active case card ----------------------------------------------------
_BAND_ICON = {"HIGH": "🔴", "ELEVATED": "🟠", "MODERATE": "🟡", "LOW": "🟢"}
cid = active_case_id()
if cid:
    match = next((c for c in cases if c["case_id"] == cid), None)
    with st.container(border=True):
        st.markdown(f"**Active case** · `{cid}`")
        if match:
            st.write(
                f"{match.get('seed_type')} · **{match.get('seed_value')}**  —  "
                f"{match.get('evidence_count', 0)} findings · "
                f"{match.get('link_count', 0)} links · "
                f"{match.get('target_category', '—')} · {match.get('jurisdiction', '—')}"
            )
        # Smart-engine snapshot: risk band + headline finding.
        if match and match.get("evidence_count", 0):
            intel = api_get(f"/api/v1/insights/{cid}")
            if intel:
                risk = intel.get("risk", {})
                band = risk.get("band", "LOW")
                findings = intel.get("key_findings", [])
                profile = intel.get("subject_profile", {})
                ic1, ic2, ic3 = st.columns(3)
                ic1.metric(
                    "Exposure",
                    f"{_BAND_ICON.get(band, '')} {band}",
                    f"{risk.get('score', 0)} / 100",
                )
                ic2.metric("Confirmed accounts",
                           len(profile.get("confirmed_accounts", [])))
                ic3.metric("Platforms", profile.get("platform_count", 0))
                if findings:
                    top = findings[0]
                    st.caption(
                        f"Headline: **{top.get('title', '')}** "
                        f"(conf {top.get('confidence', 0)})"
                    )
                st.page_link(
                    "pages/6_intelligence.py",
                    label="Open full Intelligence Assessment →",
                    icon="🧠",
                )

# --- recent cases --------------------------------------------------------
st.subheader("Recent cases")
if cases:
    df = pd.DataFrame([
        {
            "seed": f"{c.get('seed_type')} · {c.get('seed_value')}",
            "category": c.get("target_category"),
            "jurisdiction": c.get("jurisdiction"),
            "analyst": c.get("analyst_id"),
            "evidence": c.get("evidence_count", 0),
            "links": c.get("link_count", 0),
            "created": fmt_dt(c.get("created_at")),
            "case_id": short_id(c.get("case_id", ""), 8),
        }
        for c in cases
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption("Pick a case in the sidebar to make it active across all pages.")
else:
    st.info("No cases yet. Open one in **Case Intake** to launch the pipeline.")

st.divider()
st.markdown(
    """
### Workflow
1. **Case Intake** — open a lawful case (legal gate enforced).
2. **Pipeline Status** — watch Tier 1-4 tools execute (enable auto-refresh).
3. **Evidence Explorer** — browse every preserved finding, snapshot, and enrichment.
4. **Identity Graph** — explore SAME_AS correlations and discovery pivots.
5. **Review Queue** — adjudicate MEDIUM-confidence links.
6. **Intelligence** — the smart synthesis: risk, ranked findings, and leads.
7. **Persona Resolution** — resolve discovered identities into personas.
8. **Report** — generate the signed evidence bundle (JSON + PDF + SHA-256).
"""
)
