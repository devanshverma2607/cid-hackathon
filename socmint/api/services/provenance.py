"""MODULE 0 — Provenance Service.

Owns creation, validation, persistence, hashing, and audit logging of evidence.
Every other module produces or consumes the EvidenceUnit schema this service
governs. See MODULE 0 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.models.evidence import EvidenceUnit


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _vector_literal(embedding: Optional[list[float]]) -> Optional[str]:
    """Render a Python float list as a pgvector text literal, or None."""
    if not embedding:
        return None
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


class ProvenanceService:
    """Create, validate, persist, hash, and audit EvidenceUnits."""

    def create_evidence_unit(self, data: dict) -> EvidenceUnit:
        """Validate input and build an EvidenceUnit with generated provenance."""
        payload = dict(data)
        payload.setdefault("evidence_id", uuid4())
        payload.setdefault("timestamp_collected", _utcnow())
        unit = EvidenceUnit(**payload)
        self.validate_schema(unit)
        return unit

    def validate_schema(self, unit: EvidenceUnit) -> bool:
        """Run validation and required-field checks; raise on failure."""
        # Re-validate the model to catch any mutated assignments.
        EvidenceUnit.model_validate(unit.model_dump())
        if not str(unit.seed_value).strip():
            raise ValidationError.from_exception_data(
                "EvidenceUnit",
                [{
                    "type": "value_error",
                    "loc": ("seed_value",),
                    "input": unit.seed_value,
                    "ctx": {"error": "seed_value must not be empty"},
                }],
            )
        if not str(unit.tool_name).strip():
            raise ValidationError.from_exception_data(
                "EvidenceUnit",
                [{
                    "type": "value_error",
                    "loc": ("tool_name",),
                    "input": unit.tool_name,
                    "ctx": {"error": "tool_name must not be empty"},
                }],
            )
        return True

    def write_to_db(self, unit: EvidenceUnit, session: Session) -> UUID:
        """Upsert an EvidenceUnit on the dedup UNIQUE constraint.

        On conflict, refresh preservation + enrichment fields.
        """
        self.validate_schema(unit)
        stmt = text(
            """
            INSERT INTO evidence_units (
                evidence_id, case_id, run_id, tool_name, tool_version, tool_tier,
                source_platform, source_tier, seed_type, seed_value, result_type,
                result_value, confidence_raw, signal_weights, bio_embedding,
                timestamp_collected, timestamp_preserved, snapshot_ref,
                snapshot_hash, wayback_ref, platform_enrichment, analyst_id, notes
            ) VALUES (
                :evidence_id, :case_id, :run_id, :tool_name, :tool_version, :tool_tier,
                :source_platform, :source_tier, :seed_type, :seed_value, :result_type,
                :result_value, :confidence_raw, CAST(:signal_weights AS JSONB),
                CAST(:bio_embedding AS vector), :timestamp_collected,
                :timestamp_preserved, :snapshot_ref, :snapshot_hash, :wayback_ref,
                CAST(:platform_enrichment AS JSONB), :analyst_id, :notes
            )
            ON CONFLICT (case_id, source_platform, result_value, seed_value)
            DO UPDATE SET
                run_id              = EXCLUDED.run_id,
                result_type         = EXCLUDED.result_type,
                timestamp_collected = EXCLUDED.timestamp_collected,
                snapshot_ref        = EXCLUDED.snapshot_ref,
                snapshot_hash       = EXCLUDED.snapshot_hash,
                wayback_ref         = EXCLUDED.wayback_ref,
                platform_enrichment = EXCLUDED.platform_enrichment
            RETURNING evidence_id
            """
        )
        params = {
            "evidence_id": str(unit.evidence_id),
            "case_id": str(unit.case_id),
            "run_id": str(unit.run_id),
            "tool_name": unit.tool_name,
            "tool_version": unit.tool_version,
            "tool_tier": unit.tool_tier,
            "source_platform": unit.source_platform,
            "source_tier": unit.source_tier,
            "seed_type": unit.seed_type,
            "seed_value": unit.seed_value,
            "result_type": unit.result_type,
            "result_value": unit.result_value,
            "confidence_raw": unit.confidence_raw,
            "signal_weights": json.dumps(unit.signal_weights) if unit.signal_weights is not None else None,
            "bio_embedding": _vector_literal(unit.bio_embedding),
            "timestamp_collected": unit.timestamp_collected,
            "timestamp_preserved": unit.timestamp_preserved,
            "snapshot_ref": unit.snapshot_ref,
            "snapshot_hash": unit.snapshot_hash,
            "wayback_ref": unit.wayback_ref,
            "platform_enrichment": json.dumps(unit.platform_enrichment) if unit.platform_enrichment is not None else None,
            "analyst_id": unit.analyst_id,
            "notes": unit.notes,
        }
        result = session.execute(stmt, params)
        returned = result.scalar_one()
        session.commit()
        return UUID(str(returned))

    def update_preservation_refs(
        self, evidence_id: UUID, refs: dict, session: Session
    ) -> None:
        """Patch preservation columns onto an already-persisted evidence row.

        Used by the asynchronous preservation task so forensic snapshotting can
        run OFF the sweep critical path (it makes several slow archive.org round
        trips per URL) and patch the refs back once complete.
        """
        stmt = text(
            """
            UPDATE evidence_units
            SET snapshot_ref         = :snapshot_ref,
                snapshot_hash        = :snapshot_hash,
                wayback_ref          = :wayback_ref,
                archive_today_ref    = :archive_today_ref,
                timestamp_preserved  = :timestamp_preserved
            WHERE evidence_id = :evidence_id
            """
        )
        session.execute(
            stmt,
            {
                "snapshot_ref": refs.get("snapshot_ref"),
                "snapshot_hash": refs.get("snapshot_hash"),
                "wayback_ref": refs.get("wayback_ref"),
                "archive_today_ref": refs.get("archive_today_ref"),
                "timestamp_preserved": _utcnow(),
                "evidence_id": str(evidence_id),
            },
        )
        session.commit()

    def compute_hash(self, artifact_bytes: bytes) -> str:
        """SHA-256 hex digest of an artifact."""
        return hashlib.sha256(artifact_bytes).hexdigest()

    def log_audit_event(
        self,
        case_id: Optional[UUID],
        run_id: Optional[UUID],
        event_type: str,
        actor_id: str,
        metadata: dict,
        session: Session,
    ) -> None:
        """Append-only insert into audit_log. Never UPDATE or DELETE this table."""
        stmt = text(
            """
            INSERT INTO audit_log (case_id, run_id, event_type, actor_id, event_metadata)
            VALUES (:case_id, :run_id, :event_type, :actor_id, CAST(:event_metadata AS JSONB))
            """
        )
        session.execute(
            stmt,
            {
                "case_id": str(case_id) if case_id else None,
                "run_id": str(run_id) if run_id else None,
                "event_type": event_type,
                "actor_id": actor_id,
                "event_metadata": json.dumps(metadata or {}),
            },
        )
        session.commit()

    def attach_enrichment(self, evidence_id: UUID, enrichment_data: dict, session: Session) -> None:
        """Update only the platform_enrichment JSONB column; log an audit event."""
        stmt = text(
            """
            UPDATE evidence_units
            SET platform_enrichment = CAST(:enrichment AS JSONB)
            WHERE evidence_id = :evidence_id
            RETURNING case_id, run_id
            """
        )
        row = session.execute(
            stmt,
            {"enrichment": json.dumps(enrichment_data or {}), "evidence_id": str(evidence_id)},
        ).first()
        session.commit()

        case_id = UUID(str(row[0])) if row and row[0] else None
        run_id = UUID(str(row[1])) if row and row[1] else None
        self.log_audit_event(
            case_id=case_id,
            run_id=run_id,
            event_type="ENRICHMENT_ATTACHED",
            actor_id="system",
            metadata={"evidence_id": str(evidence_id)},
            session=session,
        )
