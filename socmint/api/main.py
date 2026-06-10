"""FastAPI application entry point for the SOCMINT API gateway."""
from __future__ import annotations

import logging

from fastapi import FastAPI

from api.config import get_settings
from api.routers import cases, dossier, evidence, graph, insights, persona, pipeline, reports

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

app = FastAPI(
    title="SOCMINT — Suspect Profiling System",
    version="2.0",
    description="OSINT pipeline: discover, correlate, preserve, and report linked identities.",
)

app.include_router(cases.router)
app.include_router(pipeline.router)
app.include_router(evidence.router)
app.include_router(graph.router)
app.include_router(persona.router)
app.include_router(reports.router)
app.include_router(insights.router)
app.include_router(dossier.router)


@app.get("/")
def root() -> dict:
    """Service banner."""
    return {"service": "socmint-api", "version": "2.0", "docs": "/docs"}
