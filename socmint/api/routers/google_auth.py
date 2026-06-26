"""/api/v1/auth/google — Google OAuth 2.0 login flow.

Flow:
  1. ``GET /auth/google/login``   → redirect the browser to Google's consent screen.
  2. Google redirects back to ``GET /auth/google/callback`` with ``?code=&state=``.
  3. Callback exchanges the code for tokens, fetches user info, creates or links
     the SOCMINT user, mints a JWT, and redirects to the Streamlit dashboard
     with ``?token=<jwt>`` so the frontend can pick it up from the URL.

CSRF protection is handled via a ``state`` parameter stored in a signed JWT
(short-lived, 10-minute expiry) so it survives the redirect round-trip without
server-side session storage.
"""
from __future__ import annotations

import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.config import get_settings
from api.db.postgres import get_db, get_user_by_email
from api.services.auth import create_access_token, get_password_hash
from api.services.provenance import ProvenanceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth/google", tags=["google-auth"])

provenance = ProvenanceService()

# Google's OpenID Connect discovery endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


# ---------------------------------------------------------------------------
# Helpers: CSRF state token (signed JWT, 10-min expiry)
# ---------------------------------------------------------------------------

def _create_state_token() -> tuple[str, str]:
    """Mint a short-lived JWT encoding a random nonce as CSRF state."""
    settings = get_settings()
    nonce = secrets.token_urlsafe(32)
    token = jwt.encode(
        {"nonce": nonce, "purpose": "google_oauth_state"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return token, nonce


def _verify_state_token(state: str) -> bool:
    """Validate that the state token is a well-formed JWT we signed."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            state,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload.get("purpose") == "google_oauth_state"
    except JWTError:
        return False


# ---------------------------------------------------------------------------
# GET /auth/google/login — redirect to Google consent screen
# ---------------------------------------------------------------------------

@router.get("/login")
def google_login():
    """Build the Google authorization URL and redirect the user."""
    settings = get_settings()

    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google OAuth is not configured (GOOGLE_CLIENT_ID is empty).",
        )

    state_token, _ = _create_state_token()

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "state": state_token,
        "prompt": "select_account",
    }
    url = str(httpx.URL(GOOGLE_AUTH_URL, params=params))
    return RedirectResponse(url)


# ---------------------------------------------------------------------------
# GET /auth/google/callback — exchange code, create/link user, redirect
# ---------------------------------------------------------------------------

@router.get("/callback")
def google_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    session: Session = Depends(get_db),
):
    """Handle the OAuth callback from Google.

    1. Verify the ``state`` CSRF token.
    2. Exchange the ``code`` for an access token.
    3. Fetch user info (email, name, picture).
    4. Find-or-create the SOCMINT user.
    5. Mint a JWT and redirect to the Streamlit dashboard.
    """
    settings = get_settings()

    # --- error from Google (user denied, etc.) ---
    if error:
        logger.warning("Google OAuth error: %s", error)
        return RedirectResponse(
            f"{settings.frontend_url}/pages/0_login?error=google_denied"
        )

    # --- CSRF check ---
    if not state or not _verify_state_token(state):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired OAuth state parameter (CSRF check failed).",
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing authorization code from Google.",
        )

    # --- exchange code for tokens ---
    try:
        token_resp = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15.0,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except Exception as exc:
        logger.error("Google token exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to exchange Google auth code: {exc}",
        ) from exc

    access_token_google = token_data.get("access_token")
    if not access_token_google:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google did not return an access token.",
        )

    # --- fetch user info ---
    try:
        userinfo_resp = httpx.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token_google}"},
            timeout=10.0,
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    except Exception as exc:
        logger.error("Google userinfo fetch failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch Google user info: {exc}",
        ) from exc

    google_email = userinfo.get("email", "").lower().strip()
    google_name = userinfo.get("name", "")
    google_sub = userinfo.get("sub", "")  # Google's unique user ID

    if not google_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account does not have an email address.",
        )

    # --- find or create the SOCMINT user ---
    existing = get_user_by_email(session, google_email)

    if existing:
        # Link google_id if not already set.
        if not existing.get("google_id") and google_sub:
            session.execute(
                text("UPDATE users SET google_id = :gid WHERE user_id = :uid"),
                {"gid": google_sub, "uid": str(existing["user_id"])},
            )
            session.commit()

        user_row = existing
        event_type = "USER_LOGIN"
    else:
        # Create a new user with a random password (they'll always login via Google).
        random_password = secrets.token_urlsafe(32)
        hashed = get_password_hash(random_password)
        # Derive username from email (before @), ensure uniqueness.
        base_username = google_email.split("@")[0].lower().replace(".", "_")
        username = base_username
        suffix = 1
        while True:
            check = session.execute(
                text("SELECT 1 FROM users WHERE username = :u"),
                {"u": username},
            ).first()
            if not check:
                break
            username = f"{base_username}_{suffix}"
            suffix += 1

        result = session.execute(
            text(
                """
                INSERT INTO users (username, email, hashed_password, full_name, role, google_id)
                VALUES (:username, :email, :hashed_password, :full_name, 'analyst', :google_id)
                RETURNING user_id, username, email, full_name, role, is_active, created_at
                """
            ),
            {
                "username": username,
                "email": google_email,
                "hashed_password": hashed,
                "full_name": google_name,
                "google_id": google_sub or None,
            },
        )
        user_row = dict(result.mappings().first())
        session.commit()
        event_type = "USER_REGISTERED"

    # --- audit ---
    provenance.log_audit_event(
        case_id=None,
        run_id=None,
        event_type=event_type,
        actor_id=user_row.get("username", google_email),
        metadata={
            "user_id": str(user_row.get("user_id", "")),
            "email": google_email,
            "provider": "google",
            "google_sub": google_sub,
        },
        session=session,
    )

    # --- mint JWT ---
    jwt_token = create_access_token(
        data={
            "user_id": str(user_row["user_id"]),
            "username": user_row["username"],
            "email": user_row["email"],
            "role": user_row["role"],
        }
    )

    # --- redirect to Streamlit dashboard with token in URL ---
    redirect_url = f"{settings.frontend_url}?token={jwt_token}"
    logger.info("Google OAuth: %s → %s", event_type, user_row.get("username"))
    return RedirectResponse(redirect_url)
