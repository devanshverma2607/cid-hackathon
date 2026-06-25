"""MODULE 0 — EvidenceUnit Pydantic model.

The canonical evidence record produced and consumed across the whole pipeline.
Mirrors the `evidence_units` table (Section 10.1) and the EvidenceUnit definition
in MODULE 0 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# result_type — exact Literal values from MODULE 0 / Section 5.
# SDM (Social Depth Module) types are appended at the end.
ResultType = Literal[
    "account_found",
    "email_registered",
    "breach_hit",
    "gravatar_hit",
    "google_hit",
    "whatsapp_hit",
    "domain_hit",
    "dork_hit",
    "archive_hit",
    "phone_intel",
    "email_reputation",
    "onion_hit",
    # --- SDM result types ---
    "profile_change_detected",
    "behavioral_insight",
    "reverse_image_hit",
    "post_timeline_collected",
    "community_membership_found",
    # --- status markers ---
    "unavailable",
    "blocked",
]

# tool_tier — 1=fast sweep, 2=deep, 3=passive, 4=triggered.
ToolTier = Literal[1, 2, 3, 4]

# source_tier — 1=API, 2=public web, 3=archive, 4=inferred.
SourceTier = Literal[1, 2, 3, 4]


def _utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class EvidenceUnit(BaseModel):
    """A single, provenance-tracked piece of evidence."""

    evidence_id: UUID = Field(default_factory=uuid4)
    case_id: UUID
    run_id: UUID

    tool_name: str
    tool_version: str
    tool_tier: ToolTier

    source_platform: str
    source_tier: SourceTier

    seed_type: str
    seed_value: str

    result_type: ResultType
    result_value: str

    confidence_raw: Optional[float] = None
    signal_weights: Optional[dict] = None
    bio_embedding: Optional[list[float]] = None
    image_embedding: Optional[list[float]] = None
    face_embedding: Optional[list[float]] = None

    timestamp_collected: datetime = Field(default_factory=_utcnow)
    timestamp_preserved: Optional[datetime] = None

    snapshot_ref: Optional[str] = None
    snapshot_hash: Optional[str] = None
    wayback_ref: Optional[str] = None
    archive_today_ref: Optional[str] = None

    platform_enrichment: Optional[dict] = None

    analyst_id: str
    notes: Optional[str] = None

    model_config = {"validate_assignment": True}
