"""Shared helpers for the SOCMINT Streamlit dashboard.

Centralises the API client, the case picker, and small formatting utilities so
every page talks to the FastAPI backend the same way and the analyst never has
to copy a Case ID by hand.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests
import streamlit as st

DEFAULT_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    """Build the Authorization header from session state, or empty dict."""
    token = st.session_state.get("auth_token")
    return {"Authorization": f"Bearer {token}"} if token else {}
def require_auth() -> None:
    """Guard: redirect to the login page if no JWT is stored.
    Call this as the first line after imports in every protected page.
    """
    if not st.session_state.get("auth_token"):
        st.switch_page("pages/0_login.py")
def logout() -> None:
    """Clear auth state and redirect to login."""
    for key in ("auth_token", "user"):
        st.session_state.pop(key, None)
    st.switch_page("pages/0_login.py")

# ---------------------------------------------------------------------------
# API client (auth-aware)
# ---------------------------------------------------------------------------

def get_api_base() -> str:
    """Resolve the API base URL (session override -> env -> in-cluster default)."""
    return st.session_state.get("api_base_url") or os.environ.get(
        "API_BASE_URL", "http://api:8000"
    )


def api_get(path: str, params: Optional[dict] = None, *, timeout: int = DEFAULT_TIMEOUT):
    """GET JSON from the API. Shows an error and returns None on failure."""
    try:
        resp = requests.get(
            f"{get_api_base()}{path}",
            params=params,
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"API GET {path} failed: {exc}")
        return None


def api_get_bytes(path: str, *, timeout: int = DEFAULT_TIMEOUT) -> Optional[bytes]:
    """GET raw bytes (e.g. a preserved screenshot). Returns None on failure."""
    try:
        resp = requests.get(
            f"{get_api_base()}{path}",
            headers=_auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.content
    except Exception:  # noqa: BLE001
        return None


def api_post(path: str, json: Optional[dict] = None, *, timeout: int = DEFAULT_TIMEOUT):
    """POST JSON to the API. Returns the Response, or None on a connection error."""
    try:
        return requests.post(
            f"{get_api_base()}{path}",
            json=json,
            headers=_auth_headers(),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"API POST {path} failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Cases + picker
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5, show_spinner=False)
def fetch_cases() -> list[dict]:
    """Return all cases (newest first) for the picker. Empty list on failure."""
    try:
        resp = requests.get(
            f"{get_api_base()}/api/v1/cases",
            headers=_auth_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("cases", [])
    except Exception:  # noqa: BLE001
        return []


def active_case_id() -> str:
    """Currently selected case id (empty string if none)."""
    return st.session_state.get("active_case_id", "")


def case_label(case: dict) -> str:
    """Human-friendly label for a case in the picker."""
    seed = f"{case.get('seed_type', '?')} · {case.get('seed_value', '?')}"
    ev = case.get("evidence_count", 0)
    ln = case.get("link_count", 0)
    return f"{seed}  ·  {short_id(case.get('case_id', ''))}  ·  ev={ev} ln={ln}"


def case_selector(label: str = "Active case", *, sidebar: bool = False) -> str:
    """Render the case picker and return the chosen case id.

    The active case lives in the *plain* session-state key ``active_case_id``
    (deliberately NOT the selectbox's widget key). Streamlit clears widget-key
    state when you navigate between pages, so binding the picker directly to
    ``active_case_id`` made it forget the manual selection and snap back to the
    newest case on every tab change. Keeping it in a plain key — and seeding a
    keyless selectbox from it via ``index`` each run — survives navigation,
    while case intake and the manual-paste fallback can still set it freely.
    """
    container = st.sidebar if sidebar else st
    cases = fetch_cases()
    current = st.session_state.get("active_case_id", "")

    ids = [c["case_id"] for c in cases]
    labels = {c["case_id"]: case_label(c) for c in cases}
    if current and current not in ids:
        ids.insert(0, current)
        labels[current] = f"external · {short_id(current)}"

    # Manual paste runs before the selectbox so it can change the active case.
    with container.expander("Paste an external Case ID"):
        manual = st.text_input("Case ID", key="case_manual")
        if st.button("Load", key="case_load") and manual.strip():
            st.session_state["active_case_id"] = manual.strip()
            fetch_cases.clear()
            st.rerun()

    if not ids:
        container.caption("No cases yet — open one in **Case Intake**.")
        return ""

    # Default to the newest case only when nothing valid is selected yet.
    if current not in ids:
        current = ids[0]
        st.session_state["active_case_id"] = current

    chosen = container.selectbox(
        label, ids, index=ids.index(current),
        format_func=lambda cid: labels.get(cid, cid),
    )
    if chosen != st.session_state.get("active_case_id"):
        st.session_state["active_case_id"] = chosen
    return chosen


def require_case(case_id: str) -> None:
    """Stop the page with a friendly hint if no case is selected."""
    if not case_id:
        st.info("Select or create a case to continue.")
        st.stop()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

TIER_COLORS = {
    "HIGH": "#2ecc71", "MEDIUM": "#f1c40f", "LOW": "#e74c3c", "PIVOT": "#9b59b6",
}
KIND_COLORS = {
    "account": "#3498db", "source": "#e67e22", "email": "#1abc9c",
    "phone": "#e84393", "username": "#6c5ce7", "domain": "#f9ca24",
}
STATUS_ICONS = {"done": "🟢", "skipped": "🟡", "pending": "⚪", "running": "🔵"}


def short_id(value: str, length: int = 8) -> str:
    """First N characters of an id (for compact display)."""
    return (value or "")[:length]


def status_icon(status: str) -> str:
    return STATUS_ICONS.get(status, "⚪")


def tier_color(tier: str) -> str:
    return TIER_COLORS.get((tier or "").upper(), "#888888")


def kind_color(kind: str) -> str:
    return KIND_COLORS.get((kind or "").lower(), "#888888")


def tier_badge(tier: str) -> str:
    """Coloured HTML pill for a confidence tier."""
    color = tier_color(tier)
    return (
        f"<span style='background:{color};color:#0b0b0b;padding:2px 8px;"
        f"border-radius:10px;font-size:0.75rem;font-weight:600'>{tier}</span>"
    )


def fmt_dt(value: Any) -> str:
    """Trim an ISO timestamp to minute precision for display."""
    if not value:
        return "—"
    return str(value).replace("T", " ")[:16]


# ---------------------------------------------------------------------------
# Visual helpers (gauges, bars, timelines)
# ---------------------------------------------------------------------------

CONF_COLORS = {
    "high": "#2ecc71", "medium": "#f1c40f", "low": "#e67e22", "very_low": "#e74c3c",
}


def score_color(score: float) -> str:
    """Green → amber → red ramp for a 0–100 score."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "#888888"
    if s >= 70:
        return "#e74c3c"
    if s >= 40:
        return "#f1c40f"
    if s >= 15:
        return "#3498db"
    return "#2ecc71"


def _conf_word(level) -> str:
    """Normalise a confidence value (word OR 0..1 / 0..100 number) to a word."""
    if isinstance(level, (int, float)):
        frac = float(level)
        frac = frac / 100.0 if frac > 1 else frac
        frac = max(0.0, min(1.0, frac))
        if frac >= 0.75:
            return "high"
        if frac >= 0.5:
            return "medium"
        if frac >= 0.3:
            return "low"
        return "very_low"
    return (level or "").lower().replace(" ", "_")


def conf_color(level) -> str:
    """Colour for a confidence level word OR numeric confidence (0..1 / 0..100)."""
    return CONF_COLORS.get(_conf_word(level), "#888888")


def risk_gauge(score: float, band: str = "", title: str = "Exposure", height: int = 220):
    """A plotly gauge for a 0–100 score. Returns a Figure (caller does st.plotly_chart)."""
    import plotly.graph_objects as go

    color = score_color(score)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=float(score or 0),
        number={"font": {"size": 34, "color": color}, "suffix": "/100"},
        title={"text": f"{title}<br><span style='font-size:0.8em;color:{color}'>"
                       f"{(band or '').upper()}</span>", "font": {"size": 15}},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#666"},
            "bar": {"color": color, "thickness": 0.32},
            "borderwidth": 0,
            "steps": [
                {"range": [0, 15], "color": "rgba(46,204,113,0.18)"},
                {"range": [15, 40], "color": "rgba(52,152,219,0.18)"},
                {"range": [40, 70], "color": "rgba(241,196,15,0.20)"},
                {"range": [70, 100], "color": "rgba(231,76,60,0.20)"},
            ],
            "threshold": {
                "line": {"color": color, "width": 3},
                "thickness": 0.78, "value": float(score or 0),
            },
        },
    ))
    fig.update_layout(
        height=height, margin=dict(l=20, r=20, t=50, b=10),
        paper_bgcolor="rgba(0,0,0,0)", font={"color": "#ccc"},
    )
    return fig


