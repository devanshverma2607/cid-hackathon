"""Celery tasks wrapping the Go-binary adapters."""
from __future__ import annotations

import logging
from uuid import UUID

from worker_go.celery_app import celery_app
from worker_go.adapters.enola import EnolaAdapter
from worker_go.adapters.detectdee import DetectDeeAdapter
from worker_go.adapters.mailsleuth import MailsleuthAdapter
from worker_go.adapters.email2whatsapp import Email2WhatsAppAdapter
from worker_go.adapters.githound import GitHoundAdapter

logger = logging.getLogger(__name__)

_ADAPTERS = {
    "enola": EnolaAdapter,
    "detectdee": DetectDeeAdapter,
    "mailsleuth": MailsleuthAdapter,
    "email2whatsapp": Email2WhatsAppAdapter,
    "githound": GitHoundAdapter,
}


@celery_app.task(name="go.run_adapter")
def run_go_adapter(
    adapter_name: str, seed: str, case_id: str, run_id: str, analyst_id: str, seed_type: str = "username"
) -> dict:
    """Run a single Go-binary adapter and persist its evidence."""
    from worker_python.tasks._pipeline import preserve_and_persist

    adapter_cls = _ADAPTERS.get(adapter_name)
    if adapter_cls is None:
        return {"adapter": adapter_name, "error": "unknown adapter", "hits": 0}

    adapter = adapter_cls()
    units = adapter.execute(seed, UUID(case_id), UUID(run_id), analyst_id, seed_type)
    positives = [u for u in units if u.result_type not in ("unavailable", "blocked")]
    count = preserve_and_persist(positives)
    return {"adapter": adapter_name, "hits": count}
