"""Tier 3 Celery task — passive recon (background, non-blocking)."""
from __future__ import annotations

import logging
from uuid import UUID

from worker_python.celery_app import celery_app
from worker_python.adapters.fallback_chain import FallbackChainManager, ChainExhaustedError
from worker_python.tasks._pipeline import preserve_and_persist, COOLDOWNS

logger = logging.getLogger(__name__)


@celery_app.task(name="tier3.passive_recon")
def run_passive_recon(
    seed_value: str, seed_type: str, case_id: str, run_id: str, analyst_id: str
) -> dict:
    """Run Tor-routed dorking + archive sweep with per-tool cooldowns."""
    manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)

    # Tor-routed dorking tools enforce their own 30s cooldown via COOLDOWNS.
    logger.info("passive recon cooldown profile: %ss", COOLDOWNS.get("dorks_eye", 30))

    try:
        units = manager.execute_chain("passive_recon", seed_type, seed_value)
    except ChainExhaustedError:
        units = []
    count = preserve_and_persist(units)
    return {"tier": 3, "chain": "passive_recon", "hits": count}
