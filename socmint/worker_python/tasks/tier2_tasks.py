"""Tier 2 Celery tasks — deep sweep + chord callback (MODULE 3)."""
from __future__ import annotations

import logging
import re
from uuid import UUID

from worker_python.celery_app import celery_app
from worker_python.adapters.fallback_chain import FallbackChainManager, ChainExhaustedError
from worker_python.tasks._pipeline import preserve_and_persist

logger = logging.getLogger(__name__)


# Map a confirmed-account URL host to a Tier-4 enrichment platform key (the keys
# of FallbackChainManager.platform_map). Only platforms with a real enrichment
# adapter are listed — everything else is left to the username sweep.
_ENRICHABLE_HOSTS = {
    "github.com": "github",
    "instagram.com": "instagram",
    "t.me": "telegram", "telegram.org": "telegram", "telegram.me": "telegram",
    "tiktok.com": "tiktok",
    "bsky.app": "bluesky",
    "linkedin.com": "linkedin",
    "protonmail.com": "protonmail", "proton.me": "protonmail",
    "snapchat.com": "snapchat",
    "reddit.com": "reddit", "old.reddit.com": "reddit",
}
_MAX_CONFIRMED_ENRICHMENTS = 25

# Free-mail / relay providers whose domain is not worth a domain-recon sweep
# (reconning gmail.com etc. yields nothing about the subject).
_FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "tutanota.com",
    "fastmail.com", "hey.com", "qq.com", "163.com", "126.com", "cox.net",
    "comcast.net", "verizon.net", "sbcglobal.net", "users.noreply.github.com",
})
_EMAIL_DOMAIN_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_MAX_DOMAIN_RECON = 5

# --- username -> candidate-email derivation ---------------------------------
# A username-only case never exercises any email-based OSINT. Real investigators
# routinely guess ``<username>@<provider>`` and check where it is registered.
# We do the same with holehe (keyless, reliable), bounded and clearly LABELLED:
# results are persisted at low confidence behind a notes sentinel so downstream
# engines surface them as investigative *leads*, never as confirmed identity.
CANDIDATE_NOTE_PREFIX = "[candidate-email]"
_CANDIDATE_EMAIL_PROVIDERS = (
    "gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "proton.me",
)
_MAX_CANDIDATE_USERNAMES = 2
_MAX_CANDIDATE_EMAILS = 8
_CANDIDATE_LOCALPART_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{2,30}$")


def _enrichable_platform(url: str) -> str | None:
    """Return the enrichment platform key for a profile URL, or None."""
    from urllib.parse import urlparse

    if not url or "://" not in url:
        return None
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _ENRICHABLE_HOSTS:
        return _ENRICHABLE_HOSTS[host]
    if "mastodon" in host:
        return "mastodon"
    return None


def _enrich_confirmed_accounts(
    case_id: str, run_id: str, analyst_id: str,
    already: set[str], enrich_task,
) -> int:
    """Dispatch platform enrichment for confirmed accounts on enrichable hosts."""
    from sqlalchemy import text
    from api.db.postgres import session_scope

    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    "SELECT DISTINCT result_value FROM evidence_units "
                    "WHERE case_id = :c AND result_type = 'account_found' "
                    "AND result_value LIKE 'http%'"
                ),
                {"c": case_id},
            ).all()
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the chord
        logger.warning("confirmed-account enrichment query failed: %s", exc)
        return 0

    dispatched = 0
    for (url,) in rows:
        if dispatched >= _MAX_CONFIRMED_ENRICHMENTS:
            break
        if url in already:
            continue
        platform_key = _enrichable_platform(url)
        if not platform_key:
            continue
        # Only enrich canonical profile URLs. The username sweep sometimes records
        # the *detection* endpoint (an API/search call) as the hit — those carry no
        # profile to enrich and would yield a garbage username.
        low_url = url.lower()
        if "?" in url or "/api/" in low_url or "/search" in low_url or "/xrpc/" in low_url:
            continue
        username = url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        try:
            enrich_task.delay(platform_key, url, username, case_id, run_id, analyst_id)
            already.add(url)
            dispatched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrichment dispatch failed for %s: %s", url, exc)
    if dispatched:
        logger.info("dispatched %d confirmed-account enrichments", dispatched)
    return dispatched


