"""Tier 1 Celery tasks — fast sweep (MODULE 3)."""
from __future__ import annotations

import logging
from uuid import UUID

from worker_python.celery_app import celery_app
from worker_python.adapters.fallback_chain import FallbackChainManager, ChainExhaustedError
from worker_python.tasks._pipeline import preserve_and_persist

logger = logging.getLogger(__name__)


@celery_app.task(name="tier1.username_sweep")
def run_tier1_username_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the username_tier1 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("username_tier1", "username", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 1, "chain": "username_tier1", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier1 username sweep failed: %s", exc)
        return {"tier": 1, "chain": "username_tier1", "hits": 0, "error": str(exc)}


@celery_app.task(name="tier1.email_sweep")
def run_tier1_email_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the email_tier1 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("email_tier1", "email", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 1, "chain": "email_tier1", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier1 email sweep failed: %s", exc)
        return {"tier": 1, "chain": "email_tier1", "hits": 0, "error": str(exc)}


@celery_app.task(name="tier1.phone_sweep")
def run_tier1_phone_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the phone_tier1 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("phone_tier1", "phone", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 1, "chain": "phone_tier1", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier1 phone sweep failed: %s", exc)
        return {"tier": 1, "chain": "phone_tier1", "hits": 0, "error": str(exc)}
