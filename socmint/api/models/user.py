"""Pydantic models for user authentication and registration.
Follows the project convention: strict typing, UUID identifiers, Literal-
constrained enums. The role field aligns with the Legal Gate requirement
that ``supervisor_approval`` must be authorised by a user with at least
the 'supervisor' role.
"""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field, field_validator
# The three roles supported by the system.  'supervisor' satisfies the Legal
# Gate's ``supervisor_approval`` requirement; 'admin' can manage users.
UserRole = Literal["analyst", "supervisor", "admin"]
# ---------------------------------------------------------------------------
# Request / shared models
# ---------------------------------------------------------------------------
class UserBase(BaseModel):
    """Fields shared across creation and read models."""
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    full_name: str = Field(default="", max_length=200)
    role: UserRole = "analyst"
    @field_validator("username")
    @classmethod
    def _username_clean(cls, v: str) -> str:
        """Lowercase and strip whitespace — matches seed normalisation style."""
        cleaned = v.strip().lower()
        if not cleaned:
            raise ValueError("username must not be blank")
        return cleaned
class UserCreate(UserBase):
    """Payload for ``POST /api/v1/auth/register``.
    The plain-text password is validated here (min 8 chars) and hashed
    server-side before storage — it is never persisted as-is.
    """
    password: str = Field(min_length=8, max_length=128)
# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class UserOut(UserBase):
    """Safe public representation of a stored user — no password field."""
    user_id: UUID
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}
# ---------------------------------------------------------------------------
# JWT token models
# ---------------------------------------------------------------------------
class Token(BaseModel):
    """Returned by the login endpoint."""
    access_token: str
    token_type: str = "bearer"
class TokenData(BaseModel):
    """Claims decoded from the JWT payload.
    Carried in the token so ``get_current_user`` can resolve the caller
    without a database round-trip on every request (the DB lookup only
    happens to verify ``is_active``).
    """
    user_id: str          # UUID serialised as str inside the JWT
    username: str
    email: str
    role: UserRole
