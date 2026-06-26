"""Authentication service — password hashing, JWT management, FastAPI dependency.

Uses ``passlib[bcrypt]`` for password hashing and ``python-jose`` for JWT
signing/verification.  All secrets (``JWT_SECRET_KEY``, ``JWT_ALGORITHM``,
``ACCESS_TOKEN_EXPIRE_MINUTES``) are pulled from the centralised
``get_settings()`` singleton so they flow through from ``.env`` like every
other credential in the stack.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import text
from sqlalchemy.orm import Session
from api.config import get_settings
from api.db.postgres import get_db, get_user_by_username
from api.models.user import TokenData, UserOut
logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
def get_password_hash(plain_password: str) -> str:
    """Return the bcrypt hash of *plain_password*."""
    return _pwd_context.hash(plain_password)
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return ``True`` if *plain_password* matches *hashed_password*."""
    return _pwd_context.verify(plain_password, hashed_password)
# ---------------------------------------------------------------------------
# JWT creation and decoding
# ---------------------------------------------------------------------------
def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Sign a JWT containing *data* with the configured secret and algorithm.
    Expiry defaults to ``ACCESS_TOKEN_EXPIRE_MINUTES`` from the environment
    when *expires_delta* is not supplied.
    """
    settings = get_settings()
    to_encode = data.copy()
    if expires_delta is not None:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.access_token_expire_minutes
        )
    to_encode["exp"] = expire
    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
def decode_access_token(token: str) -> TokenData:
    """Decode and validate a JWT, returning the embedded :class:`TokenData`.
    Raises ``HTTPException(401)`` on any failure (expired, malformed,
    missing claims).
    """
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str | None = payload.get("user_id")
        username: str | None = payload.get("username")
        email: str | None = payload.get("email")
        role: str | None = payload.get("role")
        if user_id is None or username is None:
            raise credentials_exception
        return TokenData(
            user_id=user_id,
            username=username,
            email=email or "",
            role=role or "analyst",
        )
    except JWTError:
        raise credentials_exception
# ---------------------------------------------------------------------------
# FastAPI dependency — extract + validate the current user
# ---------------------------------------------------------------------------
# tokenUrl points at the login endpoint so Swagger's "Authorize" dialog works.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")
def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_db),
) -> UserOut:
    """FastAPI dependency: resolve the bearer token to a verified user.
    Steps:
      1. Decode the JWT → ``TokenData``.
      2. Load the user row from PostgreSQL by username.
      3. Verify the user exists and ``is_active`` is ``True``.
    Raises ``HTTPException(401)`` if any step fails.
    """
    token_data = decode_access_token(token)
    user_row = get_user_by_username(session, token_data.username)
    if user_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user_row.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return UserOut(
        user_id=user_row["user_id"],
        username=user_row["username"],
        email=user_row["email"],
        full_name=user_row.get("full_name", ""),
        role=user_row["role"],
        is_active=user_row["is_active"],
        created_at=user_row["created_at"],
    )
# ---------------------------------------------------------------------------
# Role guard — use as a sub-dependency on endpoints that need role checks
# ---------------------------------------------------------------------------
def require_role(*allowed_roles: str):
    """Return a FastAPI dependency that rejects users outside *allowed_roles*.
    Usage::
        @router.get("/admin-only")
        def admin_panel(user: UserOut = Depends(require_role("admin"))):
            ...
    """
    def _guard(current_user: UserOut = Depends(get_current_user)) -> UserOut:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not permitted. "
                       f"Required: {', '.join(allowed_roles)}",
            )
        return current_user
    return _guard
# ---------------------------------------------------------------------------
# Bootstrap admin — seeds the first admin on an empty users table
# ---------------------------------------------------------------------------
def seed_admin(session: Session) -> None:
    """Create the bootstrap admin user if the users table is empty.
    Controlled by ``SOCMINT_ADMIN_EMAIL`` / ``SOCMINT_ADMIN_PASSWORD`` in
    ``.env``.  Safe to call on every startup — it's a no-op once any user
    exists.
    """
    settings = get_settings()
    if not settings.socmint_admin_email or not settings.socmint_admin_password:
        logger.info("No SOCMINT_ADMIN_EMAIL configured — skipping admin seed.")
        return
    # Check whether the table exists at all (first-boot race with schema init).
    try:
        result = session.execute(text("SELECT COUNT(*) FROM users"))
        count = result.scalar()
    except Exception:
        logger.warning("users table not yet available — admin seed skipped.")
        session.rollback()
        return
    if count and count > 0:
        logger.info("Users table already populated — admin seed skipped.")
        return
    hashed = get_password_hash(settings.socmint_admin_password)
    session.execute(
        text(
            """
            INSERT INTO users (username, email, hashed_password, full_name, role)
            VALUES (:username, :email, :hashed_password, 'System Administrator', 'admin')
            ON CONFLICT (username) DO NOTHING
            """
        ),
        {
            "username": settings.socmint_admin_username,
            "email": settings.socmint_admin_email,
            "hashed_password": hashed,
        },
    )
    session.commit()
    logger.info(
        "Bootstrap admin created: %s (%s)",
        settings.socmint_admin_username,
        settings.socmint_admin_email,
    )
