"""/api/v1/pipeline — per-tier, per-tool execution status from the audit log."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.postgres import get_db
from api.models.user import UserOut
from api.services.auth import get_current_user

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])

# Static tool→tier registry used to bucket status output.
TIER_TOOLS = {
    1: ["blackbird", "whatsmyname", "zehef", "socialscan", "hashtray", "ignorant"],
    2: ["sherlock", "maigret", "nexfil", "social_analyzer", "tracer", "enola",
        "detectdee", "holehe", "h8mail", "mailcat", "eyes", "mailsleuth",
        "ghunt", "email2whatsapp"],
    3: ["dorks_eye", "dorksint", "waybackurls", "huntpastebin"],
    4: ["toutatis", "medor", "snapintel", "telegram_intel",
        "tiktok_userdata", "mastosint", "osintssky", "osintchan",
        "proton_intel", "linkedin2username", "theharvester", "finalrecon",
        "webdiver", "github_api", "sublist3r", "dnstwist"],
}


@router.get("/status/{case_id}")
def pipeline_status(
    case_id: UUID,
    _user: UserOut = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> dict:
    """Aggregate evidence + audit data into a per-tier, per-tool status view."""
    rows = session.execute(
        text(
            "SELECT tool_name, tool_tier, COUNT(*) AS hits "
            "FROM evidence_units WHERE case_id = :cid "
            "AND result_type NOT IN ('unavailable','blocked') "
            "GROUP BY tool_name, tool_tier"
        ),
        {"cid": str(case_id)},
    ).mappings().all()

    hits_by_tool = {r["tool_name"]: r["hits"] for r in rows}

    # Tools that ran but produced no positive hit persist a single
    # 'unavailable'/'blocked' status marker (see FallbackChainManager). These
    # are the authoritative record that a tool executed — far more reliable than
    # the best-effort audit log — so a tool that ran-empty is distinguishable
    # from one that never ran. Audit TOOL_SKIPPED events are still honoured as a
    # fallback for older runs that predate the marker behaviour.
    ran_empty = session.execute(
        text(
            "SELECT DISTINCT tool_name FROM evidence_units "
            "WHERE case_id = :cid AND result_type IN ('unavailable','blocked')"
        ),
        {"cid": str(case_id)},
    ).mappings().all()
    ran_empty_tools = {r["tool_name"] for r in ran_empty if r["tool_name"]}

    skipped = session.execute(
        text(
            "SELECT event_metadata->>'tool' AS tool FROM audit_log "
            "WHERE case_id = :cid AND event_type = 'TOOL_SKIPPED'"
        ),
        {"cid": str(case_id)},
    ).mappings().all()
    skipped_tools = {r["tool"] for r in skipped if r["tool"]} | ran_empty_tools

    def tool_status(tool: str) -> dict:
        if tool in hits_by_tool:
            return {"tool": tool, "status": "done", "hits": int(hits_by_tool[tool])}
        if tool in skipped_tools:
            return {"tool": tool, "status": "skipped", "hits": 0}
        return {"tool": tool, "status": "pending", "hits": 0}

    response = {f"tier{t}": [tool_status(tool) for tool in tools] for t, tools in TIER_TOOLS.items()}

    total_hits = int(sum(hits_by_tool.values()))
    preservation_complete = session.execute(
        text("SELECT COUNT(*) FROM evidence_units WHERE case_id = :cid AND snapshot_hash IS NOT NULL"),
        {"cid": str(case_id)},
    ).scalar_one()
    high_links = session.execute(
        text("SELECT COUNT(*) FROM identity_links WHERE case_id = :cid AND confidence_tier = 'HIGH'"),
        {"cid": str(case_id)},
    ).scalar_one()

    # --- lifecycle + duration --------------------------------------------------
    # Scan starts when the case is created (the pipeline dispatches immediately),
    # stays "running" while fresh evidence keeps landing, and is "complete" once
    # the correlation/pivot terminal events fire or activity goes quiet.
    timing = session.execute(
        text(
            """
            SELECT
                (SELECT created_at FROM cases WHERE case_id = :cid) AS started_at,
                (SELECT MAX(timestamp_collected) FROM evidence_units
                    WHERE case_id = :cid) AS last_activity_at,
                (SELECT MAX(created_at) FROM audit_log WHERE case_id = :cid
                    AND event_type IN ('CORRELATION_COMPLETE',
                                       'PIVOT_CORRELATION_COMPLETE')) AS completed_at,
                now() AS server_now
            """
        ),
        {"cid": str(case_id)},
    ).mappings().first()

    started_at = timing["started_at"]
    last_activity_at = timing["last_activity_at"]
    completed_at = timing["completed_at"]
    server_now = timing["server_now"]

    def _secs(a, b):
        return (a - b).total_seconds() if a and b else None

    ACTIVE_WINDOW = 30  # seconds of quiet before a run is no longer "running"

    state = "idle"
    elapsed_seconds = 0.0
    if started_at:
        since_activity = _secs(server_now, last_activity_at)
        since_start = _secs(server_now, started_at)
        if since_activity is not None and since_activity <= ACTIVE_WINDOW:
            state = "running"
        elif completed_at is not None:
            state = "complete"
        elif since_start is not None and since_start <= ACTIVE_WINDOW:
            state = "running"
        elif total_hits > 0:
            state = "complete"
        else:
            state = "idle"

        if state == "running":
            elapsed_seconds = since_start or 0.0
        else:
            # Duration of actual scanning = start → last evidence collected.
            # (Prefer last activity over the formal completion event, whose
            # chord callback can fire minutes late without new collection.)
            end = last_activity_at or completed_at or started_at
            elapsed_seconds = _secs(end, started_at) or 0.0

    # --- tool progress ---------------------------------------------------------
    all_tools = [t for tools in TIER_TOOLS.values() for t in tools]
    tools_total = len(all_tools)
    tools_done = sum(1 for t in all_tools if t in hits_by_tool)
    tools_skipped = sum(1 for t in all_tools if t not in hits_by_tool and t in skipped_tools)
    tools_pending = tools_total - tools_done - tools_skipped
    progress = (tools_done + tools_skipped) / tools_total if tools_total else 0.0

    response.update(
        total_hits=total_hits,
        preservation_complete=int(preservation_complete),
        high_confidence_links=int(high_links),
        state=state,
        started_at=started_at.isoformat() if started_at else None,
        last_activity_at=last_activity_at.isoformat() if last_activity_at else None,
        completed_at=completed_at.isoformat() if completed_at else None,
        elapsed_seconds=round(elapsed_seconds, 1),
        tools_total=tools_total,
        tools_done=tools_done,
        tools_skipped=tools_skipped,
        tools_pending=tools_pending,
        progress=round(progress, 3),
    )
    return response
