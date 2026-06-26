"""/api/v1/graph — identity graph export + system-wide health checks."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import minio_client, neo4j as neo4j_db
from api.db.postgres import get_db
from api.models.user import UserOut
from api.services.auth import get_current_user
from api.services.graph_builder import GraphBuilder

router = APIRouter(prefix="/api/v1", tags=["graph"])


@router.get("/graph/{case_id}")
def get_graph(
    case_id: UUID,
    max_nodes: int = 50,
    include_pivots: bool = True,
    _user: UserOut = Depends(get_current_user),
) -> dict:
    """Return the Plotly-ready {nodes, edges} graph for a case."""
    return GraphBuilder().export_graph_for_plotly(
        case_id, max_nodes=max_nodes, include_pivots=include_pivots
    )


@router.get("/graph/{case_id}/communities")
def get_communities(
    case_id: UUID,
    _user: UserOut = Depends(get_current_user),
) -> dict:
    """Detect community structure in the case identity graph (Louvain / fallback)."""
    return GraphBuilder().detect_communities(case_id, write_back=True)


@router.get("/health")
def health(session: Session = Depends(get_db)) -> dict:
    """Liveness probe across all backing services."""
    services: dict[str, str] = {}

    try:
        session.execute(text("SELECT 1"))
        services["postgres"] = "up"
    except Exception as exc:  # noqa: BLE001
        services["postgres"] = f"down: {exc}"

    try:
        services["neo4j"] = "up" if neo4j_db.ping() else "down"
    except Exception as exc:  # noqa: BLE001
        services["neo4j"] = f"down: {exc}"

    try:
        services["minio"] = "up" if minio_client.ping() else "down"
    except Exception as exc:  # noqa: BLE001
        services["minio"] = f"down: {exc}"

    overall = "healthy" if all(v == "up" for v in services.values()) else "degraded"
    return {"status": overall, "services": services}
