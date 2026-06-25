"""/api/v1/pipeline — per-tier, per-tool execution status from evidence + audit.

The status view is reconstructed purely from the durable record (``evidence_units``
rows, their status markers, and ``audit_log`` lifecycle events) rather than from
Celery task state, so it stays accurate across restarts and never depends on the
broker. The lifecycle state machine is **activity-driven**: a scan is "running"
while fresh evidence keeps landing (or while it is still awaiting its correlation
callback / watchdog), and only "complete" once everything — the Tier 1/2 sweep,
correlation, Tier 4 enrichment, the recursive pivot loop, and Tier 3 passive
recon — has gone quiet. This matters because correlation fires *early* (right
after the Tier 1/2 chord) while pivots, enrichment, and Tor-routed dorking keep
producing evidence for minutes afterward; treating ``CORRELATION_COMPLETE`` as
"finished" would freeze the timer long before the scan actually ends.
"""
from __future__ import annotations

import os
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.postgres import get_db

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])

# Static tool→tier registry used to bucket status output (display order).
TIER_TOOLS = {
    1: ["blackbird", "whatsmyname", "zehef", "socialscan", "hashtray",
        "phone_enrich", "ignorant", "phoneinfoga", "abstractapi_phone"],
    2: ["sherlock", "maigret", "nexfil", "social_analyzer", "tracer", "enola",
        "detectdee", "holehe", "h8mail", "mailcat", "eyes", "mailsleuth",
        "ghunt", "email2whatsapp", "xposedornot", "hudsonrock", "proxynova", "intelx", "hunterio",
        "emailrep", "epieos"],
    3: ["dorks_eye", "dorksint", "waybackurls", "huntpastebin", "forum_sweep", "ahmia"],
    4: ["toutatis", "medor", "snapintel", "telegram_intel",
        "tiktok_userdata", "mastosint", "osintssky", "osintchan",
        "proton_intel", "linkedin2username", "theharvester", "finalrecon",
        "webdiver", "github_api", "sublist3r", "dnstwist", "virustotal", "shodan", "hunterio",
        "censys", "dnsdumpster", "reddit_intel"],
    # SDM (Social Depth Module) — fires after Tier 4 for confirmed accounts.
    5: ["sdm_profile_hydration", "sdm_photo_intelligence",
        "sdm_behavioral_fingerprint", "sdm_network_extractor",
        "sdm_community_membership"],
}

# Which seed types make each non-triggered tool *expected* to run for a case.
# (Tier 4 tools are triggered on confirmed hits, so they are never in the
# baseline expected set — they only count once they have actually executed.)
_USERNAME_TOOLS = {"blackbird", "whatsmyname", "sherlock", "maigret", "nexfil",
                   "social_analyzer", "tracer", "enola", "detectdee",
                   "hudsonrock", "proxynova", "intelx"}
_EMAIL_TOOLS = {"zehef", "socialscan", "hashtray", "holehe", "h8mail", "mailcat",
                "eyes", "mailsleuth", "ghunt", "email2whatsapp",
                "xposedornot", "hudsonrock", "proxynova", "intelx", "hunterio",
                "emailrep", "epieos"}
_PHONE_TOOLS = {"phone_enrich", "ignorant", "phoneinfoga", "abstractapi_phone"}
_PASSIVE_TOOLS = {"dorks_eye", "dorksint", "waybackurls", "huntpastebin", "forum_sweep", "ahmia"}
_TIER4_TOOLS = set(TIER_TOOLS[4])
_TOOL_TIER = {t: tier for tier, tools in TIER_TOOLS.items() for t in tools}

# Seconds of quiet (no new evidence) before an active scan is judged finished.
# Generous enough to bridge the gaps the pipeline legitimately produces: Tier 3
# Tor dorks (30s cooldown + slow exits), the spin-up between pivot hops, and
# Tier 4 enrichment dispatch — so the state never flickers running↔complete.
ACTIVE_WINDOW = int(os.environ.get("PIPELINE_ACTIVE_WINDOW_SECONDS", "90"))
# The correlation watchdog can defer the whole post-sweep stage; until it has
# had a chance to fire, a quiet case is still "working", not finished.
WATCHDOG_WINDOW = int(os.environ.get("CORRELATION_WATCHDOG_SECONDS", "540"))

