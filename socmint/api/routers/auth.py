"""/api/v1/auth endpoints — user registration, login, and profile.
Integrates with the SOCMINT Provenance Service to append ``USER_REGISTERED``
and ``USER_LOGIN`` events to the append-only ``audit_log`` table, keeping the
authentication lifecycle fully traceable alongside investigation events.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import text
from sqlalchemy.orm import Session
from api.db.postgres import get_db, get_user_by_email, get_user_by_username
from api.models.user import Token, UserCreate, UserOut
from api.services.auth import (
    create_access_token,
    get_current_user,
    get_password_hash,
    require_role,
    verify_password,
)
from api.services.provenance import ProvenanceService
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
provenance = ProvenanceService()
# ---------------------------------------------------------------------------
# POST /register — public (no auth required)
# ---------------------------------------------------------------------------
@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, session: Session = Depends(get_db)) -> UserOut:
    """Create a new user account.
    Checks for duplicate username/email (→ 409) and writes a
    ``USER_REGISTERED`` audit event via the Provenance Service.
    """
    # --- duplicate checks ---------------------------------------------------
    if get_user_by_username(session, payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already registered",
        )
    if get_user_by_email(session, payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )
    # --- persist ------------------------------------------------------------
    hashed_password = get_password_hash(payload.password)
    result = session.execute(
        text(
            """
            INSERT INTO users (username, email, hashed_password, full_name, role)
            VALUES (:username, :email, :hashed_password, :full_name, :role)
            RETURNING user_id, username, email, full_name, role, is_active, created_at
            """
        ),
        {
            "username": payload.username,
            "email": payload.email,
            "hashed_password": hashed_password,
            "full_name": payload.full_name,
            "role": payload.role,
        },
    )
    row = result.mappings().first()
    session.commit()
    user_out = UserOut(**dict(row))
    # --- audit --------------------------------------------------------------
    provenance.log_audit_event(
        case_id=None,
        run_id=None,
        event_type="USER_REGISTERED",
        actor_id=user_out.username,
        metadata={
            "user_id": str(user_out.user_id),
            "email": user_out.email,
            "role": user_out.role,
        },
        session=session,
    )
    logger.info("User registered: %s (%s)", user_out.username, user_out.email)
    return user_out
# ---------------------------------------------------------------------------
# POST /token — OAuth2-compatible login (public, no auth required)
# ---------------------------------------------------------------------------
@router.post("/token", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_db),
) -> Token:
    """Authenticate via username + password and return a JWT bearer token.
    Uses the standard ``OAuth2PasswordRequestForm`` so Swagger UI's
    **Authorize** dialog works out of the box.  Writes a ``USER_LOGIN``
    audit event on success.
    """
    # OAuth2PasswordRequestForm sends the identifier in `username` field.
    user_row = get_user_by_username(session, form_data.username)
    if user_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_password(form_data.password, user_row["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user_row.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # --- issue token --------------------------------------------------------
    access_token = create_access_token(
        data={
            "user_id": str(user_row["user_id"]),
            "username": user_row["username"],
            "email": user_row["email"],
            "role": user_row["role"],
        }
    )
    # --- audit --------------------------------------------------------------
    provenance.log_audit_event(
        case_id=None,
        run_id=None,
        event_type="USER_LOGIN",
        actor_id=user_row["username"],
        metadata={
            "user_id": str(user_row["user_id"]),
            "email": user_row["email"],
            "role": user_row["role"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        session=session,
    )
    logger.info("User logged in: %s", user_row["username"])
    return Token(access_token=access_token, token_type="bearer")
# ---------------------------------------------------------------------------
# GET /me — returns the current user's profile (requires auth)
# ---------------------------------------------------------------------------
@router.get("/me", response_model=UserOut)
def get_me(current_user: UserOut = Depends(get_current_user)) -> UserOut:
    """Return the authenticated user's profile."""
    return current_user
# ---------------------------------------------------------------------------
# GET /users — admin-only user listing
# ---------------------------------------------------------------------------
@router.get("/users", response_model=list[UserOut])
def list_users(
    _admin: UserOut = Depends(require_role("admin")),
    session: Session = Depends(get_db),
) -> list[UserOut]:
    """List all registered users. Restricted to admin role."""
    rows = session.execute(
        text(
            "SELECT user_id, username, email, full_name, role, is_active, created_at "
            "FROM users ORDER BY created_at DESC"
        )
    ).mappings().all()
    return [UserOut(**dict(r)) for r in rows]
# ---------------------------------------------------------------------------
# POST /users/{user_id}/deactivate — admin-only account deactivation
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(
    user_id: str,
    admin: UserOut = Depends(require_role("admin")),
    session: Session = Depends(get_db),
) -> UserOut:
    """Deactivate a user account. Restricted to admin role."""
    row = session.execute(
        text(
            "UPDATE users SET is_active = FALSE WHERE user_id = :user_id "
            "RETURNING user_id, username, email, full_name, role, is_active, created_at"
        ),
        {"user_id": user_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    session.commit()
    provenance.log_audit_event(
        case_id=None,
        run_id=None,
        event_type="USER_DEACTIVATED",
        actor_id=admin.username,
        metadata={
            "target_user_id": user_id,
            "target_username": row["username"],
        },
        session=session,
    )
    return UserOut(**dict(row))
