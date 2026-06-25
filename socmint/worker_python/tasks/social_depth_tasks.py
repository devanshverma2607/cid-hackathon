"""Social Depth Module — Celery tasks.

Five tasks that fire after Tier 4 enrichment for confirmed accounts:
  sdm.profile_hydration     — deep profile snapshot + diff tracking
  sdm.photo_intelligence    — reverse image search + photo change log
  sdm.behavioral_fingerprint — post collection + behavioral analysis
  sdm.network_extractor     — interaction graph + pivot seed emission
  sdm.community_membership  — public community memberships

All tasks:
  - Confirm case_id exists before doing any work (legal gate)
  - Return unavailable/blocked markers on failure (never raise)
  - Log start/completion/skip to audit_log
  - Gate on SDM_ENABLED env var
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import UUID

from worker_python.celery_app import celery_app
from worker_python.tasks._pipeline import preserve_and_persist

logger = logging.getLogger(__name__)

SDM_ENABLED = os.environ.get("SDM_ENABLED", "0").strip().lower() in ("1", "true", "yes")

# Platform → depth adapter class mapping
_DEPTH_ADAPTERS: dict[str, type] = {}


def _load_adapters() -> dict[str, type]:
    """Lazy-load adapter classes to avoid import-time failures."""
    if _DEPTH_ADAPTERS:
        return _DEPTH_ADAPTERS
    try:
        from worker_python.adapters.depth.github_depth import GitHubDepthAdapter
        from worker_python.adapters.depth.reddit_depth import RedditDepthAdapter
        from worker_python.adapters.depth.twitter_depth import TwitterDepthAdapter
        from worker_python.adapters.depth.mastodon_depth import MastodonDepthAdapter
        from worker_python.adapters.depth.bluesky_depth import BlueskyDepthAdapter
        from worker_python.adapters.depth.other_platforms import (
            InstagramDepthAdapter, TikTokDepthAdapter,
            LinkedInDepthAdapter, TelegramDepthAdapter, YouTubeDepthAdapter,
        )
        _DEPTH_ADAPTERS.update({
            "github": GitHubDepthAdapter,
            "reddit": RedditDepthAdapter,
            "twitter": TwitterDepthAdapter, "x": TwitterDepthAdapter,
            "mastodon": MastodonDepthAdapter,
            "bluesky": BlueskyDepthAdapter,
            "instagram": InstagramDepthAdapter,
            "tiktok": TikTokDepthAdapter,
            "linkedin": LinkedInDepthAdapter,
            "telegram": TelegramDepthAdapter,
            "youtube": YouTubeDepthAdapter,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("SDM adapter import failed: %s", exc)
    return _DEPTH_ADAPTERS


def _case_exists(case_id: str) -> bool:
    """Confirm case_id exists in the cases table (legal gate check)."""
    try:
        from sqlalchemy import text
        from api.db.postgres import session_scope
        with session_scope() as session:
            row = session.execute(
                text("SELECT 1 FROM cases WHERE case_id = :c LIMIT 1"),
                {"c": case_id},
            ).first()
            return row is not None
    except Exception:  # noqa: BLE001
        return False


def _audit(case_id: str, run_id: str, analyst_id: str, event_type: str, metadata: dict) -> None:
    """Log an SDM event to audit_log."""
    try:
        from api.db.postgres import session_scope
        from api.services.provenance import ProvenanceService
        prov = ProvenanceService()
        with session_scope() as session:
            prov.log_audit_event(
                case_id=UUID(case_id), run_id=UUID(run_id),
                event_type=event_type, actor_id=analyst_id,
                metadata=metadata, session=session,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("SDM audit log failed: %s", exc)


def _make_sdm_evidence(tool_name, platform, case_id, run_id, analyst_id,
                       result_type, result_value, confidence_raw=0.50,
                       enrichment=None, notes=None):
    """Build an EvidenceUnit for SDM output."""
    from api.models.evidence import EvidenceUnit
    from uuid import uuid4
    return EvidenceUnit(
        evidence_id=uuid4(), case_id=UUID(case_id), run_id=UUID(run_id),
        tool_name=tool_name, tool_version="sdm-1.0", tool_tier=4,
        source_platform=platform, source_tier=4,
        seed_type="username", seed_value=result_value,
        result_type=result_type, result_value=result_value,
        confidence_raw=confidence_raw, platform_enrichment=enrichment,
        analyst_id=analyst_id, notes=notes,
    )


# ---- Task 1: Profile Hydration --------------------------------------------
@celery_app.task(name="sdm.profile_hydration")
def run_profile_hydration(
    platform: str, url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> dict:
    """Deep profile snapshot + diff tracking."""
    if not SDM_ENABLED:
        return {"skipped": True, "reason": "sdm_disabled"}
    if not _case_exists(case_id):
        return {"skipped": True, "reason": "case_not_found"}

    _audit(case_id, run_id, analyst_id, "SDM_HYDRATION_START",
           {"platform": platform, "username": username})

    adapters = _load_adapters()
    adapter_cls = adapters.get(platform.lower())
    if not adapter_cls:
        _audit(case_id, run_id, analyst_id, "SDM_HYDRATION_SKIP",
               {"platform": platform, "reason": "no_adapter"})
        return {"skipped": True, "reason": "no_adapter"}

    try:
        adapter = adapter_cls()
        snapshot = adapter.hydrate(username, url)
        if not snapshot:
            _audit(case_id, run_id, analyst_id, "SDM_HYDRATION_SKIP",
                   {"platform": platform, "reason": "unavailable"})
            return {"status": "unavailable", "platform": platform}

        # Store snapshot with timestamp
        snapshot["_collected_at"] = datetime.now(timezone.utc).isoformat()
        enrichment = {"profile_snapshot": snapshot}

        # Check for prior snapshot and compute diff
        try:
            from sqlalchemy import text
            from api.db.postgres import session_scope
            with session_scope() as session:
                row = session.execute(
                    text(
                        "SELECT platform_enrichment FROM evidence_units "
                        "WHERE case_id = :c AND source_platform = :p "
                        "AND platform_enrichment IS NOT NULL "
                        "ORDER BY timestamp_collected DESC LIMIT 1"
                    ),
                    {"c": case_id, "p": platform},
                ).first()
                if row and row[0] and isinstance(row[0], dict):
                    prior = row[0].get("profile_snapshot")
                    if prior and isinstance(prior, dict):
                        diff = _compute_profile_diff(prior, snapshot)
                        if diff:
                            enrichment["profile_diff"] = diff
                            # Emit profile_change_detected evidence
                            for field, change in diff.items():
                                unit = _make_sdm_evidence(
                                    "sdm_profile_hydration", platform,
                                    case_id, run_id, analyst_id,
                                    "profile_change_detected", url,
                                    confidence_raw=0.85,
                                    enrichment={"changed_field": field, **change},
                                )
                                preserve_and_persist([unit])
        except Exception as exc:  # noqa: BLE001
            logger.debug("profile diff check failed: %s", exc)

        # Store the hydration evidence
        unit = _make_sdm_evidence(
            "sdm_profile_hydration", platform, case_id, run_id, analyst_id,
            "account_found", url, confidence_raw=0.85, enrichment=enrichment,
        )
        preserve_and_persist([unit])

        _audit(case_id, run_id, analyst_id, "SDM_HYDRATION_COMPLETE",
               {"platform": platform, "fields": len(snapshot)})
        return {"status": "ok", "platform": platform, "fields": len(snapshot)}

    except Exception as exc:  # noqa: BLE001
        logger.warning("SDM hydration failed for %s/%s: %s", platform, username, exc)
        _audit(case_id, run_id, analyst_id, "SDM_HYDRATION_SKIP",
               {"platform": platform, "reason": str(exc)[:200]})
        return {"status": "error", "platform": platform, "error": str(exc)[:200]}


def _compute_profile_diff(prior: dict, current: dict) -> dict:
    """Compare two profile snapshots and return changed fields."""
    diff = {}
    check_fields = [
        "display_name", "bio", "bio_link", "location", "website",
        "follower_count", "following_count", "post_count", "verified",
    ]
    for field in check_fields:
        old_val = prior.get(field)
        new_val = current.get(field)
        if old_val != new_val and (old_val or new_val):
            diff[field] = {"old": old_val, "new": new_val}
    return diff


# ---- Task 2: Photo Intelligence -------------------------------------------
@celery_app.task(name="sdm.photo_intelligence")
def run_photo_intelligence(
    platform: str, url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> dict:
    """Reverse image search + photo change log (Wayback)."""
    if not SDM_ENABLED:
        return {"skipped": True, "reason": "sdm_disabled"}
    if not _case_exists(case_id):
        return {"skipped": True, "reason": "case_not_found"}

    _audit(case_id, run_id, analyst_id, "SDM_PHOTO_START",
           {"platform": platform})

    # Photo intelligence runs on the avatar URL from enrichment
    try:
        from sqlalchemy import text
        from api.db.postgres import session_scope
        with session_scope() as session:
            row = session.execute(
                text(
                    "SELECT platform_enrichment FROM evidence_units "
                    "WHERE case_id = :c AND source_platform = :p "
                    "AND platform_enrichment IS NOT NULL "
                    "ORDER BY timestamp_collected DESC LIMIT 1"
                ),
                {"c": case_id, "p": platform},
            ).first()

        if not row or not row[0]:
            return {"status": "no_enrichment"}

        enrichment = row[0] if isinstance(row[0], dict) else {}
        snapshot = enrichment.get("profile_snapshot", enrichment)
        avatar_url = snapshot.get("avatar_url") or snapshot.get("profile_pic_url") or ""

        if not avatar_url:
            return {"status": "no_avatar"}

        # Stage 1: URL fingerprinting
        photo_url_id = _extract_photo_url_id(avatar_url)
        result_enrichment = {}
        if photo_url_id:
            result_enrichment["photo_url_id"] = photo_url_id

        _audit(case_id, run_id, analyst_id, "SDM_PHOTO_COMPLETE",
               {"platform": platform, "has_url_id": bool(photo_url_id)})

        if result_enrichment:
            unit = _make_sdm_evidence(
                "sdm_photo_intelligence", platform, case_id, run_id, analyst_id,
                "account_found", url, confidence_raw=0.70,
                enrichment=result_enrichment,
            )
            preserve_and_persist([unit])

        return {"status": "ok", "platform": platform, "photo_url_id": photo_url_id}

    except Exception as exc:  # noqa: BLE001
        logger.warning("SDM photo intelligence failed: %s", exc)
        return {"status": "error", "error": str(exc)[:200]}


def _extract_photo_url_id(url: str) -> str | None:
    """Extract stable photo ID from CDN URL patterns."""
    import re
    patterns = [
        r'pbs\.twimg\.com/profile_images/(\d+)',  # Twitter
        r'avatars\.githubusercontent\.com/u/(\d+)',  # GitHub
        r'instagram.*?/([a-zA-Z0-9_-]{20,})',  # Instagram media ID
        r'facebook.*?/(\d{10,})',  # Facebook numeric ID
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


# ---- Task 3: Behavioral Fingerprint ---------------------------------------
@celery_app.task(name="sdm.behavioral_fingerprint")
def run_behavioral_fingerprint(
    platform: str, url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> dict:
    """Collect posts + run behavioral analysis."""
    if not SDM_ENABLED:
        return {"skipped": True, "reason": "sdm_disabled"}
    if not _case_exists(case_id):
        return {"skipped": True, "reason": "case_not_found"}

    _audit(case_id, run_id, analyst_id, "SDM_BEHAVIORAL_START",
           {"platform": platform})

    adapters = _load_adapters()
    adapter_cls = adapters.get(platform.lower())
    if not adapter_cls:
        return {"skipped": True, "reason": "no_adapter"}

    try:
        adapter = adapter_cls()
        posts = adapter.collect_posts(username, url)
        if not posts:
            _audit(case_id, run_id, analyst_id, "SDM_BEHAVIORAL_SKIP",
                   {"platform": platform, "reason": "no_posts"})
            return {"status": "unavailable", "platform": platform}

        # Store post timestamps
        timestamps = [p["timestamp"] for p in posts if p.get("timestamp")]
        enrichment = {"post_timestamps": timestamps[:200]}

        # Emit post_timeline_collected evidence
        unit = _make_sdm_evidence(
            "sdm_behavioral_fingerprint", platform, case_id, run_id, analyst_id,
            "post_timeline_collected", url, confidence_raw=0.50,
            enrichment=enrichment,
        )
        preserve_and_persist([unit])

        # Run behavioral analysis
        from api.services.behavioral_engine import (
            compute_frequency, infer_timezone,
            detect_rhythm_breaks, detect_velocity_spikes,
        )

        freq = compute_frequency(timestamps)
        behavioral_enrichment = {"posting_frequency": freq}

        # Timezone inference
        if freq.get("sufficient") and freq.get("hour_histogram"):
            tz_result = infer_timezone(freq["hour_histogram"], freq["sample_size"])
            if tz_result:
                behavioral_enrichment["inferred_timezone"] = tz_result
                tz_unit = _make_sdm_evidence(
                    "sdm_behavioral_fingerprint", platform,
                    case_id, run_id, analyst_id,
                    "behavioral_insight", url,
                    confidence_raw=tz_result["confidence_raw"],
                    enrichment={"inferred_timezone": tz_result},
                    notes="[behavioral-inferred] Timezone inferred from posting pattern",
                )
                preserve_and_persist([tz_unit])

        # Rhythm breaks
        breaks = detect_rhythm_breaks(timestamps)
        if breaks:
            behavioral_enrichment["rhythm_breaks"] = breaks
            brk_unit = _make_sdm_evidence(
                "sdm_behavioral_fingerprint", platform,
                case_id, run_id, analyst_id,
                "behavioral_insight", url,
                confidence_raw=_BEHAVIORAL_MAX_CONFIDENCE,
                enrichment={"rhythm_breaks": breaks},
                notes="[behavioral-inferred] Activity rhythm breaks detected",
            )
            preserve_and_persist([brk_unit])

        # Velocity spikes
        spikes = detect_velocity_spikes(timestamps)
        if spikes:
            behavioral_enrichment["velocity_spikes"] = spikes
            spk_unit = _make_sdm_evidence(
                "sdm_behavioral_fingerprint", platform,
                case_id, run_id, analyst_id,
                "behavioral_insight", url,
                confidence_raw=_BEHAVIORAL_MAX_CONFIDENCE,
                enrichment={"velocity_spikes": spikes},
                notes="[behavioral-inferred] Posting velocity spikes detected",
            )
            preserve_and_persist([spk_unit])

        _audit(case_id, run_id, analyst_id, "SDM_BEHAVIORAL_COMPLETE",
               {"platform": platform, "posts": len(timestamps),
                "has_tz": "inferred_timezone" in behavioral_enrichment})

        return {"status": "ok", "platform": platform, "posts": len(timestamps)}

    except Exception as exc:  # noqa: BLE001
        logger.warning("SDM behavioral fingerprint failed: %s", exc)
        return {"status": "error", "error": str(exc)[:200]}


_BEHAVIORAL_MAX_CONFIDENCE = 0.40


# ---- Task 4: Network Extractor --------------------------------------------
@celery_app.task(name="sdm.network_extractor")
def run_network_extractor(
    platform: str, url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> dict:
    """Build interaction graph from public posts."""
    if not SDM_ENABLED:
        return {"skipped": True, "reason": "sdm_disabled"}
    if not _case_exists(case_id):
        return {"skipped": True, "reason": "case_not_found"}

    _audit(case_id, run_id, analyst_id, "SDM_NETWORK_START",
           {"platform": platform})

    adapters = _load_adapters()
    adapter_cls = adapters.get(platform.lower())
    if not adapter_cls:
        return {"skipped": True, "reason": "no_adapter"}

    try:
        adapter = adapter_cls()
        interactions = adapter.collect_interactions(username, url)
        if not interactions:
            return {"status": "unavailable", "platform": platform}

        from worker_python.adapters.depth.network_extractor import (
            build_interaction_graph, extract_pivot_seeds,
        )

        graph = build_interaction_graph(interactions, platform, url, case_id)
        enrichment = {"interaction_graph": graph}

        unit = _make_sdm_evidence(
            "sdm_network_extractor", platform, case_id, run_id, analyst_id,
            "account_found", url, confidence_raw=0.50,
            enrichment=enrichment,
        )
        preserve_and_persist([unit])

        # Feed pivot seeds for top interaction targets
        seeds = extract_pivot_seeds(interactions, platform)
        if seeds:
            try:
                from api.services.pivot_engine import PivotEngine, PivotSeed
                pe = PivotEngine()
                pivot_seeds = [
                    PivotSeed(
                        seed_type=s["seed_type"], seed_value=s["seed_value"],
                        via_tool=s["via_tool"], via_platform=s["via_platform"],
                        source_value=s["source_value"],
                    )
                    for s in seeds
                ]
                pe.mark_processed(case_id, [])  # ensure visited set exists
                new = pe.select_new(case_id, pivot_seeds)
                if new:
                    logger.info("SDM network: %d new pivot seeds from %s", len(new), platform)
            except Exception as exc:  # noqa: BLE001
                logger.debug("SDM pivot seed emission failed: %s", exc)

        _audit(case_id, run_id, analyst_id, "SDM_NETWORK_COMPLETE",
               {"platform": platform, "targets": len(graph)})
        return {"status": "ok", "platform": platform, "targets": len(graph)}

    except Exception as exc:  # noqa: BLE001
        logger.warning("SDM network extractor failed: %s", exc)
        return {"status": "error", "error": str(exc)[:200]}


# ---- Task 5: Community Membership -----------------------------------------
@celery_app.task(name="sdm.community_membership")
def run_community_membership(
    platform: str, url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> dict:
    """Collect public community memberships."""
    if not SDM_ENABLED:
        return {"skipped": True, "reason": "sdm_disabled"}
    if not _case_exists(case_id):
        return {"skipped": True, "reason": "case_not_found"}

    _audit(case_id, run_id, analyst_id, "SDM_COMMUNITY_START",
           {"platform": platform})

    adapters = _load_adapters()
    adapter_cls = adapters.get(platform.lower())
    if not adapter_cls:
        return {"skipped": True, "reason": "no_adapter"}

    try:
        adapter = adapter_cls()
        communities = adapter.collect_communities(username, url)
        if not communities:
            return {"status": "unavailable", "platform": platform}

        enrichment = {"community_memberships": communities}

        for comm in communities:
            unit = _make_sdm_evidence(
                "sdm_community_membership", platform,
                case_id, run_id, analyst_id,
                "community_membership_found", comm.get("name", ""),
                confidence_raw=0.50,
                enrichment={"community": comm},
            )
            preserve_and_persist([unit])

        _audit(case_id, run_id, analyst_id, "SDM_COMMUNITY_COMPLETE",
               {"platform": platform, "count": len(communities)})
        return {"status": "ok", "platform": platform, "count": len(communities)}

    except Exception as exc:  # noqa: BLE001
        logger.warning("SDM community membership failed: %s", exc)
        return {"status": "error", "error": str(exc)[:200]}


# ---- Dispatch helper (called from tier2_tasks.aggregate_results) -----------
def dispatch_sdm_for_platform(
    platform: str, url: str, username: str,
    case_id: str, run_id: str, analyst_id: str,
) -> int:
    """Fire all SDM tasks for a confirmed platform. Returns count dispatched."""
    if not SDM_ENABLED:
        return 0
    dispatched = 0
    for task_fn in (
        run_profile_hydration,
        run_photo_intelligence,
        run_behavioral_fingerprint,
        run_network_extractor,
        run_community_membership,
    ):
        try:
            task_fn.delay(platform, url, username, case_id, run_id, analyst_id)
            dispatched += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("SDM dispatch failed for %s/%s: %s", platform, task_fn.name, exc)
    return dispatched