_PHASE_LABELS = {
    "queued": "Queued — dispatching tools",
    "sweeping": "Sweeping — Tier 1/2/3 tools collecting",
    "analysing": "Analysing — correlating & enriching",
    "pivoting": "Pivoting — expanding discovered identifiers",
    "complete": "Complete",
    "idle": "Idle — no activity",
    "failed": "Dispatch failed",
}


def _expected_tools(seed_types: set[str]) -> set[str]:
    """Baseline set of tools that *will* run for a case, from its seed types.

    Mirrors the header composition in ``celery_app._build_header``: an email seed
    runs the email *and* username chains; username/profile_url runs the username
    chain; phone runs the phone chain; any seed fires Tier 3 passive recon.
    """
    expected: set[str] = set()
    has_username = bool({"username", "profile_url"} & seed_types)
    if "email" in seed_types:
        expected |= _EMAIL_TOOLS | _USERNAME_TOOLS  # email header runs username too
    if has_username:
        expected |= _USERNAME_TOOLS
    if "phone" in seed_types:
        expected |= _PHONE_TOOLS
    if seed_types:
        expected |= _PASSIVE_TOOLS
    return expected


def _case_seed_types(session: Session, case_id: UUID) -> set[str]:
    """Resolve the dispatch seed types for a case (case_seeds → cases fallback)."""
    seed_types: set[str] = set()
    try:
        rows = session.execute(
            text("SELECT DISTINCT dispatch_type FROM case_seeds WHERE case_id = :cid"),
            {"cid": str(case_id)},
        ).all()
        seed_types = {r[0] for r in rows if r[0]}
    except Exception:  # noqa: BLE001 — case_seeds may be absent on legacy DBs
        seed_types = set()
    if not seed_types:
        row = session.execute(
            text("SELECT seed_type FROM cases WHERE case_id = :cid"),
            {"cid": str(case_id)},
        ).first()
        if row and row[0]:
            seed_types = {row[0]}
    return seed_types


def _compute_lifecycle(
    *, started_at, last_activity_at, correlation_at, last_pivot_at,
    pivot_hops: int, dispatch_failed: bool, total_hits: int, server_now,
) -> dict:
    """Pure lifecycle state machine (no DB) — state / phase / finished_at / elapsed.

    Activity-driven and watchdog-aware so the live view never freezes mid-scan: a
    scan is "running" while fresh evidence keeps landing *or* while it is still
    awaiting its correlation callback (the watchdog window), and only "complete"
    once everything has genuinely gone quiet. The finish time is the latest of
    the last evidence row and the correlation/pivot events, so the reported total
    duration reflects the real end of work — not the early correlation event.
    """
    def _secs(a, b):
        return (a - b).total_seconds() if a and b else None

    correlation_done = correlation_at is not None
    since_activity = _secs(server_now, last_activity_at)
    since_start = _secs(server_now, started_at)
    fresh = since_activity is not None and since_activity <= ACTIVE_WINDOW

    # --- state ---------------------------------------------------------------
    if not started_at:
        state = "idle"
    elif dispatch_failed and total_hits == 0:
        state = "failed"
    elif fresh:
        # Evidence landed within the quiet window → genuinely still working.
        state = "running"
    elif (not correlation_done and since_start is not None
          and since_start <= WATCHDOG_WINDOW + ACTIVE_WINDOW):
        # Sweep is quiet but correlation (or its watchdog) has not fired yet —
        # the post-sweep stage is still pending, so the scan is not finished.
        state = "running"
    elif correlation_done or total_hits > 0:
        state = "complete"
    elif since_start is not None and since_start <= ACTIVE_WINDOW:
        state = "running"          # just dispatched; tools spinning up
    else:
        state = "idle"

    # --- phase (finer-grained, for the UI) -----------------------------------
    if state == "failed":
        phase = "failed"
    elif state == "idle":
        phase = "idle"
    elif state == "complete":
        phase = "complete"
    elif last_activity_at is None:
        phase = "queued"
    elif not correlation_done:
        phase = "sweeping"
    elif pivot_hops > 0:
        phase = "pivoting"
    else:
        phase = "analysing"

    # --- duration ------------------------------------------------------------
    end_candidates = [t for t in (last_activity_at, correlation_at, last_pivot_at) if t]
    finished_candidate = max(end_candidates) if end_candidates else None
    if state == "running":
        elapsed_seconds = since_start or 0.0
        finished_at = None
    elif state in ("complete", "idle", "failed"):
        finished_at = finished_candidate if state == "complete" else last_activity_at
        elapsed_seconds = _secs(finished_at or started_at, started_at) or 0.0
    else:
        elapsed_seconds = 0.0
        finished_at = None

    return {
        "state": state,
        "phase": phase,
        "correlation_done": correlation_done,
        "fresh": fresh,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed_seconds,
    }



