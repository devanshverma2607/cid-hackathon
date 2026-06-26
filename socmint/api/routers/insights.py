"""/api/v1/insights — synthesised intelligence assessment for a case."""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.postgres import get_db
from api.models.user import UserOut
from api.services.auth import get_current_user
from api.services.insight_engine import InsightEngine

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])


def _rows(session: Session, sql: str, case_id: UUID) -> list[dict]:
    result = session.execute(text(sql), {"cid": str(case_id)}).mappings().all()
    return [json.loads(json.dumps(dict(r), default=str)) for r in result]


@router.get("/{case_id}")
def get_insights(
    case_id: UUID,
    _user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Build and return the ranked intelligence assessment for a case."""
    evidence = _rows(
        session, "SELECT * FROM evidence_units WHERE case_id = :cid", case_id
    )
    links = _rows(
        session,
        "SELECT * FROM identity_links WHERE case_id = :cid ORDER BY confidence_score DESC",
        case_id,
    )
    case_rows = _rows(session, "SELECT * FROM cases WHERE case_id = :cid", case_id)
    case = case_rows[0] if case_rows else {"case_id": str(case_id)}

    return InsightEngine().assess(evidence, links, case)


@router.get("/{case_id}/ai-narrative")
def get_ai_narrative(
    case_id: UUID,
    _user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Generate the optional grounded local-LLM narrative for a case.

    Kept on a dedicated endpoint so the main assessment renders instantly; this
    one can take longer (model load + inference) or return null when the local
    LLM is unavailable.
    """
    evidence = _rows(
        session, "SELECT * FROM evidence_units WHERE case_id = :cid", case_id
    )
    links = _rows(
        session,
        "SELECT * FROM identity_links WHERE case_id = :cid ORDER BY confidence_score DESC",
        case_id,
    )
    case_rows = _rows(session, "SELECT * FROM cases WHERE case_id = :cid", case_id)
    case = case_rows[0] if case_rows else {"case_id": str(case_id)}

    result = InsightEngine().assess(evidence, links, case, include_ai=True)
    return {"case_id": str(case_id), "ai_narrative": result.get("ai_narrative")}
