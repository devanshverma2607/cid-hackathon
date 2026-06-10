"""MODULE 1 — Case Pydantic models (Legal Gate input + stored case).

Mirrors the `cases` table (Section 10.1) and the mandatory Legal Gate fields in
MODULE 1 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

SeedType = Literal["username", "email", "phone", "profile_url"]
TargetCategory = Literal["cybercrime", "fraud", "harassment", "research"]


class SeedInput(BaseModel):
    """A single subject identifier supplied at intake.

    The problem statement requires accepting *one or more* identifiers of mixed
    type (usernames/aliases, emails, phone numbers, profile URLs). Each one is
    validated and normalised by the Legal Gate independently.
    """

    seed_type: SeedType
    seed_value: str = Field(min_length=1)


class CaseCreate(BaseModel):
    """All mandatory Legal Gate fields an analyst must supply.

    ``seed_type``/``seed_value`` carry the *primary* identifier (kept for
    backward compatibility and stored on the ``cases`` row). ``additional_seeds``
    carries any extra identifiers for the same subject — every one is collected
    and correlated under the same ``case_id``.
    """

    authority_id: str = Field(min_length=1)
    agency_id: str = Field(min_length=1)
    analyst_id: str = Field(min_length=1)
    supervisor_approval: bool
    purpose_statement: str
    target_category: TargetCategory
    jurisdiction: str = Field(min_length=1)
    retention_period: int
    seed_type: SeedType
    seed_value: str = Field(min_length=1)
    additional_seeds: list[SeedInput] = Field(default_factory=list)

    @field_validator("supervisor_approval")
    @classmethod
    def _supervisor_must_approve(cls, v: bool) -> bool:
        # A False supervisor_approval fails the legal gate.
        if v is not True:
            raise ValueError("supervisor_approval must be True")
        return v

    @field_validator("purpose_statement")
    @classmethod
    def _purpose_min_length(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError("purpose_statement must be at least 20 characters")
        return v

    @field_validator("retention_period")
    @classmethod
    def _retention_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("retention_period must be a positive integer")
        return v


class Case(CaseCreate):
    """A persisted case, extending the intake fields with identifiers."""

    case_id: UUID
    created_at: datetime
