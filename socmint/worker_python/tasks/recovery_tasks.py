"""Durable scan-recovery tasks (beat-driven safety net).

The per-dispatch correlation watchdog (``finalize_correlation`` scheduled with a
``countdown``) lives only in the worker's memory: if the worker restarts before
the countdown elapses, the watchdog is lost and a run whose chord callback was
also dropped stays stuck forever with no ``CORRELATION_COMPLETE`` event — no
correlation, no Tier-4 enrichment, no pivots. This was observed in production.

``recover_stuck_correlations`` is a periodic task emitted by **Celery beat** from
its own persisted schedule, so it keeps running across worker restarts. Each
tick it finds runs that gathered evidence but never correlated, and whose sweep
has been quiet long enough that the chord is clearly never going to fire, then
runs the (idempotent) correlation finalizer for them. It is the crash-proof
counterpart to the in-memory countdown watchdog.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import text

from worker_python.celery_app import celery_app

logger = logging.getLogger(__name__)

# A run is a recovery candidate once its newest evidence row is older than this
# (seconds): long enough that the Tier-1/2 chord plus its callback should have
# completed, so a missing CORRELATION_COMPLETE means the callback was lost. Kept
# a little above the dashboard ACTIVE_WINDOW so we never pre-empt a sweep that is
# merely between two slow tools.
RECOVERY_QUIET_SECONDS = int(os.environ.get("RECOVERY_QUIET_SECONDS", "180"))

# Don't keep retrying ancient cases forever — only look back this far (hours).
RECOVERY_LOOKBACK_HOURS = int(os.environ.get("RECOVERY_LOOKBACK_HOURS", "24"))

# Cap how many stuck runs we finalize per tick so a backlog can't stampede the
# worker pool in a single sweep.
RECOVERY_MAX_PER_TICK = int(os.environ.get("RECOVERY_MAX_PER_TICK", "5"))


@celery_app.task(name="pipeline.recover_stuck_correlations")
def recover_stuck_correlations() -> dict:
    """Find runs that swept evidence but never correlated, and finalize them.

    Durable, idempotent recovery: ``finalize_correlation`` itself re-checks for a
    ``CORRELATION_COMPLETE`` event and no-ops if correlation already happened, so
    running this on a healthy run is harmless.
    """
    from api.db.postgres import session_scope
    from worker_python.tasks.tier2_tasks import finalize_correlation

    try:
        with session_scope() as session:
            # Candidate runs: have a dispatch/creation event and at least one
            # evidence row, the newest evidence is older than the quiet window,
            # and no CORRELATION_COMPLETE has ever been logged for the run.
            rows = session.execute(
                text(
                    """
                    SELECT c.case_id,
                           a.run_id,
                           MAX(e.timestamp_collected) AS last_ev
                    FROM cases c
                    JOIN audit_log a
                      ON a.case_id = c.case_id
                     AND a.event_type = 'CASE_CREATED'
                    JOIN evidence_units e
                      ON e.case_id = c.case_id
                    WHERE c.created_at > now() - make_interval(hours => :lookback)
                    GROUP BY c.case_id, a.run_id
                    HAVING MAX(e.timestamp_collected) < now() - make_interval(secs => :quiet)
                       AND NOT EXISTS (
                           SELECT 1 FROM audit_log d
                           WHERE d.run_id = a.run_id
                             AND d.event_type = 'CORRELATION_COMPLETE'
                       )
                    ORDER BY last_ev DESC
                    LIMIT :cap
                    """
                ),
                {
                    "lookback": RECOVERY_LOOKBACK_HOURS,
                    "quiet": RECOVERY_QUIET_SECONDS,
                    "cap": RECOVERY_MAX_PER_TICK,
                },
            ).all()
    except Exception as exc:  # noqa: BLE001 — recovery must never crash the worker
        logger.warning("recovery sweep query failed: %s", exc)
        return {"checked": 0, "recovered": 0}

    recovered = 0
    for case_id, run_id, _last_ev in rows:
        logger.warning(
            "recovery: run %s (case %s) swept evidence but never correlated — "
            "dispatching durable finalizer",
            run_id, case_id,
        )
        try:
            # Hand off to the worker pool so a slow correlation never stalls the
            # beat tick; finalize_correlation is idempotent (re-checks the audit
            # log) and uses 'system' as the actor for the recovered run.
            finalize_correlation.delay(str(case_id), str(run_id), "system")
            recovered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovery dispatch failed for run %s: %s", run_id, exc)

    if recovered:
        logger.info("recovery sweep dispatched %d stuck-run finalizer(s)", recovered)
    return {"checked": len(rows), "recovered": recovered}
