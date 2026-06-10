"""/api/v1/cases endpoints — case intake behind the Legal Gate.

POST /api/v1/cases/create validates authorisation, normalises the seed, persists
the case, writes a CASE_CREATED audit event, and dispatches the pipeline.
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.postgres import get_db
from api.models.case import CaseCreate
from api.services.legal_gate import LegalGate
from api.services.provenance import ProvenanceService

router = APIRouter(prefix="/api/v1/cases", tags=["cases"])

legal_gate = LegalGate()
provenance = ProvenanceService()


def _dispatch_pipeline(seed_type: str, seed_value: str, case_id: UUID, run_id: UUID, analyst_id: str) -> None:
    """Dispatch the Tier 1/2 chord + Tier 3 background recon for one seed.

    Imported lazily so the API still serves if the worker package is absent.
    """
    from worker_python.celery_app import dispatch_pipeline

    dispatch_pipeline(
        seed_type=seed_type,
        seed_value=seed_value,
        case_id=str(case_id),
        run_id=str(run_id),
        analyst_id=analyst_id,
    )


def _dispatch_multi_pipeline(
    seeds: list[dict], case_id: UUID, run_id: UUID, analyst_id: str
) -> None:
    """Dispatch ONE combined chord across every identifier of the subject.

    ``seeds`` carries the resolved dispatch ``{"seed_type", "seed_value"}`` pairs
    for all inputs. A single chord guarantees correlation runs once over the
    union of all inputs' evidence. Imported lazily so the API still serves if the
    worker package is absent.
    """
    from worker_python.celery_app import dispatch_multi_pipeline

    dispatch_multi_pipeline(
        seeds=seeds,
        case_id=str(case_id),
        run_id=str(run_id),
        analyst_id=analyst_id,
    )



_CASE_SEEDS_READY = False


def _ensure_case_seeds_table(session: Session) -> None:
    """Create the ``case_seeds`` table if it is missing.

    ``schema.sql`` only runs on a fresh DB volume, so for already-initialised
    databases we create the table idempotently at runtime (once per process).
    """
    global _CASE_SEEDS_READY
    if _CASE_SEEDS_READY:
        return
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS case_seeds (
                seed_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                case_id         UUID NOT NULL REFERENCES cases(case_id),
                seed_type       TEXT NOT NULL,
                seed_value      TEXT NOT NULL,
                dispatch_type   TEXT NOT NULL,
                dispatch_value  TEXT NOT NULL,
                is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (case_id, seed_type, seed_value)
            )
            """
        )
    )
    session.execute(
        text("CREATE INDEX IF NOT EXISTS idx_case_seeds_case ON case_seeds (case_id)")
    )
    session.commit()
    _CASE_SEEDS_READY = True



