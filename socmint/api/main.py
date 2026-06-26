"""FastAPI application entry point for the SOCMINT API gateway."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


from api.config import get_settings
from api.routers import auth, cases, dossier, evidence, google_auth, graph, insights, persona, pipeline, reports

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Lifespan: runs once on startup (seed bootstrap admin)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    # Seed the bootstrap admin when the users table is empty.
    try:
        from api.db.postgres import get_session_factory
        from api.services.auth import seed_admin
        session = get_session_factory()()
        try:
            seed_admin(session)
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Admin seed skipped: %s", exc)
    yield  # application runs here

app = FastAPI(
    title="SOCMINT — Suspect Profiling System",
    version="2.0",
    description="OSINT pipeline: discover, correlate, preserve, and report linked identities.",
    lifespan=lifespan,
)
# ---------------------------------------------------------------------------
# CORS — the Streamlit dashboard (:8501) makes cross-origin requests to
# the API (:8000).  Allow all origins in development; tighten for production.
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)       # /api/v1/auth — register, login, me
app.include_router(google_auth.router) # /api/v1/auth/google — OAuth login
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