def conf_bar(label: str, level, *, value: str = "") -> str:
    """HTML row: a label, a confidence-coloured fill bar, and an optional value.

    ``level`` may be a word (high/medium/low/very_low) or a numeric confidence
    (0..1 or 0..100); the bar width maps to it. Render with
    ``st.markdown(conf_bar(...), unsafe_allow_html=True)``.
    """
    widths = {"high": 100, "medium": 66, "low": 40, "very_low": 20}
    if isinstance(level, (int, float)):
        frac = float(level)
        frac = frac / 100.0 if frac > 1 else frac
        pct = round(max(0.0, min(1.0, frac)) * 100)
    else:
        pct = widths.get((level or "").lower().replace(" ", "_"), 30)
    word = _conf_word(level)
    color = conf_color(level)
    val_html = (
        f"<span style='color:#eee;font-weight:600'>{value}</span>" if value else ""
    )
    return (
        f"<div style='margin:6px 0'>"
        f"<div style='display:flex;justify-content:space-between;font-size:0.8rem;"
        f"color:#aaa;margin-bottom:2px'><span>{label}</span>{val_html}</div>"
        f"<div style='background:#222;border-radius:6px;height:9px;overflow:hidden'>"
        f"<div style='width:{pct}%;background:{color};height:100%;"
        f"border-radius:6px'></div></div>"
        f"<div style='font-size:0.68rem;color:{color};text-align:right'>"
        f"{word.upper().replace('_', ' ')}</div></div>"
    )


