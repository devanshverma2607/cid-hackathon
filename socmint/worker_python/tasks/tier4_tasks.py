"""Tier 4 Celery task — platform enrichment on confirmed hits (MODULE 3)."""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text

from worker_python.celery_app import celery_app
from worker_python.adapters.fallback_chain import FallbackChainManager
from worker_python.adapters.platform.socid import SocidExtractorAdapter
from api.services.photo_hash import PhotoHasher
from worker_python.tasks._pipeline import preserve_and_persist

logger = logging.getLogger(__name__)


@celery_app.task(name="tier4.platform_enrichment")
def run_platform_enrichment(
    platform: str, account_url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> dict:
    """Trigger the platform tool matrix, attach enrichment, and preserve."""
    from api.db.postgres import session_scope
    from api.services.provenance import ProvenanceService

    case_uuid = UUID(case_id)
    run_uuid = UUID(run_id)
    manager = FallbackChainManager(case_uuid, run_uuid, analyst_id)

    units = manager.trigger_platform_tools(platform, account_url, case_uuid, run_uuid)

    # Structured-identifier extraction on the confirmed profile URL. Alternate
    # usernames / emails / linked profiles it surfaces feed the pivot brain.
    if "://" in (account_url or ""):
        try:
            socid_units = SocidExtractorAdapter().execute(
                account_url, case_uuid, run_uuid, analyst_id, "username"
            )
            units.extend(
                u for u in socid_units if u.result_type not in ("unavailable", "blocked")
            )
        except Exception as exc:  # noqa: BLE001 — enrichment must never break the task
            logger.debug("socid_extractor enrichment failed: %s", exc)

    # Profile-photo perceptual hash — activates the W_PHOTO_MATCH correlation
    # signal whenever the same avatar is reused across platforms.
    hasher = PhotoHasher()
    for unit in units:
        if unit.platform_enrichment:
            hasher.enrich_with_phash(unit.platform_enrichment)

    # Screenshot + Wayback preservation runs inside preserve_and_persist for hits.
    count = preserve_and_persist(units)

    # Attach enrichment payloads back onto matching evidence rows.
    provenance = ProvenanceService()
    with session_scope() as session:
        for unit in units:
            if unit.platform_enrichment:
                row = session.execute(
                    text(
                        "SELECT evidence_id FROM evidence_units "
                        "WHERE case_id = :cid AND source_platform = :p AND result_value = :rv "
                        "LIMIT 1"
                    ),
                    {"cid": case_id, "p": unit.source_platform, "rv": unit.result_value},
                ).first()
                if row:
                    provenance.attach_enrichment(row[0], unit.platform_enrichment, session)

    return {"tier": 4, "platform": platform, "hits": count}
