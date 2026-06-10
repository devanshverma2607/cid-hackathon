"""Page 8 — Intelligence. The synthesised, ranked assessment of a case.

Calls the Insight Engine (`/api/v1/insights/{case_id}`) and renders a suspect
profile, exposure/risk gauge, ranked key findings, and investigative leads.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, case_selector, require_case,
)

st.set_page_config(page_title="Intelligence", page_icon="🧠", layout="wide")
st.title("🧠 Intelligence Assessment")
st.caption("The smart synthesis — profile, exposure, ranked findings, and leads.")

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

intel = api_get(f"/api/v1/insights/{case_id}")
if intel is None:
    st.stop()

risk = intel.get("risk", {})
coverage = intel.get("coverage", {})
profile = intel.get("subject_profile", {})
exposure = intel.get("exposure", {})

_BAND_COLOR = {"HIGH": "🔴", "ELEVATED": "🟠", "MODERATE": "🟡", "LOW": "🟢"}
_SEV_COLOR = {"critical": "🔴", "high": "🟠", "notable": "🟡", "info": "🔵"}

# --- top line: risk gauge + narrative ---------------------------------------
band = risk.get("band", "LOW")
c1, c2 = st.columns([1, 3])
with c1:
    st.metric("Exposure", f"{_BAND_COLOR.get(band, '')} {band}",
              f"{risk.get('score', 0)} / 100")
with c2:
    st.markdown(f"**Assessment**\n\n{intel.get('narrative', '')}")
    if risk.get("drivers"):
        st.caption("Risk drivers: " + " · ".join(risk["drivers"]))

st.divider()

# --- coverage strip ----------------------------------------------------------
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Tools run", coverage.get("tools_run", 0))
m2.metric("With hits", coverage.get("tools_with_hits", 0))
m3.metric("Empty", coverage.get("tools_empty", 0))
m4.metric("Unavailable", coverage.get("tools_unavailable", 0))
m5.metric("Evidence units", coverage.get("total_evidence_units", 0))

# --- subject profile ---------------------------------------------------------
st.subheader("Subject Profile")
ids = profile.get("identifiers", {})
pc1, pc2, pc3 = st.columns(3)
pc1.markdown("**Emails**\n\n" + ("\n".join(f"- {e}" for e in ids.get("emails", [])) or "_none_"))
pc2.markdown("**Usernames**\n\n" + ("\n".join(f"- {u}" for u in ids.get("usernames", [])) or "_none_"))
pc3.markdown("**Phones**\n\n" + ("\n".join(f"- {p}" for p in ids.get("phones", [])) or "_none_"))

confirmed = profile.get("confirmed_accounts", [])
reported = profile.get("reported_accounts", [])
st.markdown(
    f"**Digital footprint:** {len(confirmed)} confirmed + {len(reported)} reported "
    f"account(s) across **{profile.get('platform_count', 0)}** platform(s)."
)

if confirmed:
    st.markdown("**Confirmed accounts** (corroborated / first-party)")
    st.dataframe(
        pd.DataFrame([
            {
                "platform": a["platform"],
                "handle": a["handle"],
                "confidence": a["confidence"],
                "corroboration": a["corroboration"],
                "tools": ", ".join(a["tools"]),
                "url": a["url"],
            }
            for a in confirmed
        ]),
        use_container_width=True, hide_index=True,
        column_config={"url": st.column_config.LinkColumn("url")},
    )

with st.expander(f"Reported accounts (single-source, {len(reported)})", expanded=False):
    if reported:
        st.dataframe(
            pd.DataFrame([
                {
                    "platform": a["platform"],
                    "handle": a["handle"],
                    "confidence": a["confidence"],
                    "tools": ", ".join(a["tools"]),
                    "url": a["url"],
                }
                for a in reported
            ]),
            use_container_width=True, hide_index=True,
            column_config={"url": st.column_config.LinkColumn("url")},
        )
    else:
        st.caption("None.")

# --- key findings ------------------------------------------------------------
st.subheader("Key Findings")
findings = intel.get("key_findings", [])
if not findings:
    st.info("No notable findings synthesised yet — run the pipeline or collect more evidence.")
for f in findings:
    icon = _SEV_COLOR.get(f.get("severity"), "⚪")
    with st.container(border=True):
        st.markdown(f"{icon} **{f.get('title', '')}**  ·  _{f.get('category', '')}_  ·  conf {f.get('confidence', 0)}")
        st.caption(f.get("detail", ""))
        if f.get("supporting_tools"):
            st.caption("Sources: " + ", ".join(f["supporting_tools"]))

# --- exposure detail ---------------------------------------------------------
with st.expander("Exposure detail (breaches, pastes, registrations)", expanded=False):
    ec1, ec2, ec3 = st.columns(3)
    ec1.metric("Breach hits", exposure.get("breach_count", 0))
    ec2.metric("Paste/archive", exposure.get("paste_count", 0))
    ec3.metric("Registrations", exposure.get("registration_count", 0))
    for label, key in (
        ("Breach hits", "breach_hits"),
        ("Paste / archive mentions", "paste_archive_hits"),
        ("Email registrations", "email_registrations"),
    ):
        rows = exposure.get(key, [])
        if rows:
            st.markdown(f"**{label}**")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# --- investigative leads -----------------------------------------------------
st.subheader("Investigative Leads")
leads = intel.get("investigative_leads", [])
if leads:
    st.caption("New handles/values surfaced during collection — candidates to pivot on.")
    st.dataframe(
        pd.DataFrame([
            {
                "value": l["value"],
                "platform": l["platform"],
                "tools": ", ".join(l["tools"]),
                "source": l.get("source_url", ""),
            }
            for l in leads
        ]),
        use_container_width=True, hide_index=True,
        column_config={"source": st.column_config.LinkColumn("source")},
    )
else:
    st.caption("No new pivot leads beyond the seeds.")