def _recon_discovered_domains(
    case_id: str, run_id: str, analyst_id: str, already: set[str],
) -> int:
    """Run the domain Tier 4 matrix on org domains discovered in the case.

    The domain tools (theHarvester / finalrecon / webdiver / sublist3r /
    dnstwist) otherwise only fire when a *domain* seed/pivot exists, so a plain
    username/email case never exercised them. Here we derive registrable domains
    from confirmed emails (skipping free-mail providers) and sweep each one,
    bounded by ``_MAX_DOMAIN_RECON``.
    """
    from sqlalchemy import text
    from api.db.postgres import session_scope
    from worker_python.tasks.pivot_tasks import run_domain_recon

    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    "SELECT DISTINCT result_value FROM evidence_units "
                    "WHERE case_id = :c AND result_type IN "
                    "('email_registered','breach_hit','google_hit','account_found')"
                ),
                {"c": case_id},
            ).all()
    except Exception as exc:  # noqa: BLE001 — domain recon must never break the chord
        logger.warning("domain-recon query failed: %s", exc)
        return 0

    domains: set[str] = set()
    for (val,) in rows:
        m = _EMAIL_DOMAIN_RE.search(val or "")
        if not m:
            continue
        dom = m.group(1).lower().strip(".")
        if dom and dom not in _FREEMAIL_DOMAINS and dom not in already:
            domains.add(dom)

    dispatched = 0
    for dom in sorted(domains)[:_MAX_DOMAIN_RECON]:
        try:
            run_domain_recon.delay(dom, case_id, run_id, analyst_id)
            already.add(dom)
            dispatched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("domain recon dispatch failed for %s: %s", dom, exc)
    if dispatched:
        logger.info("dispatched %d domain-recon sweeps", dispatched)
    return dispatched


def _top_username_seeds(case_id: str, limit: int) -> list[str]:
    """Return the most-evidenced username seeds (the strongest identity anchors).

    The original analyst-chosen username produces the most evidence, so ordering
    by row count surfaces it first; this bounds identity-conflation risk when we
    later guess emails from these handles.
    """
    from sqlalchemy import text
    from api.db.postgres import session_scope

    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    "SELECT seed_value, COUNT(*) AS n FROM evidence_units "
                    "WHERE case_id = :c AND seed_type = 'username' "
                    "AND COALESCE(seed_value, '') <> '' "
                    "GROUP BY seed_value ORDER BY n DESC LIMIT :lim"
                ),
                {"c": case_id, "lim": limit * 4},
            ).all()
    except Exception as exc:  # noqa: BLE001 — derivation must never break the chord
        logger.warning("username-seed query failed: %s", exc)
        return []

    out: list[str] = []
    seen: set[str] = set()
    for val, _n in rows:
        local = (val or "").strip().lower().lstrip("@")
        if not local or any(c in local for c in ("://", "@", "/")):
            continue
        if local in seen:
            continue
        seen.add(local)
        out.append(local)
        if len(out) >= limit:
            break
    return out


def _existing_case_emails(case_id: str) -> set[str]:
    """All e-mail addresses already present in the case (as seed or value)."""
    from sqlalchemy import text
    from api.db.postgres import session_scope

    emails: set[str] = set()
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    "SELECT DISTINCT seed_value, result_value FROM evidence_units "
                    "WHERE case_id = :c AND "
                    "(seed_value LIKE '%@%' OR result_value LIKE '%@%')"
                ),
                {"c": case_id},
            ).all()
            for seed_v, res_v in rows:
                for field in (seed_v, res_v):
                    if field and "@" in field:
                        emails.add(field.strip().lower())
    except Exception as exc:  # noqa: BLE001
        logger.warning("existing-email query failed: %s", exc)
    return emails


