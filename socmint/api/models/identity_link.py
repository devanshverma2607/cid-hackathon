"""IdentityLink Pydantic model.

Mirrors the `identity_links` table (Section 10.1) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

ConfidenceTier = Literal["HIGH", "MEDIUM", "LOW", "DISCARD"]
AnalystDecision = Literal["CONFIRMED", "REJECTED", "FLAG_UNCERTAIN"]


class IdentityLink(BaseModel):
    """A scored link asserting that two accounts belong to the same identity."""

    link_id: UUID = Field(default_factory=uuid4)
    case_id: UUID

    account_a: str
    account_b: str
    platform_a: str
    platform_b: str

    confidence_score: float
    confidence_tier: ConfidenceTier
    signal_breakdown: dict
    signal_count: int

    analyst_decision: Optional[AnalystDecision] = None
    analyst_note: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
