"""Page 1 — Case Intake. Opens a lawful case through the legal gate."""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import api_post, fetch_cases  # noqa: E402

st.set_page_config(page_title="Case Intake", page_icon="📋", layout="wide")
st.title("📋 Case Intake")
st.caption("All fields are mandatory. Supervisor approval is legally required.")

MIN_PURPOSE_CHARS = 20

# Friendly labels for fields the backend may reject.
_FIELD_LABELS = {
    "authority_id": "Authority ID",
    "agency_id": "Agency ID",
    "analyst_id": "Analyst ID",
    "jurisdiction": "Jurisdiction",
    "target_category": "Target Category",
    "seed_type": "Seed Type",
    "seed_value": "Seed Value",
    "retention_period": "Retention Period",
    "purpose_statement": "Purpose Statement",
    "supervisor_approval": "Supervisor approval",
}


def _humanise_errors(body) -> list[str]:
    """Turn a FastAPI error body into clean, human-readable messages."""
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, str):
        return [detail]
    messages = []
    for item in detail or []:
        if not isinstance(item, dict):
            messages.append(str(item))
            continue
        loc = [p for p in item.get("loc", []) if p != "body"]
        field = _FIELD_LABELS.get(loc[-1], loc[-1]) if loc else None
        msg = str(item.get("msg", "Invalid value")).replace("Value error, ", "")
        # Avoid stuttering when the message already names the field.
        if field and field.lower().replace(" ", "_") not in msg.lower().replace(" ", "_"):
            messages.append(f"**{field}** — {msg}")
        else:
            messages.append(msg)
    return messages or ["The legal gate rejected the case."]

with st.form("case_intake"):
    col1, col2 = st.columns(2)
    with col1:
        authority_id = st.text_input("Authority ID")
        agency_id = st.text_input("Agency ID")
        analyst_id = st.text_input("Analyst ID", value=st.session_state.get("analyst_id", ""))
        jurisdiction = st.text_input("Jurisdiction")
        target_category = st.selectbox(
            "Target Category", ["cybercrime", "fraud", "harassment", "research"]
        )
    with col2:
        seed_type = st.selectbox(
            "Primary Seed Type", ["username", "email", "phone", "profile_url"]
        )
        seed_value = st.text_input("Primary Seed Value")
        retention_period = st.number_input("Retention Period (days)", min_value=1, value=90)
        supervisor_approval = st.checkbox("Supervisor approval obtained")

    st.markdown("**Additional identifiers** (optional) — same subject, mixed types.")
    additional_df = st.data_editor(
        pd.DataFrame(columns=["seed_type", "seed_value"]),
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key="additional_seeds_editor",
        column_config={
            "seed_type": st.column_config.SelectboxColumn(
                "Type",
                options=["username", "email", "phone", "profile_url"],
                required=True,
            ),
            "seed_value": st.column_config.TextColumn("Value", required=True),
        },
    )

    purpose_statement = st.text_area(
        f"Purpose Statement (min. {MIN_PURPOSE_CHARS} characters)",
        help="Lawful basis and investigative purpose for this collection.",
    )
    submitted = st.form_submit_button("Open Case & Launch Pipeline", type="primary")

if submitted:
    # Validate locally first so analysts get a clean message, not a raw 422.
    problems = []
    required = {
        "Authority ID": authority_id, "Agency ID": agency_id,
        "Analyst ID": analyst_id, "Jurisdiction": jurisdiction,
        "Seed Value": seed_value,
    }
    for label, value in required.items():
        if not (value or "").strip():
            problems.append(f"**{label}** is required.")
    if len(purpose_statement.strip()) < MIN_PURPOSE_CHARS:
        problems.append(
            f"**Purpose Statement** must be at least {MIN_PURPOSE_CHARS} characters "
            f"(currently {len(purpose_statement.strip())})."
        )
    if not supervisor_approval:
        problems.append("**Supervisor approval** is legally required to open a case.")

    if problems:
        st.error("Please fix the following before submitting:")
        st.markdown("\n".join(f"- {p}" for p in problems))
        st.stop()

    additional_seeds = []
    for _, row in additional_df.iterrows():
        s_type = str(row.get("seed_type", "") or "").strip()
        s_value = str(row.get("seed_value", "") or "").strip()
        if s_type and s_value:
            additional_seeds.append({"seed_type": s_type, "seed_value": s_value})

    payload = {
        "authority_id": authority_id.strip(),
        "agency_id": agency_id.strip(),
        "analyst_id": analyst_id.strip(),
        "supervisor_approval": supervisor_approval,
        "purpose_statement": purpose_statement.strip(),
        "target_category": target_category,
        "jurisdiction": jurisdiction.strip(),
        "retention_period": int(retention_period),
        "seed_type": seed_type,
        "seed_value": seed_value.strip(),
        "additional_seeds": additional_seeds,
    }
    resp = api_post("/api/v1/cases/create", json=payload)
    if resp is None:
        st.stop()
    if resp.status_code in (200, 201):
        data = resp.json()
        case_id = data.get("case_id")
        st.session_state["active_case_id"] = case_id
        if analyst_id:
            st.session_state["analyst_id"] = analyst_id.strip()
        fetch_cases.clear()
        st.success(f"Case opened: {case_id}")
        st.json(data)
        st.info("Now open **Pipeline Status** to watch the tools run.")
    else:
        st.error(f"Legal gate rejected the case ({resp.status_code}).")
        try:
            for msg in _humanise_errors(resp.json()):
                st.markdown(f"- {msg}")
        except Exception:  # noqa: BLE001
            st.code(resp.text)
