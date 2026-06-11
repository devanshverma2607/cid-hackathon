"""Pivot tasks — recursive cross-tool seed expansion (the correlation brain).

These tasks close the feedback loop: after a hop of tools runs, the Pivot Engine
reads every identifier those tools discovered and re-seeds the appropriate chain
for each new one, then re-correlates and recurses — bounded by depth / breadth /
total-seed caps enforced in the Pivot Engine's Redis visited set.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text

from worker_python.celery_app import celery_app
from worker_python.adapters.fallback_chain import FallbackChainManager
from worker_python.tasks._pipeline import preserve_and_persist

logger = logging.getLogger(__name__)


def _load_case_units(case_id: str):
    """Load all evidence units for a case as EvidenceUnit objects."""
    from api.db.postgres import session_scope
    from api.services.correlation import CorrelationEngine

    engine = CorrelationEngine()
    units = []
    with session_scope() as session:
        rows = session.execute(
            text("SELECT * FROM evidence_units WHERE case_id = :cid"),
            {"cid": case_id},
        ).mappings().all()
        for row in rows:
            try:
                units.append(engine._row_to_unit(dict(row)))
            except Exception as exc:  # noqa: BLE001 — skip malformed rows
                logger.debug("skip row in pivot load: %s", exc)
    return units


def _load_case(case_id: str) -> dict:
    """Load the case row (target_category drives dynamic pivot bounds)."""
    from api.db.postgres import session_scope
    try:
        with session_scope() as session:
            row = session.execute(
                text("SELECT target_category, seed_type FROM cases WHERE case_id = :cid"),
                {"cid": case_id},
            ).mappings().first()
            return dict(row) if row else {}
    except Exception as exc:  # noqa: BLE001 — missing case → conservative baseline
        logger.debug("pivot case load failed: %s", exc)
        return {}
def _sweep_signatures(seed: "object", case_id: str, run_id: str, analyst_id: str) -> list:
    from worker_python.tasks.tier1_tasks import (
        run_tier1_username_sweep, run_tier1_email_sweep, run_tier1_phone_sweep,
    )
    from worker_python.tasks.tier2_tasks import (
        run_tier2_username_sweep, run_tier2_email_sweep,
    )

    args = (seed.seed_value, case_id, run_id, analyst_id)
    if seed.seed_type == "email":
        return [run_tier1_email_sweep.s(*args), run_tier2_email_sweep.s(*args)]
    if seed.seed_type == "username":
        return [run_tier1_username_sweep.s(*args), run_tier2_username_sweep.s(*args)]
    if seed.seed_type == "phone":
        return [run_tier1_phone_sweep.s(*args)]
    if seed.seed_type == "domain":
        return [run_domain_recon.s(*args)]
    return []


@celery_app.task(name="pivot.domain_recon")
def run_domain_recon(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the domain tool matrix (theHarvester/finalrecon/sublist3r/dnstwist)."""
    manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
    units = manager.trigger_domain_tools(seed_value, UUID(case_id), UUID(run_id))
    count = preserve_and_persist(units)
    return {"tier": 4, "chain": "domain", "seed": seed_value, "hits": count}


@celery_app.task(name="pivot.expand")
def run_pivot_expansion(
    _prev, case_id: str, run_id: str, analyst_id: str, depth: int = 0
) -> dict:
    """Extract new identifiers from current evidence and re-seed each one.

    `_prev` absorbs the chord result list when this is used as a callback.
    """
    from celery import chord, group

    from api.services.pivot_engine import PivotEngine, PIVOT_ENABLED
    from api.services.graph_builder import GraphBuilder

    bounds = PivotEngine.compute_bounds(_load_case(case_id))
    if not PIVOT_ENABLED or depth >= bounds.max_depth:
        return {"depth": depth, "expanded": 0, "reason": "disabled_or_max_depth"}

    pivot = PivotEngine()
    units = _load_case_units(case_id)

    # Mark every identifier already used as a seed so we never re-sweep it.
    pivot.mark_processed(
        case_id, {f"{u.seed_type}:{(u.seed_value or '').lower()}" for u in units if u.seed_value}
    )

    candidates = pivot.extract_pivots(units)
    new_seeds = pivot.select_new(case_id, candidates, bounds.max_total, bounds.max_per_hop)
    if not new_seeds:
        return {"depth": depth, "expanded": 0, "reason": "no_new_identifiers"}

    # Record the cross-tool discovery edges (best-effort) for the brain graph.
    graph = GraphBuilder()
    for seed in new_seeds:
        try:
            graph.upsert_pivot_edge(
                case_id, seed.via_platform, seed.via_tool, seed.seed_type, seed.seed_value
            )
        except Exception as exc:  # noqa: BLE001 — graph is non-critical
            logger.debug("pivot edge write failed: %s", exc)

    # Build the sweep header for every new seed.
    header: list = []
    for seed in new_seeds:
        header.extend(_sweep_signatures(seed, case_id, run_id, analyst_id))

    logger.info(
        "pivot hop %s: re-seeding %s new identifiers (%s sweep tasks)",
        depth + 1, len(new_seeds), len(header),
    )
    if not header:
        return {"depth": depth, "expanded": 0, "reason": "no_sweeps"}

    chord(group(*header))(
        pivot_collect.s(case_id, run_id, analyst_id, depth + 1)
    )
    return {
        "depth": depth,
        "expanded": len(new_seeds),
        "seeds": [s.key for s in new_seeds],
    }


@celery_app.task(name="pivot.collect")
def pivot_collect(
    _results, case_id: str, run_id: str, analyst_id: str, depth: int
) -> dict:
    """Callback after a pivot hop: re-correlate, enrich, then recurse."""
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
            event_type="PIVOT_CORRELATION_COMPLETE",
            actor_id=analyst_id,
            metadata={"depth": depth, "links": len(links)},
            session=session,
        )

    # Fire platform enrichment for any HIGH/MEDIUM links found this hop.
    for link in links:
        if link.confidence_tier in ("HIGH", "MEDIUM"):
            for platform, account in (
                (link.platform_a, link.account_a), (link.platform_b, link.account_b)
            ):
                username = account.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
                run_platform_enrichment.delay(
                    platform, account, username, case_id, run_id, analyst_id
                )

    # Recurse to the next hop (Pivot Engine enforces depth/total caps).
    run_pivot_expansion.delay(None, case_id, run_id, analyst_id, depth)
    return {"depth": depth, "links": len(links)}
