"""/api/v1/persona — identity resolution (persona clustering) for a case."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.db.postgres import get_db
from api.services.persona_resolver import PersonaResolver

router = APIRouter(prefix="/api/v1", tags=["persona"])


@router.get("/persona/{case_id}")
def get_personas(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Cluster the case's accounts into confidence-scored human personas."""
    return PersonaResolver().resolve(case_id, session)
