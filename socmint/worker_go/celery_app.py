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
)
