"""Page 0 — Login & Registration.

Landing page when no JWT exists in session state. Handles:
  - Local login (POST /api/v1/auth/token)
  - Registration (POST /api/v1/auth/register)
  - Google OAuth (redirects to /api/v1/auth/google/login; picks up the JWT
    from the ``?token=`` query param on return)

On success the JWT and user metadata are stored in ``st.session_state``
and the user is redirected to the main dashboard.
"""
from __future__ import annotations

import pathlib
import sys

import requests
import streamlit as st
import os

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from socmint_ui import get_api_base  # noqa: E402

st.set_page_config(page_title="SOCMINT — Login", page_icon="🔐", layout="centered")

# ---------------------------------------------------------------------------
# 1. Pick up a JWT from the URL (Google OAuth callback redirect)
# ---------------------------------------------------------------------------
query_params = st.query_params
token_from_url = query_params.get("token", "")

if token_from_url:
    # Validate the token by calling /auth/me
    try:
        me_resp = requests.get(
            f"{get_api_base()}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token_from_url}"},
            timeout=10,
        )
        if me_resp.status_code == 200:
            st.session_state["auth_token"] = token_from_url
            st.session_state["user"] = me_resp.json()
            # Clear the token from the URL so it's not leaked in history/logs
            st.query_params.clear()
            st.switch_page("app.py")
        else:
            st.error("The login token is invalid or expired. Please sign in again.")
            st.query_params.clear()
    except Exception:  # noqa: BLE001
        st.error("Could not validate the login token. Please sign in manually.")
        st.query_params.clear()

# Handle Google OAuth error redirect
google_error = query_params.get("error", "")
if google_error:
    st.error("Google login was denied or failed. Please try again.")
    st.query_params.clear()

# ---------------------------------------------------------------------------
# 2. Already authenticated → go straight to the main dashboard.
# ---------------------------------------------------------------------------
if st.session_state.get("auth_token"):
    st.switch_page("app.py")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div style="text-align:center;margin-bottom:2rem">
        <h1 style="margin-bottom:0">🛰️ SOCMINT</h1>
        <p style="color:#8a909a;font-size:0.95rem;margin-top:4px">
            Suspect Profiling System · Sign in to continue
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Google OAuth — prominent button above the tabs
# ---------------------------------------------------------------------------
external_api = os.getenv("API_EXTERNAL_URL", "http://localhost:8000")
google_login_url = f"{external_api}/api/v1/auth/google/login"

st.markdown(
    f"""
    <div style="text-align:center;margin-bottom:1.2rem">
        <a href="{google_login_url}" target="_self" style="
            display:inline-flex;align-items:center;gap:10px;
            padding:12px 28px;border-radius:8px;
            background:#fff;color:#3c4043;
            font-size:0.95rem;font-weight:600;
            text-decoration:none;
            border:1px solid #dadce0;
            box-shadow:0 1px 3px rgba(0,0,0,.08);
            transition:box-shadow .2s;
        " onmouseover="this.style.boxShadow='0 2px 8px rgba(0,0,0,.15)'"
          onmouseout="this.style.boxShadow='0 1px 3px rgba(0,0,0,.08)'">
            <svg width="20" height="20" viewBox="0 0 48 48">
                <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
                <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
                <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
                <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
            </svg>
            Sign in with Google
        </a>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    "<div style='text-align:center;color:#666;font-size:0.85rem;margin-bottom:0.8rem'>"
    "— or sign in with your credentials —</div>",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Tabs: Login | Register
# ---------------------------------------------------------------------------
tab_login, tab_register = st.tabs(["🔑 Login", "📝 Register"])

# ---- LOGIN ----------------------------------------------------------------
with tab_login:
    with st.form("login_form"):
        username = st.text_input("Username", key="login_user")
        password = st.text_input("Password", type="password", key="login_pass")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if submitted:
        if not username.strip() or not password.strip():
            st.error("Username and password are required.")
        else:
            try:
                resp = requests.post(
                    f"{get_api_base()}/api/v1/auth/token",
                    data={"username": username.strip(), "password": password},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    st.session_state["auth_token"] = data["access_token"]

                    # Fetch user profile with the new token.
                    me_resp = requests.get(
                        f"{get_api_base()}/api/v1/auth/me",
                        headers={"Authorization": f"Bearer {data['access_token']}"},
                        timeout=10,
                    )
                    if me_resp.status_code == 200:
                        st.session_state["user"] = me_resp.json()
                    else:
                        st.session_state["user"] = {"username": username.strip()}

                    st.success(f"Welcome back, **{username.strip()}**!")
                    st.switch_page("app.py")
                elif resp.status_code == 401:
                    st.error("Invalid username or password.")
                else:
                    detail = resp.json().get("detail", resp.text) if resp.text else "Unknown error"
                    st.error(f"Login failed ({resp.status_code}): {detail}")
            except requests.ConnectionError:
                st.error("Cannot reach the API. Is the backend running?")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Login error: {exc}")

# ---- REGISTER -------------------------------------------------------------
with tab_register:
    with st.form("register_form"):
        reg_username = st.text_input("Username", key="reg_user")
        reg_email = st.text_input("Email", key="reg_email")
        reg_full_name = st.text_input("Full name", key="reg_name")
        reg_password = st.text_input("Password (min 8 characters)", type="password", key="reg_pass")
        reg_confirm = st.text_input("Confirm password", type="password", key="reg_confirm")
        reg_submitted = st.form_submit_button("Create account", use_container_width=True)

    if reg_submitted:
        problems = []
        if not reg_username.strip():
            problems.append("Username is required.")
        if not reg_email.strip() or "@" not in reg_email:
            problems.append("A valid email is required.")
        if len(reg_password) < 8:
            problems.append("Password must be at least 8 characters.")
        if reg_password != reg_confirm:
            problems.append("Passwords do not match.")

        if problems:
            for p in problems:
                st.error(p)
        else:
            try:
                resp = requests.post(
                    f"{get_api_base()}/api/v1/auth/register",
                    json={
                        "username": reg_username.strip().lower(),
                        "email": reg_email.strip().lower(),
                        "full_name": reg_full_name.strip(),
                        "password": reg_password,
                    },
                    timeout=15,
                )
                if resp.status_code == 201:
                    st.success("Account created! Signing you in…")

                    # Auto-login after registration.
                    login_resp = requests.post(
                        f"{get_api_base()}/api/v1/auth/token",
                        data={"username": reg_username.strip().lower(), "password": reg_password},
                        timeout=15,
                    )
                    if login_resp.status_code == 200:
                        data = login_resp.json()
                        st.session_state["auth_token"] = data["access_token"]
                        st.session_state["user"] = resp.json()
                        st.switch_page("app.py")
                    else:
                        st.info("Account created. Please switch to the Login tab to sign in.")
                elif resp.status_code == 409:
                    st.error(resp.json().get("detail", "Username or email already registered."))
                else:
                    detail = resp.json().get("detail", resp.text) if resp.text else "Unknown error"
                    st.error(f"Registration failed ({resp.status_code}): {detail}")
            except requests.ConnectionError:
                st.error("Cannot reach the API. Is the backend running?")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Registration error: {exc}")

# ---------------------------------------------------------------------------
st.divider()
st.caption("SOCMINT Suspect Profiling System · Lawful OSINT intelligence")

