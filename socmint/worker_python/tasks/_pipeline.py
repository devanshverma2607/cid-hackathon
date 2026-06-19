"""Shared pipeline helpers for Celery tasks (cooldowns, preserve + persist)."""
from __future__ import annotations

import logging

from api.db.postgres import session_scope
from api.models.evidence import EvidenceUnit
from api.services.normaliser import DataNormaliser
from api.services.provenance import ProvenanceService

logger = logging.getLogger(__name__)

# Section / MODULE 3 — per-tool rate-limit cooldowns (seconds).
COOLDOWNS = {
    "sherlock": 5,
    "maigret": 5,
    "holehe": 10,
    "h8mail": 5,
    "ghunt": 15,
    "whatsmyname": 5,
    "nexfil": 5,
    "social_analyzer": 8,
    "dorks_eye": 30,
    "dorksint": 30,
    "forum_sweep": 30,
    "toutatis": 20,
    "telegramsint": 10,
    "geogramint": 10,
    "tiktok_userdata": 15,
    "xposedornot": 1,
    "hudsonrock": 2,
    "proxynova": 2,
    "intelx": 2,
    "abstractapi_phone": 1,
    "virustotal": 15,
    "shodan": 2,
    "hunterio": 2,
}


def _is_positive(unit: EvidenceUnit) -> bool:
    return unit.result_type not in ("unavailable", "blocked")


# Forensic preservation (HTML fetch + screenshot + Wayback) is expensive and is
# only worth running on suspect-linked hits that point at a real profile/leak.
# High-volume recon noise (dork search results, archive URL lists, raw domain
# hits, generic search enrichment) would otherwise inflate every task by minutes
# while archiving irrelevant pages, so it is persisted WITHOUT preservation.
PRESERVE_TYPES = frozenset({"account_found", "email_registered", "breach_hit"})

# A single account-enumeration tool (e.g. maigret/sherlock) can return dozens of
# profile URLs for a common alias. Forensically preserving every one would stall
# the tier for minutes on Wayback timeouts/429s. Cap preservation to the first N
# hits per tool; the remainder are still persisted to the DB (and can be
# preserved on demand later), they just skip the expensive snapshot step here.
MAX_PRESERVE_PER_TOOL = 8


def preserve_and_persist(units: list[EvidenceUnit]) -> int:
    """Normalise + upsert evidence to PostgreSQL, then queue async preservation.

    Forensic preservation (HTML fetch + Wayback save + prior-snapshot pull) makes
    several slow archive.org round trips per URL. Running it inline here delayed
    the Tier 1/2 chord — and therefore correlation — by minutes whenever
    archive.org was slow. So evidence is now persisted immediately (fast, so live
    status + correlation are not blocked) and the capped, preservable profile
    hits are handed to a background task (``preservation.preserve_batch``) which
    snapshots them and patches the refs back. Returns positive hits persisted.
    """
    normaliser = DataNormaliser()
    provenance = ProvenanceService()

    units = normaliser.normalise(units)
    positive_count = 0
    preserved_per_tool: dict[str, int] = {}
    to_preserve: list[tuple[str, str, str]] = []  # (evidence_id, url, case_id)

    with session_scope() as session:
        for unit in units:
            positive = _is_positive(unit)
            if positive:
                positive_count += 1
            try:
                # write_to_db upserts on the dedup constraint and RETURNs the
                # canonical evidence_id (the existing row's id on conflict), which
                # is what the async preservation task must patch.
                evidence_id = provenance.write_to_db(unit, session)
                # Commit each hit as it lands so live pipeline status and the
                # dashboard reflect progress during the sweep, rather than only
                # after the whole (potentially long) task completes.
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                logger.warning("db write failed for evidence: %s", exc)
                continue

            if positive:
                url = unit.result_value if "://" in (unit.result_value or "") else None
                tool = unit.tool_name or "unknown"
                under_cap = preserved_per_tool.get(tool, 0) < MAX_PRESERVE_PER_TOOL
                if url and unit.result_type in PRESERVE_TYPES and under_cap:
                    preserved_per_tool[tool] = preserved_per_tool.get(tool, 0) + 1
                    to_preserve.append((str(evidence_id), url, str(unit.case_id)))

    # Hand forensic preservation to the background task so it never blocks the
    # sweep (and thus the chord callback / correlation).
    if to_preserve:
        try:
            from worker_python.tasks.preservation_tasks import preserve_evidence_batch
            preserve_evidence_batch.delay(to_preserve)
        except Exception as exc:  # noqa: BLE001 — preservation is best-effort
            logger.warning("could not dispatch async preservation: %s", exc)

    return positive_count
