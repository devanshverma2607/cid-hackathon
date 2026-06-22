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
        "worker_python.tasks.recovery_tasks",
    ],
)

# Visibility timeout (seconds) for the Redis broker. A task that a worker has
# reserved but not yet acknowledged is redelivered to another worker after this
# long. With ``task_acks_late`` enabled, the ack only happens once the task
# *finishes*, so this MUST comfortably exceed the slowest single task (the Tier-2
# username sweep can take ~6-7 min when archive.org / search engines are slow) or
# Redis would redeliver a still-running sweep and double-run it. We pad to 1 h.
BROKER_VISIBILITY_TIMEOUT = int(os.environ.get("BROKER_VISIBILITY_TIMEOUT", "3600"))

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # --- crash resilience ---------------------------------------------------
    # Acknowledge a task only after it FINISHES (not when it is picked up). If a
    # worker dies mid-task (container restart, OOM, machine shutdown), the broker
    # redelivers the unacknowledged task to a live worker instead of silently
    # losing it. This is the core fix for the "scan died and never recovered"
    # mode: a lost Tier-1/2 header task is now retried, so the chord can still
    # complete and fire correlation. Our tasks are idempotent (evidence upserts on
    # a unique key; correlation/pivot are guarded), so re-execution is safe.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Don't let a worker hoard tasks it can't run yet — each process reserves at
    # most one extra task. Combined with acks_late this minimises how much work is
    # in-flight (and therefore at risk) when a worker is killed.
    worker_prefetch_multiplier=1,
    # Redis broker: redeliver an unacked (reserved) task only after the visibility
    # timeout, so a long-running sweep is never duplicated mid-flight.
    broker_transport_options={"visibility_timeout": BROKER_VISIBILITY_TIMEOUT},
    result_backend_transport_options={"visibility_timeout": BROKER_VISIBILITY_TIMEOUT},
    # If the broker connection drops on startup, keep retrying instead of crashing
    # the worker (Celery 6 default-on; set explicitly for older brokers too).
    broker_connection_retry_on_startup=True,
    # --- queue isolation ------------------------------------------------------
    # worker_go registers ONLY go.* tasks and consumes ONLY the dedicated "go"
    # queue (a worker discards messages for tasks it doesn't know — letting it
    # share the default queue destroyed ~half of every scan's tasks). Mirror the
    # route here so anything published from this app / the API for a go.* task
    # lands on the go queue, and everything else stays on the default queue that
    # only worker_python consumes.
    task_routes={"go.*": {"queue": "go"}},
)

# Correlation watchdog delay (seconds). The Tier 1/2 chord can intermittently
# drop a header task, leaving the callback (correlation) unfired. A guarded
# finalizer is scheduled this many seconds after dispatch; it is a no-op if the
# chord already fired correlation. Must exceed the slowest normal sweep so the
# happy-path chord callback wins the race (Tier 2 runs ~7 tools sequentially).
CORRELATION_WATCHDOG_SECONDS = int(os.environ.get("CORRELATION_WATCHDOG_SECONDS", "540"))

# Period (seconds) of the durable, beat-driven correlation sweeper. The per-
# dispatch countdown watchdog lives only in worker memory, so a worker restart
# before it fires loses it forever (observed in production: a dead run whose
# chord callback AND countdown watchdog were both lost on restart, leaving the
# case stuck with no correlation). This periodic task is re-emitted by Celery
# beat from durable schedule state, so recovery happens even across restarts.
CORRELATION_SWEEP_PERIOD = int(os.environ.get("CORRELATION_SWEEP_PERIOD", "120"))

# Beat schedule: a durable safety net that runs independently of any single
# scan's in-memory watchdog. ``recover_stuck_correlations`` scans the audit log
# for runs that swept evidence but never reached CORRELATION_COMPLETE and whose
# sweep has been quiet long enough that the chord is clearly never going to fire,
# then runs correlation for them. Because beat re-emits this from its own
# persisted schedule, it survives worker restarts that would lose a countdown
# ETA task. Beat is started by the dedicated ``worker_beat`` compose service.
celery_app.conf.beat_schedule = {
    "recover-stuck-correlations": {
        "task": "pipeline.recover_stuck_correlations",
        "schedule": float(CORRELATION_SWEEP_PERIOD),
    },
}


def _cancel_active_scans(case_id: str) -> None:
    """Cancel all active/queued tasks from previous scans so the new scan runs immediately.

    Strategy:
    1. Purge ALL pending (unstarted) tasks from the default Celery queue.
    2. Broadcast ``shutdown`` to currently executing tasks via Celery's revoke
       with ``terminate=True`` (sends SIGTERM to the worker fork processes).
    3. Log the cancellation to the audit trail.

    This is aggressive but correct for the "one active scan at a time" policy:
    the user explicitly wants a new scan to preempt any in-flight work. Evidence
    already persisted by the old scan remains in the DB (it is never deleted);
    only in-flight and queued tasks are discarded.
    """
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)

        # 1. Purge all pending tasks from the default queue (not the 'go' queue).
        purged = celery_app.control.purge()

        # 2. Get active tasks from all workers and revoke them.
        inspector = celery_app.control.inspect()
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}

        revoked_ids: list[str] = []
        for worker_name, tasks in {**active, **reserved}.items():
            for task_info in (tasks or []):
                task_id = task_info.get("id")
                if task_id:
                    celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
                    revoked_ids.append(task_id)

        # 3. Also purge the 'go' queue for Go worker tasks.
        try:
            go_queue_len = client.llen("go")
            if go_queue_len:
                client.delete("go")
        except Exception:
            pass

        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "cancelled active scans: purged=%s, revoked=%d tasks [%s]",
            purged, len(revoked_ids), ", ".join(revoked_ids[:10]),
        )

    except Exception as exc:  # noqa: BLE001 — best-effort; never block dispatch
        import logging
        logging.getLogger(__name__).warning("cancel_active_scans failed: %s", exc)


def _prepare_fresh_run(case_id: str) -> None:
    """Clear stale per-case orchestration state so a new scan starts fresh.

    Every new case already gets a unique ``case_id``/``run_id`` and its own
    Redis pivot keys, and ``task_acks_late`` ensures a crashed worker's in-flight
    tasks are redelivered rather than left orphaned — so a fresh case never
    inherits another scan's tools. This helper is the belt-and-braces companion:
    it deletes any lingering pivot visited-set / budget counters for *this*
    case_id (relevant when an operator re-dispatches the same case id, or a prior
    run for it crashed mid-pivot) so the recursive expansion starts with a clean
    budget instead of a half-spent one. Scoped to the case only — it never
    touches the broker queue, so concurrent scans are unaffected.
    """
    # Cancel any in-flight / queued tasks from previous scans (one-at-a-time policy).
    _cancel_active_scans(case_id)

    try:
        import redis  # local import keeps the module importable without redis

        client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        client.delete(
            f"socmint:pivot:seen:{case_id}",
            f"socmint:pivot:count:{case_id}",
        )
    except Exception:  # noqa: BLE001 — best-effort hygiene, never block dispatch
        pass


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

    # Start from a clean slate for this case (clears stale pivot budget/visited).
    _prepare_fresh_run(case_id)

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

    # Start from a clean slate for this case (clears stale pivot budget/visited).
    _prepare_fresh_run(case_id)

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

