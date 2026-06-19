"""Celery application for the Go-binary worker.

Shares the same Redis broker/backend as the Python worker and registers the
go_tasks module that wraps the compiled Go binaries.
"""
from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "socmint_go",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["worker_go.tasks.go_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # --- queue isolation (CRITICAL) ------------------------------------------
    # This worker registers ONLY ``go.run_adapter``. It must NEVER consume the
    # default "celery" queue shared with worker_python: a Celery worker that
    # receives a message for a task it doesn't know logs "Received unregistered
    # task ... discarded" and PERMANENTLY DESTROYS it. With both workers on the
    # default queue, Redis round-robined pipeline messages between them and
    # worker_go silently ate ~half of every scan's tier sweeps, chord callbacks
    # and recovery tasks (observed: username/phone sweeps + finalize_correlation
    # discarded → scans stuck with most tools "pending" forever). All go.* tasks
    # therefore live on a dedicated "go" queue, and the compose command starts
    # this worker with ``--queues go`` so it can never touch the default queue.
    task_default_queue="go",
    task_routes={"go.*": {"queue": "go"}},
)
