"""Celery application + pipeline orchestration for the Python worker.

Configures Celery with Redis, registers task modules, and exposes
`dispatch_pipeline()` which fires the Tier 1/2 chord plus Tier 3 background
recon (Section 16 / MODULE 3 of SOCMINT_PLAN_v2_0.txt).
"""
from __future__ import annotations

import os

from celery import Celery, chord, group

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "socmint",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "worker_python.tasks.tier1_tasks",
        "worker_python.tasks.tier2_tasks",
        "worker_python.tasks.tier3_tasks",
        "worker_python.tasks.tier4_tasks",
        "worker_python.tasks.pivot_tasks",
        "worker_python.tasks.preservation_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

# Correlation watchdog delay (seconds). The Tier 1/2 chord can intermittently
# drop a header task, leaving the callback (correlation) unfired. A guarded
# finalizer is scheduled this many seconds after dispatch; it is a no-op if the
# chord already fired correlation. Must exceed the slowest normal sweep so the
# happy-path chord callback wins the race (Tier 2 runs ~7 tools sequentially).
CORRELATION_WATCHDOG_SECONDS = int(os.environ.get("CORRELATION_WATCHDOG_SECONDS", "540"))


def _build_header(
    seed_type: str, seed_value: str, case_id: str, run_id: str, analyst_id: str
) -> list:
    """Return the flat list of Tier 1/2 chord-header signatures for one seed.

    A flat list is used (rather than nesting group(tier1, tier2)) because Celery
    flattens nested single-task groups unreliably, which could drop a single
    Tier-1 task from the chord header.
    """
    from worker_python.tasks.tier1_tasks import (
        run_tier1_username_sweep, run_tier1_email_sweep, run_tier1_phone_sweep,
    )
    from worker_python.tasks.tier2_tasks import (
        run_tier2_username_sweep, run_tier2_email_sweep,
    )

    args = (seed_value, case_id, run_id, analyst_id)

    if seed_type == "email":
        return [
            run_tier1_email_sweep.s(*args),
            run_tier1_username_sweep.s(*args),
            run_tier2_email_sweep.s(*args),
            run_tier2_username_sweep.s(*args),
        ]
    if seed_type == "phone":
        # Phone seeds run the dedicated phone chain (ignorant) only. Username
        # Tier-2 tools (maigret/sherlock/…) look up *usernames* and produce
        # nothing useful for a raw phone-number string while blocking the worker
        # for minutes, so they are intentionally excluded here.
        return [run_tier1_phone_sweep.s(*args)]
    # username (and any profile_url already resolved to a username upstream)
    return [
        run_tier1_username_sweep.s(*args),
        run_tier2_username_sweep.s(*args),
    ]


def dispatch_pipeline(
    seed_type: str, seed_value: str, case_id: str, run_id: str, analyst_id: str
) -> str:
    """Fire Tier 1 + Tier 2 as a chord, with Tier 3 as a background task.

    After Tier 1 + Tier 2 complete, `aggregate_results` triggers correlation.
    """
    from worker_python.tasks.tier2_tasks import aggregate_results
    from worker_python.tasks.tier3_tasks import run_passive_recon

    header = _build_header(seed_type, seed_value, case_id, run_id, analyst_id)

    # Tier 3 passive recon fires immediately in the background (non-blocking).
    run_passive_recon.delay(seed_value, seed_type, case_id, run_id, analyst_id)

    # Chord: after the header tasks finish, aggregate_results runs correlation.
    workflow = chord(group(*header))(aggregate_results.s(case_id, run_id, analyst_id))

    # Watchdog: the chord can intermittently drop a header task, in which case
    # the callback never fires. Schedule a guarded finalizer to run correlation
    # if it has not happened by the time the sweeps should be done.
    _schedule_correlation_watchdog(case_id, run_id, analyst_id)
    return workflow.id


def dispatch_multi_pipeline(
    seeds: list[dict], case_id: str, run_id: str, analyst_id: str
) -> str:
    """Fire ONE combined chord across *multiple* subject identifiers.

    ``seeds`` is a list of ``{"seed_type": str, "seed_value": str}`` describing
    every identifier supplied for the same subject (e.g. a username *and* an
    email). The header tasks for all seeds are unioned into a single chord so
    that the single ``aggregate_results`` callback runs correlation exactly once,
    over the *complete* evidence set gathered from every input — instead of N
    racing correlations that each see only a partial picture. Tier 3 passive
    recon still fires per seed in the background.

    Falls back to a no-op when ``seeds`` is empty; deduplicates identical
    ``(seed_type, seed_value)`` pairs.
    """
    from worker_python.tasks.tier2_tasks import aggregate_results
    from worker_python.tasks.tier3_tasks import run_passive_recon

    header: list = []
    seen: set[tuple[str, str]] = set()
    for seed in seeds or []:
        seed_type = str(seed.get("seed_type") or "").strip()
        seed_value = str(seed.get("seed_value") or "").strip()
        if not seed_type or not seed_value:
            continue
        key = (seed_type, seed_value)
        if key in seen:
            continue
        seen.add(key)
        header.extend(_build_header(seed_type, seed_value, case_id, run_id, analyst_id))
        # Tier 3 passive recon per seed (non-blocking background).
        run_passive_recon.delay(seed_value, seed_type, case_id, run_id, analyst_id)

    if not header:
        return ""

    # One chord, one aggregation: correlation runs once over the union of every
    # input's evidence, so insights span all identifiers together.
    workflow = chord(group(*header))(aggregate_results.s(case_id, run_id, analyst_id))

    # Watchdog: the chord can intermittently drop a header task, in which case
    # the callback never fires. Schedule a guarded finalizer to run correlation
    # if it has not happened by the time the sweeps should be done.
    _schedule_correlation_watchdog(case_id, run_id, analyst_id)
    return workflow.id


def _schedule_correlation_watchdog(case_id: str, run_id: str, analyst_id: str) -> None:
    """Schedule the guarded correlation finalizer as a chord-drop safety net."""
    from worker_python.tasks.tier2_tasks import finalize_correlation

    finalize_correlation.apply_async(
        args=[case_id, run_id, analyst_id],
        countdown=CORRELATION_WATCHDOG_SECONDS,
    )

