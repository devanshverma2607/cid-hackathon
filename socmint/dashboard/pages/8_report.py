"""Page 6 — Report. Generate and download the signed evidence bundle."""
from __future__ import annotations

import pathlib
import sys

import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import (  # noqa: E402
    active_case_id, api_get, api_get_bytes, api_post, case_selector, require_case,
)

st.set_page_config(page_title="Report", page_icon="📦", layout="wide")
st.title("📦 Report & Evidence Bundle")
st.caption("Generate the signed JSON + PDF + SHA-256 manifest for the case.")

case_selector(sidebar=True)
case_id = active_case_id()
require_case(case_id)

status = api_get(f"/api/v1/reports/status/{case_id}")
if status is None:
    st.stop()

c1, c2, c3 = st.columns(3)
c1.metric("Evidence Units", status.get("evidence_units", 0))
c2.metric("Identity Links", status.get("identity_links", 0))
c3.metric("Ready", "yes" if status.get("ready") else "no")

# Intelligence headline — the synthesis that the bundle will embed.
intel = api_get(f"/api/v1/insights/{case_id}")
if intel:
    risk = intel.get("risk", {})
    band = risk.get("band", "LOW")
    icon = {"HIGH": "🔴", "ELEVATED": "🟠", "MODERATE": "🟡", "LOW": "🟢"}.get(band, "")
    with st.container(border=True):
        st.markdown(f"**Intelligence assessment** · {icon} **{band}** ({risk.get('score', 0)}/100)")
        st.caption(intel.get("narrative", ""))
        st.page_link("pages/6_intelligence.py", label="View full assessment →", icon="🧠")

if st.button("Generate Signed Bundle", type="primary"):
    with st.spinner("Building JSON + PDF and signing…"):
        resp = api_post(f"/api/v1/reports/generate/{case_id}", timeout=120)
    if resp is not None and resp.ok:
        result = resp.json()
        st.success("Bundle generated.")
        st.code(result.get("bundle_sha256", ""), language="text")
    elif resp is not None:
        st.error(f"Generation failed: {resp.text}")

st.subheader("Downloads")
d1, d2, d3 = st.columns(3)
for col, kind, label, mime in (
    (d1, "json", "JSON Package", "application/json"),
    (d2, "pdf", "PDF Report", "application/pdf"),
    (d3, "sha256", "SHA-256 Manifest", "text/plain"),
):
    content = api_get_bytes(f"/api/v1/reports/download/{case_id}/{kind}")
    if content:
        col.download_button(
            label, data=content, file_name=f"{case_id}_{kind}", mime=mime,
            key=f"dl_{kind}", use_container_width=True,
        )
    else:
        col.caption(f"{label}: not generated yet")
