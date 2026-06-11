"""/api/v1/dossier — the consolidated subject dossier for a case.

Combines three engines into one investigator-facing view:
  * the **profile engine** (inferred attributes, behavioral fingerprint, temporal
    analysis, interests, footprint, explainable reasoning),
  * the **insight engine** (risk/exposure, ranked findings, investigative leads), and
  * the **persona resolver** (how many distinct people, which accounts cluster).
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.postgres import get_db
from api.services.insight_engine import InsightEngine
from api.services.persona_resolver import PersonaResolver
from api.services.profile_engine import ProfileEngine

router = APIRouter(prefix="/api/v1/dossier", tags=["dossier"])


def _rows(session: Session, sql: str, case_id: UUID) -> list[dict]:
    result = session.execute(text(sql), {"cid": str(case_id)}).mappings().all()
    return [json.loads(json.dumps(dict(r), default=str)) for r in result]


@router.get("/{case_id}")
def get_dossier(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Build and return the full consolidated dossier for a case."""
    evidence = _rows(session, "SELECT * FROM evidence_units WHERE case_id = :cid", case_id)
    links = _rows(
        session,
        "SELECT * FROM identity_links WHERE case_id = :cid ORDER BY confidence_score DESC",
        case_id,
    )
    case_rows = _rows(session, "SELECT * FROM cases WHERE case_id = :cid", case_id)
    case = case_rows[0] if case_rows else {"case_id": str(case_id)}

    persona = PersonaResolver().resolve(case_id, session)
    insights = InsightEngine().assess(evidence, links, case)
    profile = ProfileEngine().build(evidence, links, case, persona)

    return {
        "case_id": str(case_id),
        "generated_at": profile["generated_at"],
        "case": case,
        "profile": profile,
        "insights": insights,
        "persona": persona,
        "headline": {
            "name": (profile["attributes"]["names"][0]["value"]
                     if profile["attributes"]["names"] else None),
            "footprint_score": profile["footprint"]["footprint_score"],
            "visibility": profile["footprint"]["visibility"],
            "risk_band": insights["risk"]["band"],
            "risk_score": insights["risk"]["score"],
            "platform_count": profile["footprint"]["platform_count"],
            "persona_count": persona.get("persona_count", 0),
            "completeness": profile["profile_completeness"]["score"],
        },
    }


@router.get("/{case_id}/ai-summary")
def get_ai_summary(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Generate the optional grounded local-LLM dossier summary for a case.

    Dedicated endpoint so the main dossier renders instantly; this one can take
    longer (model load + inference) or return null when the LLM is unavailable.
    Skips the (heavy) persona resolve — the summary is derived from evidence.
    """
    evidence = _rows(session, "SELECT * FROM evidence_units WHERE case_id = :cid", case_id)
    links = _rows(
        session,
        "SELECT * FROM identity_links WHERE case_id = :cid ORDER BY confidence_score DESC",
        case_id,
    )
    case_rows = _rows(session, "SELECT * FROM cases WHERE case_id = :cid", case_id)
    case = case_rows[0] if case_rows else {"case_id": str(case_id)}

    profile = ProfileEngine().build(evidence, links, case, include_ai=True)
    return {"case_id": str(case_id), "ai_summary": profile.get("ai_summary")}