def metric_card(label: str, value: Any, *, sub: str = "", color: str = "#3498db") -> str:
    """A compact stat card (HTML). Render with unsafe_allow_html=True."""
    sub_html = f"<div style='font-size:0.7rem;color:#888'>{sub}</div>" if sub else ""
    return (
        f"<div style='background:#16181d;border:1px solid #262a31;border-left:3px solid "
        f"{color};border-radius:8px;padding:10px 14px'>"
        f"<div style='font-size:0.72rem;color:#8a909a;text-transform:uppercase;"
        f"letter-spacing:0.05em'>{label}</div>"
        f"<div style='font-size:1.5rem;font-weight:700;color:#f0f0f0;line-height:1.2'>"
        f"{value}</div>{sub_html}</div>"
    )


def timeline_chart(events: list[dict], *, height: int = 240):
    """Scatter timeline of dated events. ``events`` = [{date, label, kind}].

    Returns a plotly Figure or None when there is nothing dated to show.
    """
    import plotly.express as px

    rows = [e for e in (events or []) if e.get("date")]
    if not rows:
        return None
    fig = px.scatter(
        rows, x="date", y=[e.get("kind", "event") for e in rows],
        text=[e.get("label", "") for e in rows],
        color=[e.get("kind", "event") for e in rows],
    )
    fig.update_traces(marker=dict(size=13, line=dict(width=1, color="#111")),
                      textposition="top center")
    fig.update_layout(
        height=height, showlegend=False,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#bbb"}, yaxis_title="", xaxis_title="",
    )
    return fig

