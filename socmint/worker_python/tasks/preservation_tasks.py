"""Asynchronous forensic preservation — runs OFF the sweep critical path.

Forensic preservation (HTML fetch + Wayback save + prior-snapshot pull) makes
several slow archive.org round trips per URL. Running it inline inside the Tier
1/2 sweep tasks delayed the chord — and therefore correlation — by minutes
whenever archive.org was slow. The sweep tasks now persist evidence immediately
and hand the (already-persisted) profile hits to this background task, which
preserves them and patches the snapshot refs back onto the evidence rows.
"""
from __future__ import annotations

import logging
from uuid import UUID

from worker_python.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="preservation.preserve_batch")
def preserve_evidence_batch(items: list) -> dict:
    """Preserve a batch of ``(evidence_id, url, case_id)`` hits, update refs.

    Never raises: preservation is best-effort and must never drop or block
    evidence. Returns a small summary for observability.
    """
    from api.db.postgres import session_scope
    from api.services.preservation import PreservationService
    from api.services.provenance import ProvenanceService

    preservation = PreservationService()
    provenance = ProvenanceService()
    preserved = 0
    requested = len(items or [])

    with session_scope() as session:
        for item in items or []:
            try:
                evidence_id, url, case_id = item[0], item[1], item[2]
            except (IndexError, TypeError, KeyError):
                continue
            try:
                refs = preservation.preserve(url, UUID(evidence_id), UUID(case_id))
                provenance.update_preservation_refs(UUID(evidence_id), refs, session)
                preserved += 1
            except Exception as exc:  # noqa: BLE001 — never crash on preservation
                logger.warning("async preservation failed for %s: %s", url, exc)

    return {"preserved": preserved, "requested": requested}