@router.get("/status/{case_id}")
def pipeline_status(case_id: UUID, session: Session = Depends(get_db)) -> dict:
    """Aggregate evidence + audit data into an accurate live scan-status view."""
    rows = session.execute(
        text(
            "SELECT tool_name, tool_tier, COUNT(*) AS hits "
            "FROM evidence_units WHERE case_id = :cid "
            "AND result_type NOT IN ('unavailable','blocked') "
            "GROUP BY tool_name, tool_tier"
        ),
        {"cid": str(case_id)},
    ).mappings().all()

    hits_by_tool = {r["tool_name"]: int(r["hits"]) for r in rows}

    # Tools that ran but produced no positive hit persist a single
    # 'unavailable'/'blocked' status marker (see FallbackChainManager). These are
    # the authoritative record that a tool executed — far more reliable than the
    # best-effort audit log — so a tool that ran-empty is distinguishable from one
    # that never ran. Audit TOOL_SKIPPED events are honoured as a fallback for
    # older runs that predate the marker behaviour.
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

    # A tool is "recorded" once it has either produced a hit or a status marker.
    recorded_tools = set(hits_by_tool) | skipped_tools

    # Expected = the tools the case's seed types guarantee will run, plus any
    # tool that has actually executed (covers pivot-triggered email/domain tools
    # and fired Tier 4 enrichment). This is the honest progress denominator: it
    # excludes tools that never apply (e.g. email tools on a username-only case)
    # so progress can actually reach 100%, while still counting everything that
    # genuinely ran.
    seed_types = _case_seed_types(session, case_id)
    expected_tools = _expected_tools(seed_types) | recorded_tools

    def tool_status(tool: str) -> dict:
        applicable = tool in expected_tools
        if tool in hits_by_tool:
            status = "done"
        elif tool in skipped_tools:
            status = "skipped"
        elif applicable:
            status = "pending"          # will run / running now
        else:
            status = "waiting"          # triggered tool, not fired (or n/a seed type)
        return {
            "tool": tool,
            "status": status,
            "hits": hits_by_tool.get(tool, 0),
            "applicable": applicable,
            "triggered": tool in _TIER4_TOOLS and tool in recorded_tools,
        }

    response = {
        f"tier{t}": [tool_status(tool) for tool in tools]
        for t, tools in TIER_TOOLS.items()
    }

    total_hits = int(sum(hits_by_tool.values()))
    preservation_complete = session.execute(
        text("SELECT COUNT(*) FROM evidence_units WHERE case_id = :cid AND snapshot_hash IS NOT NULL"),
        {"cid": str(case_id)},
    ).scalar_one()
    high_links = session.execute(
        text("SELECT COUNT(*) FROM identity_links WHERE case_id = :cid AND confidence_tier = 'HIGH'"),
        {"cid": str(case_id)},
    ).scalar_one()

    # --- lifecycle signals -----------------------------------------------------
    timing = session.execute(
        text(
            """
            SELECT
                (SELECT created_at FROM cases WHERE case_id = :cid) AS started_at,
                (SELECT MAX(timestamp_collected) FROM evidence_units
                    WHERE case_id = :cid) AS last_activity_at,
                (SELECT MAX(created_at) FROM audit_log WHERE case_id = :cid
                    AND event_type = 'CORRELATION_COMPLETE') AS correlation_at,
                (SELECT MAX(created_at) FROM audit_log WHERE case_id = :cid
                    AND event_type = 'PIVOT_CORRELATION_COMPLETE') AS last_pivot_at,
                (SELECT COUNT(*) FROM audit_log WHERE case_id = :cid
                    AND event_type = 'PIVOT_CORRELATION_COMPLETE') AS pivot_hops,
                (SELECT COUNT(*) FROM audit_log WHERE case_id = :cid
                    AND event_type = 'PIPELINE_DISPATCH_FAILED') AS dispatch_failed,
                now() AS server_now
            """
        ),
        {"cid": str(case_id)},
    ).mappings().first()

    started_at = timing["started_at"]
    last_activity_at = timing["last_activity_at"]
    correlation_at = timing["correlation_at"]
    last_pivot_at = timing["last_pivot_at"]
    pivot_hops = int(timing["pivot_hops"] or 0)
    dispatch_failed = int(timing["dispatch_failed"] or 0) > 0
    server_now = timing["server_now"]

    lifecycle = _compute_lifecycle(
        started_at=started_at,
        last_activity_at=last_activity_at,
        correlation_at=correlation_at,
        last_pivot_at=last_pivot_at,
        pivot_hops=pivot_hops,
        dispatch_failed=dispatch_failed,
        total_hits=total_hits,
        server_now=server_now,
    )
    state = lifecycle["state"]
    phase = lifecycle["phase"]
    correlation_done = lifecycle["correlation_done"]
    finished_at = lifecycle["finished_at"]
    elapsed_seconds = lifecycle["elapsed_seconds"]

    # Currently-active tool: the most recently collected evidence row, surfaced
    # only while the scan is live so the UI can show "currently: sherlock".
    active_tool = None
    if state == "running" and lifecycle["fresh"]:
        row = session.execute(
            text(
                "SELECT tool_name FROM evidence_units WHERE case_id = :cid "
                "ORDER BY timestamp_collected DESC LIMIT 1"
            ),
            {"cid": str(case_id)},
        ).first()
        active_tool = row[0] if row else None

    # --- progress over the EXPECTED tool set -----------------------------------
    expected_done = sum(1 for t in expected_tools if t in hits_by_tool)
    expected_skipped = sum(
        1 for t in expected_tools if t not in hits_by_tool and t in skipped_tools
    )
    tools_total = len(expected_tools)
    tools_pending = tools_total - expected_done - expected_skipped
    progress = (expected_done + expected_skipped) / tools_total if tools_total else 0.0
    if state == "complete":
        progress = 1.0

    # Tier 4 triggered-enrichment summary (reported separately from progress, as
    # it is opportunistic rather than guaranteed).
    enrichment_triggered = sum(1 for t in _TIER4_TOOLS if t in recorded_tools)
    enrichment_done = sum(1 for t in _TIER4_TOOLS if t in hits_by_tool)

    response.update(
        total_hits=total_hits,
        preservation_complete=int(preservation_complete),
        high_confidence_links=int(high_links),
        state=state,
        phase=phase,
        phase_label=_PHASE_LABELS.get(phase, phase),
        active_tool=active_tool,
        correlation_complete=correlation_done,
        pivot_hops=pivot_hops,
        seed_types=sorted(seed_types),
        started_at=started_at.isoformat() if started_at else None,
        last_activity_at=last_activity_at.isoformat() if last_activity_at else None,
        completed_at=correlation_at.isoformat() if correlation_at else None,
        finished_at=finished_at.isoformat() if finished_at else None,
        elapsed_seconds=round(elapsed_seconds, 1),
        tools_total=tools_total,
        tools_done=expected_done,
        tools_skipped=expected_skipped,
        tools_pending=tools_pending,
        progress=round(progress, 3),
        enrichment_triggered=enrichment_triggered,
        enrichment_done=enrichment_done,
    )
    return response
