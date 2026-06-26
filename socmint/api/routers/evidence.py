"""/api/v1/evidence — evidence retrieval, review queue, and analyst decisions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import minio_client
from api.db.postgres import get_db
from api.models.user import UserOut
from api.services.auth import get_current_user
from api.services.provenance import ProvenanceService

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])


class ReviewDecision(BaseModel):
    """Analyst decision payload for a queued identity link."""

    decision: str  # CONFIRMED | REJECTED | FLAG_UNCERTAIN
    analyst_id: str
    note: Optional[str] = None


@router.get("/{case_id}")
def list_evidence(
    case_id: UUID,
    tier: Optional[int] = None,
    result_type: Optional[str] = None,
    platform: Optional[str] = None,
    include_unavailable: bool = False,
    _user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """List evidence units for a case, with optional tier/type/platform filters."""
    query = (
        "SELECT evidence_id, tool_name, tool_tier, source_platform, source_tier, "
        "seed_type, seed_value, result_type, result_value, confidence_raw, "
        "platform_enrichment, snapshot_ref, snapshot_hash, wayback_ref, notes, "
        "timestamp_collected, timestamp_preserved "
        "FROM evidence_units WHERE case_id = :cid "
    )
    params: dict = {"cid": str(case_id)}
    if tier is not None:
        query += "AND tool_tier = :tier "
        params["tier"] = tier
    if result_type:
        query += "AND result_type = :rt "
        params["rt"] = result_type
    if platform:
        query += "AND source_platform = :plat "
        params["plat"] = platform
    if not include_unavailable:
        query += "AND result_type NOT IN ('unavailable','blocked') "
    query += "ORDER BY tool_tier, timestamp_collected DESC"

    rows = session.execute(text(query), params).mappings().all()
    return {"case_id": str(case_id), "count": len(rows), "evidence": [dict(r) for r in rows]}


@router.get("/{case_id}/review-queue")
def review_queue(
    case_id: UUID,
    _user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Return MEDIUM-tier links awaiting an analyst decision."""
    rows = session.execute(
        text(
            "SELECT link_id, account_a, account_b, platform_a, platform_b, "
            "confidence_score, confidence_tier, signal_breakdown, signal_count, "
            "analyst_decision FROM identity_links "
            "WHERE case_id = :cid AND confidence_tier = 'MEDIUM' "
            "AND analyst_decision IS NULL "
            "ORDER BY confidence_score DESC"
        ),
        {"cid": str(case_id)},
    ).mappings().all()
    return {"case_id": str(case_id), "count": len(rows), "queue": [dict(r) for r in rows]}


@router.post("/review/{link_id}")
def submit_review(
    link_id: UUID,
    payload: ReviewDecision,
    current_user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Record an analyst decision on an identity link (audited)."""
    if payload.decision not in ("CONFIRMED", "REJECTED", "FLAG_UNCERTAIN"):
        raise HTTPException(status_code=400, detail="invalid decision")

    row = session.execute(
        text(
            "UPDATE identity_links "
            "SET analyst_decision = :decision, analyst_note = :note, decided_at = :now "
            "WHERE link_id = :lid RETURNING case_id"
        ),
        {
            "decision": payload.decision,
            "note": payload.note,
            "now": datetime.now(timezone.utc),
            "lid": str(link_id),
        },
    ).first()
    session.commit()

    if not row:
        raise HTTPException(status_code=404, detail="link not found")

    case_id = UUID(str(row[0]))
    ProvenanceService().log_audit_event(
        case_id=case_id,
        run_id=None,
        event_type="ANALYST_DECISION",
        actor_id=current_user.username,
        metadata={"link_id": str(link_id), "decision": payload.decision, "note": payload.note},
        session=session,
    )
    return {"link_id": str(link_id), "decision": payload.decision}


@router.get("/{case_id}/screenshot/{evidence_id}")
def get_screenshot(
    case_id: UUID,
    evidence_id: UUID,
    _user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> Response:
    """Stream the preserved screenshot (PNG) for an evidence unit from MinIO."""
    row = session.execute(
        text("SELECT snapshot_ref FROM evidence_units WHERE evidence_id = :eid AND case_id = :cid"),
        {"eid": str(evidence_id), "cid": str(case_id)},
    ).first()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="no snapshot for evidence")

    object_path = f"cases/{case_id}/{evidence_id}/screenshot.png"
    try:
        data = minio_client.get_object_bytes(object_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"screenshot not found: {exc}") from exc
    return Response(content=data, media_type="image/png")


@router.get("/{case_id}/snapshot/{evidence_id}")
def get_snapshot(case_id: UUID, evidence_id: UUID, session: Session = Depends(get_db)) -> Response:
    """Stream the preserved page snapshot (raw HTML) for an evidence unit.

    Uses the stored ``snapshot_ref`` (the exact MinIO object key) so it works
    regardless of the artifact filename.
    """
    row = session.execute(
        text("SELECT snapshot_ref FROM evidence_units WHERE evidence_id = :eid AND case_id = :cid"),
        {"eid": str(evidence_id), "cid": str(case_id)},
    ).first()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="no snapshot for evidence")

    object_path = row[0]
    try:
        data = minio_client.get_object_bytes(object_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"snapshot not found: {exc}") from exc
    media = "text/html" if object_path.endswith((".html", ".htm")) else "application/octet-stream"
    return Response(content=data, media_type=media)