def _candidate_emails(case_id: str) -> list[tuple[str, str]]:
    """Build ``(candidate_email, source_username)`` guesses for the case."""
    from api.services.normaliser import USERNAME_BLACKLIST

    usernames = _top_username_seeds(case_id, _MAX_CANDIDATE_USERNAMES)
    if not usernames:
        return []
    existing = _existing_case_emails(case_id)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for uname in usernames:
        if uname in USERNAME_BLACKLIST or not _CANDIDATE_LOCALPART_RE.match(uname):
            continue
        for provider in _CANDIDATE_EMAIL_PROVIDERS:
            email = f"{uname}@{provider}"
            if email in existing or email in seen:
                continue
            seen.add(email)
            out.append((email, uname))
            if len(out) >= _MAX_CANDIDATE_EMAILS:
                return out
    return out


@celery_app.task(name="pipeline.derive_username_emails")
def derive_username_emails(case_id: str, run_id: str, analyst_id: str) -> dict:
    """Guess ``username@provider`` emails and probe them with holehe.

    This squeezes an email-registration footprint out of a *username-only* case.
    Each candidate is checked with holehe (the keyless, reliable registration
    checker); a candidate is kept only if it is actually registered somewhere
    (proving the address is real and in use, which keeps noise down). Every kept
    unit is tagged with the ``[candidate-email]`` notes sentinel + low confidence
    so the profile/insight/persona engines treat it as an unconfirmed *lead*,
    never as part of the subject's confirmed identity.
    """
    try:
        candidates = _candidate_emails(case_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate-email derivation failed: %s", exc)
        return {"candidates": 0, "confirmed": 0, "units": 0}
    if not candidates:
        return {"candidates": 0, "confirmed": 0, "units": 0}

    from worker_python.adapters.email.holehe import HoleheAdapter

    adapter = HoleheAdapter()
    if not adapter.health_check():
        logger.info("holehe unavailable; skipping candidate-email derivation")
        return {"candidates": len(candidates), "confirmed": 0, "units": 0}

    case_uuid, run_uuid = UUID(case_id), UUID(run_id)
    confirmed = 0
    total = 0
    for email, uname in candidates:
        try:
            units = adapter.execute(email, case_uuid, run_uuid, analyst_id, "email")
        except Exception as exc:  # noqa: BLE001
            logger.debug("holehe candidate run failed for %s: %s", email, exc)
            continue
        positives = [u for u in units if u.result_type == "email_registered"]
        if not positives:
            continue  # address not in use anywhere — drop it (no conflation noise)
        note = (
            f"{CANDIDATE_NOTE_PREFIX} {email} derived from username '{uname}'. "
            f"Ownership UNCONFIRMED — holehe found {len(positives)} registration(s)."
        )
        for u in positives:
            u.seed_value = email
            u.confidence_raw = 0.35
            u.notes = note
            enrich = dict(u.platform_enrichment or {})
            enrich.update({"candidate": True, "derived_from_username": uname})
            u.platform_enrichment = enrich
        total += preserve_and_persist(positives)
        confirmed += 1
    if confirmed:
        logger.info(
            "candidate-email derivation: %d/%d guesses registered (%d units)",
            confirmed, len(candidates), total,
        )
    return {"candidates": len(candidates), "confirmed": confirmed, "units": total}


@celery_app.task(name="tier2.username_sweep")
def run_tier2_username_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the username_tier2 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("username_tier2", "username", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 2, "chain": "username_tier2", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier2 username sweep failed: %s", exc)
        return {"tier": 2, "chain": "username_tier2", "hits": 0, "error": str(exc)}


@celery_app.task(name="tier2.email_sweep")
def run_tier2_email_sweep(seed_value: str, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Run the email_tier2 chain, preserve hits, and persist evidence."""
    try:
        manager = FallbackChainManager(UUID(case_id), UUID(run_id), analyst_id)
        try:
            units = manager.execute_chain("email_tier2", "email", seed_value)
        except ChainExhaustedError:
            units = []
        count = preserve_and_persist(units)
        return {"tier": 2, "chain": "email_tier2", "hits": count}
    except Exception as exc:  # noqa: BLE001 — a tool crash must not abort the chord
        logger.error("tier2 email sweep failed: %s", exc)
        return {"tier": 2, "chain": "email_tier2", "hits": 0, "error": str(exc)}


@celery_app.task(name="pipeline.aggregate_results")
def aggregate_results(results, case_id: str, run_id: str, analyst_id: str) -> dict:
    """Chord callback: run correlation, then trigger platform enrichment."""
    from api.db.postgres import session_scope
    from api.services.correlation import CorrelationEngine
    from api.services.provenance import ProvenanceService
    from worker_python.tasks.tier4_tasks import run_platform_enrichment

    engine = CorrelationEngine()
    provenance = ProvenanceService()
    case_uuid = UUID(case_id)

    with session_scope() as session:
        links = engine.run_full_correlation(case_uuid, session)
        provenance.log_audit_event(
            case_id=case_uuid,
            run_id=UUID(run_id),
            event_type="CORRELATION_COMPLETE",
            actor_id=analyst_id,
            metadata={"links": len(links)},
            session=session,
        )

    # Fire Tier 4 platform enrichment for HIGH/MEDIUM links.
    enrichment_dispatched = 0
    dispatched_urls: set[str] = set()
    for link in links:
        if link.confidence_tier in ("HIGH", "MEDIUM"):
            for platform, account in ((link.platform_a, link.account_a), (link.platform_b, link.account_b)):
                username = account.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
                run_platform_enrichment.delay(platform, account, username, case_id, run_id, analyst_id)
                dispatched_urls.add(account)
                enrichment_dispatched += 1

    # Also enrich confirmed first-party accounts on enrichable platforms even
    # when no correlation link pointed at them. A single-username case (e.g. a
    # lone GitHub/Instagram/Telegram profile) would otherwise never have its
    # rich profile fields (name, bio, location, avatar, creation date) fetched,
    # leaving the Subject Dossier starved. Bounded to keep load predictable.
    enrichment_dispatched += _enrich_confirmed_accounts(
        case_id, run_id, analyst_id, dispatched_urls, run_platform_enrichment
    )

    # Sweep the domain Tier 4 matrix on any org domains found in confirmed
    # emails so theHarvester/finalrecon/webdiver/sublist3r/dnstwist are exercised
    # even on a username/email case that produced no explicit domain seed.
    enrichment_dispatched += _recon_discovered_domains(
        case_id, run_id, analyst_id, set()
    )

    # Derive candidate emails from confirmed usernames and probe them (holehe),
    # so a username-only case still yields an email-registration footprint. The
    # results are clearly labelled as unconfirmed leads (see the task docstring).
    derive_username_emails.delay(case_id, run_id, analyst_id)

    # Kick off the recursive pivot loop (the brain): feed every newly discovered
    # identifier back into the pipeline as a fresh seed. Bounded by depth /
    # breadth / total caps inside the Pivot Engine.
    from worker_python.tasks.pivot_tasks import run_pivot_expansion
    run_pivot_expansion.delay(None, case_id, run_id, analyst_id, 0)

    return {"links": len(links), "enrichment_dispatched": enrichment_dispatched}


@celery_app.task(name="pipeline.finalize_correlation")
def finalize_correlation(case_id: str, run_id: str, analyst_id: str) -> dict:
    """Watchdog: guarantee correlation runs even if the chord callback was lost.

    The Tier 1/2 chord intermittently drops a header task (Celery group/chord
    publish flakiness): when that happens the chord never reaches its completion
    count and ``aggregate_results`` never fires, so correlation / persona /
    pivot / enrichment silently never run. This task is scheduled with a
    countdown at dispatch time. If ``CORRELATION_COMPLETE`` has already been
    logged for this run (the happy path, where the chord fired), it is a no-op;
    otherwise it runs the aggregation itself over whatever evidence has landed.
    """
    from sqlalchemy import text
    from api.db.postgres import session_scope

    with session_scope() as session:
        done = session.execute(
            text(
                "SELECT 1 FROM audit_log "
                "WHERE run_id = :r AND event_type = 'CORRELATION_COMPLETE' LIMIT 1"
            ),
            {"r": str(run_id)},
        ).first()

    if done:
        return {"finalized": False, "reason": "already_correlated"}

    logger.warning(
        "correlation watchdog firing for run %s — chord callback was lost", run_id
    )
    return aggregate_results([], case_id, run_id, analyst_id)
