"""Tier 2 Celery tasks — deep sweep + chord callback (MODULE 3)."""
from __future__ import annotations

import logging
from uuid import UUID

from worker_python.celery_app import celery_app
from worker_python.adapters.fallback_chain import FallbackChainManager, ChainExhaustedError
from worker_python.tasks._pipeline import preserve_and_persist

logger = logging.getLogger(__name__)


# Map a confirmed-account URL host to a Tier-4 enrichment platform key (the keys
# of FallbackChainManager.platform_map). Only platforms with a real enrichment
# adapter are listed — everything else is left to the username sweep.
_ENRICHABLE_HOSTS = {
    "github.com": "github",
    "instagram.com": "instagram",
    "t.me": "telegram", "telegram.org": "telegram", "telegram.me": "telegram",
    "tiktok.com": "tiktok",
    "bsky.app": "bluesky",
    "linkedin.com": "linkedin",
    "protonmail.com": "protonmail", "proton.me": "protonmail",
    "snapchat.com": "snapchat",
}
_MAX_CONFIRMED_ENRICHMENTS = 25


def _enrichable_platform(url: str) -> str | None:
    """Return the enrichment platform key for a profile URL, or None."""
    from urllib.parse import urlparse

    if not url or "://" not in url:
        return None
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _ENRICHABLE_HOSTS:
        return _ENRICHABLE_HOSTS[host]
    if "mastodon" in host:
        return "mastodon"
    return None


def _enrich_confirmed_accounts(
    case_id: str, run_id: str, analyst_id: str,
    already: set[str], enrich_task,
) -> int:
    """Dispatch platform enrichment for confirmed accounts on enrichable hosts."""
    from sqlalchemy import text
    from api.db.postgres import session_scope

    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    "SELECT DISTINCT result_value FROM evidence_units "
                    "WHERE case_id = :c AND result_type = 'account_found' "
                    "AND result_value LIKE 'http%'"
                ),
                {"c": case_id},
            ).all()
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the chord
        logger.warning("confirmed-account enrichment query failed: %s", exc)
        return 0

    dispatched = 0
    for (url,) in rows:
        if dispatched >= _MAX_CONFIRMED_ENRICHMENTS:
            break
        if url in already:
            continue
        platform_key = _enrichable_platform(url)
        if not platform_key:
            continue
        # Only enrich canonical profile URLs. The username sweep sometimes records
        # the *detection* endpoint (an API/search call) as the hit — those carry no
        # profile to enrich and would yield a garbage username.
        low_url = url.lower()
        if "?" in url or "/api/" in low_url or "/search" in low_url or "/xrpc/" in low_url:
            continue
        username = url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        try:
            enrich_task.delay(platform_key, url, username, case_id, run_id, analyst_id)
            already.add(url)
            dispatched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrichment dispatch failed for %s: %s", url, exc)
    if dispatched:
        logger.info("dispatched %d confirmed-account enrichments", dispatched)
    return dispatched


@celery_app.task(name="tier2.username_sweep")
def run_tier2_username_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the username_tier2 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("username_tier2", "username", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 2, "chain": "username_tier2", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier2 username sweep failed: %s", exc)
        return {"tier": 2, "chain": "username_tier2", "hits": 0, "error": str(exc)}


@celery_app.task(name="tier2.email_sweep")
def run_tier2_email_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the email_tier2 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("email_tier2", "email", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 2, "chain": "email_tier2", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier2 email sweep failed: %s", exc)
        return {"tier": 2, "chain": "email_tier2", "hits": 0, "error": str(exc)}


@celery_app.task(name="pipeline.aggregate_results")
def aggregate_results(results, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Chord callback: run correlation, then trigger platform enrichment."""
    from api.db.postgres import session_scope
    from api.services.correlation import CorrelationEngine
    from api.services.provenance import ProvenanceService
    from worker_python.tasks.tier4_tasks import run_platform_enrichment

    engine = CorrelationEngine()
    provenance = ProvenanceService()
    case_uuid = UUID(case_id)

    with session_scope() as session:
        links = engine.run_full_correlation(case_uuid, session)
        provenance.log_audit_event(
            case_id=case_uuid,
            run_id=UUID(run_id),
            event_type="CORRELATION_COMPLETE",
            actor_id=analyst_id,
            metadata={"links": len(links)},
            session=session,
        )

    # Fire Tier 4 platform enrichment for HIGH/MEDIUM links.
    enrichment_dispatched = 0
    dispatched_urls: set[str] = set()
    for link in links:
        if link.confidence_tier in ("HIGH", "MEDIUM"):
            for platform, account in ((link.platform_a, link.account_a), (link.platform_b, link.account_b)):
                username = account.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
                run_platform_enrichment.delay(platform, account, username, case_id, run_id, analyst_id)
                dispatched_urls.add(account)
                enrichment_dispatched += 1

    # Also enrich confirmed first-party accounts on enrichable platforms even
    # when no correlation link pointed at them. A single-username case (e.g. a
    # lone GitHub/Instagram/Telegram profile) would otherwise never have its
    # rich profile fields (name, bio, location, avatar, creation date) fetched,
    # leaving the Subject Dossier starved. Bounded to keep load predictable.
    enrichment_dispatched += _enrich_confirmed_accounts(
        case_id, run_id, analyst_id, dispatched_urls, run_platform_enrichment
    )

    # Kick off the recursive pivot loop (the brain): feed every newly discovered
    # identifier back into the pipeline as a fresh seed. Bounded by depth /
    # breadth / total caps inside the Pivot Engine.
    from worker_python.tasks.pivot_tasks import run_pivot_expansion
    run_pivot_expansion.delay(None, case_id, run_id, analyst_id, 0)

    return {"links": len(links), "enrichment_dispatched": enrichment_dispatched}


@celery_app.task(name="pipeline.finalize_correlation")
def finalize_correlation(case_id: str, run_id: str, analyst_id: str) -> dict:
    """Watchdog: guarantee correlation runs even if the chord callback was lost.

    The Tier 1/2 chord intermittently drops a header task (Celery group/chord
    publish flakiness): when that happens the chord never reaches its completion
    count and ``aggregate_results`` never fires, so correlation / persona /
    pivot / enrichment silently never run. This task is scheduled with a
    countdown at dispatch time. If ``CORRELATION_COMPLETE`` has already been
    logged for this run (the happy path, where the chord fired), it is a no-op;
    otherwise it runs the aggregation itself over whatever evidence has landed.
    """
    from sqlalchemy import text
    from api.db.postgres import session_scope

    with session_scope() as session:
        done = session.execute(
            text(
                "SELECT 1 FROM audit_log "
                "WHERE run_id = :r AND event_type = 'CORRELATION_COMPLETE' LIMIT 1"
            ),
            {"r": str(run_id)},
        ).first()

    if done:
        return {"finalized": False, "reason": "already_correlated"}

    logger.warning(
        "correlation watchdog firing for run %s — chord callback was lost", run_id
    )
    return aggregate_results([], case_id, run_id, analyst_id)