@router.post("/create", status_code=status.HTTP_201_CREATED)
def create_case(payload: CaseCreate, session: Session = Depends(get_db)) -> dict:
    """Validate, persist, and start the pipeline for a new case.

    Accepts one *or more* subject identifiers (the primary ``seed_type``/
    ``seed_value`` plus any ``additional_seeds``). Every identifier is validated
    by the Legal Gate, persisted to ``case_seeds``, and dispatched under the same
    ``case_id`` so all evidence is correlated together.
    """
    # 1. Legal Gate — hard control on the mandatory fields + primary seed.
    ok, errors = legal_gate.validate(payload)

    # 1b. Validate each additional identifier independently.
    for idx, extra in enumerate(payload.additional_seeds):
        if not legal_gate.validate_seed(extra.seed_type, extra.seed_value):
            errors.append(f"additional_seeds[{idx}]")

    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Legal gate validation failed", "fields": errors},
        )

    # 2. Build the de-duplicated seed set (primary first), normalise + resolve.
    case_id = legal_gate.issue_case_id()
    run_id = legal_gate.issue_run_id()

    raw_seeds = [(payload.seed_type, payload.seed_value, True)] + [
        (s.seed_type, s.seed_value, False) for s in payload.additional_seeds
    ]
    seeds: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for seed_type, seed_value, is_primary in raw_seeds:
        normalised = legal_gate.normalise_seed(seed_type, seed_value)
        key = (seed_type, normalised)
        if key in seen:
            continue
        seen.add(key)
        dispatch_type, dispatch_value = legal_gate.resolve_dispatch_seed(seed_type, normalised)
        seeds.append(
            {
                "seed_type": seed_type,
                "seed_value": normalised,
                "dispatch_type": dispatch_type,
                "dispatch_value": dispatch_value,
                "is_primary": is_primary,
            }
        )

    primary = seeds[0]

    # 3. Persist the case (primary seed denormalised onto the row).
    session.execute(
        text(
            """
            INSERT INTO cases (
                case_id, authority_id, agency_id, analyst_id, supervisor_approval,
                purpose_statement, target_category, jurisdiction, retention_period,
                seed_type, seed_value
            ) VALUES (
                :case_id, :authority_id, :agency_id, :analyst_id, :supervisor_approval,
                :purpose_statement, :target_category, :jurisdiction, :retention_period,
                :seed_type, :seed_value
            )
            """
        ),
        {
            "case_id": str(case_id),
            "authority_id": payload.authority_id,
            "agency_id": payload.agency_id,
            "analyst_id": payload.analyst_id,
            "supervisor_approval": payload.supervisor_approval,
            "purpose_statement": payload.purpose_statement,
            "target_category": payload.target_category,
            "jurisdiction": payload.jurisdiction,
            "retention_period": payload.retention_period,
            "seed_type": primary["seed_type"],
            "seed_value": primary["seed_value"],
        },
    )

    # 3b. Persist the full identifier set.
    _ensure_case_seeds_table(session)
    for seed in seeds:
        session.execute(
            text(
                """
                INSERT INTO case_seeds (
                    case_id, seed_type, seed_value, dispatch_type, dispatch_value, is_primary
                ) VALUES (
                    :case_id, :seed_type, :seed_value, :dispatch_type, :dispatch_value, :is_primary
                )
                ON CONFLICT (case_id, seed_type, seed_value) DO NOTHING
                """
            ),
            {"case_id": str(case_id), **seed},
        )
    session.commit()

    # 4. Audit the intake.
    provenance.log_audit_event(
        case_id=case_id,
        run_id=run_id,
        event_type="CASE_CREATED",
        actor_id=payload.analyst_id,
        metadata={
            "seed_type": primary["seed_type"],
            "seed_value": primary["seed_value"],
            "seed_count": len(seeds),
            "seeds": [{"type": s["seed_type"], "value": s["seed_value"]} for s in seeds],
            "target_category": payload.target_category,
            "jurisdiction": payload.jurisdiction,
        },
        session=session,
    )

    # 5. Dispatch ONE combined pipeline across every identifier so correlation
    #    runs a single time over the union of all inputs' evidence (best-effort;
    #    never fail case creation on a dispatch error).
    dispatch_seeds = [
        {"seed_type": s["dispatch_type"], "seed_value": s["dispatch_value"]} for s in seeds
    ]
    pipeline_status = "pipeline_started"
    try:
        _dispatch_multi_pipeline(dispatch_seeds, case_id, run_id, payload.analyst_id)
    except Exception as exc:  # noqa: BLE001
        pipeline_status = "pipeline_dispatch_failed"
        provenance.log_audit_event(
            case_id=case_id,
            run_id=run_id,
            event_type="PIPELINE_DISPATCH_FAILED",
            actor_id="system",
            metadata={"error": str(exc), "seed_count": len(seeds)},
            session=session,
        )

    return {
        "case_id": str(case_id),
        "run_id": str(run_id),
        "status": pipeline_status,
        "seed_count": len(seeds),
    }




@router.get("")
def list_cases(session: Session = Depends(get_db)) -> dict:
    """List all cases (newest first) with evidence/link counts for the picker."""
    rows = session.execute(
        text(
            """
            SELECT c.case_id, c.seed_type, c.seed_value, c.target_category,
                   c.jurisdiction, c.analyst_id, c.created_at,
                   COALESCE(e.cnt, 0) AS evidence_count,
                   COALESCE(l.cnt, 0) AS link_count
            FROM cases c
            LEFT JOIN (
                SELECT case_id, COUNT(*) AS cnt FROM evidence_units
                WHERE result_type NOT IN ('unavailable','blocked')
                GROUP BY case_id
            ) e ON e.case_id = c.case_id
            LEFT JOIN (
                SELECT case_id, COUNT(*) AS cnt FROM identity_links
                GROUP BY case_id
            ) l ON l.case_id = c.case_id
            ORDER BY c.created_at DESC
            """
        )
    ).mappings().all()
    cases = [json.loads(json.dumps(dict(r), default=str)) for r in rows]
    return {"count": len(cases), "cases": cases}


@router.get("/{case_id}")
def get_case(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Return a stored case by id."""
    row = session.execute(
        text("SELECT * FROM cases WHERE case_id = :case_id"),
        {"case_id": str(case_id)},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="case not found")
    return json.loads(json.dumps(dict(row), default=str))
