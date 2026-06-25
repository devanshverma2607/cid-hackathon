"""MODULE 6b — Insight Engine.

Turns the raw pile of ``EvidenceUnit`` rows (and the correlation ``IdentityLink``
rows) collected for a case into a single, ranked, human-readable *intelligence
assessment*: a consolidated subject profile, corroborated account inventory,
exposure/risk scoring, ranked key findings, investigative leads, and a narrative.

Design notes
------------
* The core (:meth:`InsightEngine.assess`) is **pure** — it takes plain lists of
  dicts (evidence, links, case) and returns a plain dict. No DB, no network, no
  Android/framework objects — so it is trivially unit-testable with DTO inputs.
* Confidence is driven by *corroboration*: a platform confirmed by two or more
  independent tools outranks a lone hit. This naturally suppresses single-tool
  noise (e.g. a fuzzy username matcher that stripped a handle suffix) without
  hard-coding tool names.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

# --- result_type taxonomy (mirrors api/models/evidence.py) ------------------
PRESENCE_TYPES = {"account_found", "email_registered"}
EXPOSURE_TYPES = {"breach_hit", "archive_hit"}
ENRICH_TYPES = {"gravatar_hit", "google_hit", "whatsapp_hit", "phone_intel", "email_reputation"}
RECON_TYPES = {"domain_hit", "dork_hit", "onion_hit"}
SDM_TYPES = {"behavioral_insight", "post_timeline_collected", "community_membership_found",
             "reverse_image_hit", "profile_change_detected"}
NULL_TYPES = {"unavailable", "blocked"}

# --- severity ladder (ordered for sorting) ----------------------------------
SEVERITY_ORDER = {"critical": 4, "high": 3, "notable": 2, "info": 1}

# --- overall risk bands ------------------------------------------------------
RISK_BANDS = (
    (75, "HIGH"),
    (50, "ELEVATED"),
    (25, "MODERATE"),
    (0, "LOW"),
)

# --- footprint exposure bands (discoverability, not threat) ------------------
EXPOSURE_BANDS = (
    (70, "EXTENSIVE"),
    (45, "BROAD"),
    (20, "MODERATE"),
    (0, "MINIMAL"),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{6,}\d")

# Public email providers — detect a seed email embedded as a SUBDOMAIN of another
# host ("user@gmail.com.wordpress.com"): a templated-URL artifact, not an email.
_EMAIL_PROVIDERS = (
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "aol.com", "gmx.com", "mail.com", "zoho.com",
    "yandex.com", "rediffmail.com",
)
_TLD_NOISE = (
    ".com", ".net", ".org", ".io", ".co", ".social", ".cz", ".in", ".me",
    ".dev", ".app", ".xyz", ".info", ".gov", ".edu",
)


def _clean_email(value: str) -> str | None:
    """Normalised email, or None if it is a templated-host false positive."""
    if not value:
        return None
    email = value.strip().lower().strip(".")
    if not _EMAIL_RE.fullmatch(email):
        return None
    _, _, domain = email.partition("@")
    if domain.count(".") < 1:
        return None
    for prov in _EMAIL_PROVIDERS:
        if domain != prov and domain.startswith(prov + "."):
            return None
    return email


def _looks_like_domain(value: str) -> bool:
    """True when a candidate handle is really a host/domain fragment."""
    low = ("." + (value or "").lower())
    return any((t + ".") in low or low.endswith(t) for t in _TLD_NOISE)

# Hosts that are infrastructure/noise, never a "platform presence".
_NOISE_HOSTS = {
    "google.com", "www.google.com", "accounts.google.com",
    "emojicombos.com", "textus.com", "next.textus.com",
}

# Generic URL path segments that are pages, not usernames/handles.
_HANDLE_NOISE = {
    "search", "login", "signin", "signup", "register", "about", "help",
    "home", "index", "profile", "profiles", "user", "users", "account",
    "accounts", "settings", "share", "add", "explore", "results", "page",
    "channel", "watch", "video", "post", "posts", "status", "tag", "tags",
    "en", "www", "intl", "public", "app", "web", "api",
    # API endpoint / method words seen in tool result URLs (never handles)
    "autocomplete", "advancedsearch", "getprofile", "details", "publications",
    "actor", "people", "member", "members", "lookup", "query", "find",
    "v1", "v2", "v3", "v4", "graphql", "rest", "json", "oauth", "auth",
    "commands", "command", "docs", "doc", "site", "info", "id", "feed", "new",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _host_to_platform(host: str) -> str:
    """Collapse a hostname to a readable platform label (e.g. ``github.com``)."""
    host = (host or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"}:
        # archive.4plebs.org -> 4plebs.org ; gitlab.archlinux.org -> archlinux.org
        return ".".join(parts[-3:]) if parts[-3] not in {"www"} else ".".join(parts[-2:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _platform_of(unit: dict) -> str:
    """Best-effort platform label for an evidence unit."""
    sp = (unit.get("source_platform") or "").strip().lower()
    val = unit.get("result_value") or ""
    if val.startswith("http"):
        host = urlparse(val).netloc
        if host:
            return _host_to_platform(host)
    if sp and sp not in {"unknown", "web", "generic", ""}:
        return sp
    if "@" in val and " " not in val:  # email-registered style "amazon.com"
        return val.split("@")[-1].lower()
    return sp or "unknown"


def _handle_from(value: str) -> str:
    """Pull a bare handle from a URL/value (last meaningful path segment)."""
    value = (value or "").strip()
    if value.startswith("http"):
        path = urlparse(value).path.strip("/")
        if not path:
            return ""
        seg = path.split("/")[-1]
        return seg.lstrip("@")
    return value.lstrip("@")


class InsightEngine:
    """Synthesise raw evidence into a ranked intelligence assessment."""

    # ------------------------------------------------------------------ API
    def assess(
        self,
        evidence: list[dict],
        links: list[dict] | None = None,
        case: dict | None = None,
        include_ai: bool = False,
    ) -> dict:
        """Return the full intelligence assessment for one case.

        ``evidence`` / ``links`` are lists of dict rows (DB-row shaped). ``case``
        is the case row (may be ``None``). ``include_ai`` controls the optional
        local-LLM narrative: it is OFF by default so the interactive endpoints
        stay fast, and turned ON by the report generator and the dedicated
        ai-narrative endpoint (which can afford the extra latency).
        """
        links = links or []
        case = case or {}

        live = [
            e for e in evidence
            if e.get("result_type") not in NULL_TYPES
            and not (e.get("notes") or "").startswith("[candidate-email]")
        ]

        identifiers = self._identifiers(live, case)
        accounts = self._accounts(live)
        exposure = self._exposure(live)
        coverage = self._coverage(evidence)
        findings = self._findings(live, accounts, exposure, identifiers, links)
        risk = self._risk(accounts, exposure, findings, links)
        exposure_score = self._exposure_score(accounts, identifiers, exposure, coverage)
        leads = self._leads(live, identifiers, accounts)
        actions = self._recommended_actions(exposure, accounts, leads, identifiers, links)
        narrative = self._narrative(identifiers, accounts, exposure, risk, exposure_score, coverage)
        ai_narrative = self._ai_narrative(
            identifiers, accounts, exposure, risk, exposure_score, coverage, findings, narrative
        ) if include_ai else None

        # SDM behavioral/network/community leads
        behavioral_leads, community_leads, network_leads = self._sdm_leads(live)

        return {
            "generated_at": _now().isoformat(),
            "case_id": str(case.get("case_id", "")),
            "risk": risk,
            "subject_profile": {
                "identifiers": identifiers,
                "confirmed_accounts": accounts["confirmed"],
                "reported_accounts": accounts["reported"],
                "platform_count": accounts["platform_count"],
            },
            "exposure": exposure,
            "exposure_score": exposure_score,
            "key_findings": findings,
            "investigative_leads": leads,
            "recommended_actions": actions,
            "coverage": coverage,
            "narrative": narrative,
            "ai_narrative": ai_narrative,
            "behavioral_leads": behavioral_leads,
            "community_leads": community_leads,
            "network_leads": network_leads,
        }

    # ------------------------------------------------------------- identifiers
    def _identifiers(self, live: list[dict], case: dict) -> dict:
        emails: set[str] = set()
        usernames: set[str] = set()
        phones: set[str] = set()

        for src in (case.get("seed_value"), *[e.get("seed_value") for e in live]):
            if not src:
                continue
            self._bucket_identifier(str(src), emails, usernames, phones)

        # Mine result values for identifiers too (e.g. theHarvester emails).
        # Skip URLs — a templated host is not an email; reject provider subdomains.
        for e in live:
            val = e.get("result_value") or ""
            if val.startswith("http"):
                continue
            for m in _EMAIL_RE.findall(val):
                if (em := _clean_email(m)):
                    emails.add(em)

        return {
            "emails": sorted(emails),
            "usernames": sorted(usernames),
            "phones": sorted(phones),
        }

    @staticmethod
    def _bucket_identifier(raw: str, emails: set, usernames: set, phones: set) -> None:
        raw = raw.strip()
        if _EMAIL_RE.fullmatch(raw):
            if (em := _clean_email(raw)):
                emails.add(em)
        elif raw.startswith("+") or (raw.replace(" ", "").replace("-", "").isdigit() and len(raw) >= 7):
            phones.add(re.sub(r"[\s\-]", "", raw))
        elif raw and not raw.startswith("http") and not _looks_like_domain(raw):
            usernames.add(raw.lower())

    # ----------------------------------------------------------------- accounts
    def _accounts(self, live: list[dict]) -> dict:
        """Group discovered *profiles* by platform and rank by corroboration.

        Only ``account_found`` hits build the account inventory. ``email_registered``
        hits are exposure (a service the email is signed up to, not a browsable
        profile) and are reported separately under :meth:`_exposure`.
        """
        groups: dict[tuple[str, str], dict] = {}

        for e in live:
            if e.get("result_type") != "account_found":
                continue
            platform = _platform_of(e)
            if platform in _NOISE_HOSTS or platform == "unknown":
                continue
            handle = _handle_from(e.get("result_value") or "")
            # Degenerate handle (equals the platform / a bare domain) → use the seed.
            if not handle or handle.lower() == platform or "." in handle:
                handle = e.get("seed_value") or handle
            if handle.lower() in _HANDLE_NOISE:
                continue
            key = (platform, handle.lower())
            g = groups.setdefault(key, {
                "platform": platform,
                "handle": handle,
                "url": e.get("result_value") or "",
                "result_type": e.get("result_type"),
                "tools": set(),
                "source_tiers": set(),
            })
            g["tools"].add(e.get("tool_name") or "?")
            if e.get("source_tier") is not None:
                g["source_tiers"].add(int(e["source_tier"]))
            if (e.get("result_value") or "").startswith("http"):
                g["url"] = e["result_value"]

        confirmed, reported = [], []
        for g in groups.values():
            tools = sorted(g.pop("tools"))
            tiers = sorted(g.pop("source_tiers")) or [2]
            corroboration = len(tools)
            best_tier = min(tiers)
            tier = self._account_tier(corroboration, best_tier)
            record = {
                "platform": g["platform"],
                "handle": g["handle"],
                "url": g["url"],
                "result_type": g["result_type"],
                "tools": tools,
                "corroboration": corroboration,
                "best_source_tier": best_tier,
                "confidence": tier,
            }
            (confirmed if tier in ("HIGH", "MEDIUM") else reported).append(record)

        confirmed.sort(key=lambda r: (-r["corroboration"], r["best_source_tier"], r["platform"]))
        reported.sort(key=lambda r: (r["best_source_tier"], r["platform"]))
        platforms = {r["platform"] for r in confirmed + reported}
        return {
            "confirmed": confirmed,
            "reported": reported,
            "platform_count": len(platforms),
        }

    @staticmethod
    def _account_tier(corroboration: int, best_source_tier: int) -> str:
        """Corroboration + provenance → confidence tier for a discovered account."""
        if corroboration >= 2:
            return "HIGH"
        if best_source_tier <= 1:  # single hit but from a first-party API
            return "HIGH"
        if best_source_tier <= 2:
            return "MEDIUM"
        return "LOW"

    # ----------------------------------------------------------------- exposure
    def _exposure(self, live: list[dict]) -> dict:
        breaches, pastes, registrations, enrich = [], [], [], []
        for e in live:
            rt = e.get("result_type")
            entry = {
                "tool": e.get("tool_name"),
                "platform": _platform_of(e),
                "value": e.get("result_value"),
            }
            if rt == "breach_hit":
                breaches.append(entry)
            elif rt == "archive_hit":
                pastes.append(entry)
            elif rt == "email_registered":
                registrations.append(entry)
            elif rt in ENRICH_TYPES:
                enrich.append({**entry, "type": rt})
        return {
            "breach_hits": breaches,
            "paste_archive_hits": pastes,
            "email_registrations": registrations,
            "enrichment_hits": enrich,
            "breach_count": len(breaches),
            "paste_count": len(pastes),
            "registration_count": len(registrations),
        }

    # ----------------------------------------------------------------- findings
    def _findings(
        self,
        live: list[dict],
        accounts: dict,
        exposure: dict,
        identifiers: dict,
        links: list[dict],
    ) -> list[dict]:
        out: list[dict] = []

        def add(severity, category, title, detail, confidence, tools):
            out.append({
                "severity": severity,
                "category": category,
                "title": title,
                "detail": detail,
                "confidence": round(confidence, 2),
                "supporting_tools": sorted(set(t for t in tools if t)),
            })

        # 1. Breach exposure — highest priority.
        if exposure["breach_count"]:
            plats = sorted({b["platform"] for b in exposure["breach_hits"]})
            add("critical", "exposure",
                f"Credentials exposed in {exposure['breach_count']} breach record(s)",
                f"Subject identifiers appear in known breach data ({', '.join(plats[:8])}).",
                0.9, [b["tool"] for b in exposure["breach_hits"]])

        # 2. Paste / archive presence.
        if exposure["paste_count"]:
            add("high", "exposure",
                f"Mentioned in {exposure['paste_count']} paste/archive source(s)",
                "Identifiers surfaced in paste sites or web archives — possible leak or "
                "public disclosure worth manual review.",
                0.6, [p["tool"] for p in exposure["paste_archive_hits"]])

        # 3. Account sprawl from email.
        if exposure["registration_count"] >= 3:
            svc = sorted({r["platform"] for r in exposure["email_registrations"]})
            add("notable", "footprint",
                f"Email registered on {exposure['registration_count']} services",
                f"Active registrations detected on: {', '.join(svc[:12])}.",
                0.75, [r["tool"] for r in exposure["email_registrations"]])

        # 4. Phone tied to social accounts.
        phone_social = [
            e for e in live
            if e.get("seed_type") == "phone" and e.get("result_type") == "account_found"
        ]
        if phone_social:
            plats = sorted({_platform_of(e) for e in phone_social})
            add("high", "linkage",
                "Phone number linked to social account(s)",
                f"The subject phone resolves to: {', '.join(plats)}. Strong real-world pivot.",
                0.8, [e.get("tool_name") for e in phone_social])

        # 5. Corroborated cross-platform persona (same handle, multiple platforms).
        by_handle: dict[str, set[str]] = defaultdict(set)
        handle_tools: dict[str, set[str]] = defaultdict(set)
        for rec in accounts["confirmed"] + accounts["reported"]:
            by_handle[rec["handle"].lower()].add(rec["platform"])
            handle_tools[rec["handle"].lower()].update(rec["tools"])
        for handle, plats in by_handle.items():
            if handle and len(plats) >= 3:
                add("notable", "persona",
                    f"Reused handle '{handle}' across {len(plats)} platforms",
                    f"Consistent username on: {', '.join(sorted(plats)[:12])}. Indicates a "
                    "single persona spanning these sites.",
                    min(0.9, 0.5 + 0.1 * len(plats)), handle_tools[handle])

        # 6. High-confidence corroborated accounts.
        for rec in accounts["confirmed"]:
            if rec["corroboration"] >= 2:
                add("notable", "account",
                    f"Confirmed account: {rec['platform']}/{rec['handle']}",
                    f"Independently confirmed by {rec['corroboration']} tools "
                    f"({', '.join(rec['tools'])}).",
                    0.85, rec["tools"])

        # 7. Handle == email local-part (identity reuse).
        local_parts = {em.split("@")[0] for em in identifiers["emails"]}
        for un in identifiers["usernames"]:
            base = un.split(".")[0]
            if un in local_parts or base in local_parts:
                add("info", "linkage",
                    f"Username '{un}' matches an email local-part",
                    "The username and email share a stem — supports same-owner attribution.",
                    0.5, [])
                break

        # 8. Correlation engine HIGH links (if provided).
        for link in links:
            if str(link.get("confidence_tier")).upper() == "HIGH":
                add("high", "linkage",
                    f"Identity link: {link.get('platform_a')} ↔ {link.get('platform_b')}",
                    f"Correlation engine scored {link.get('confidence_score')} "
                    f"({link.get('signal_count')} signals).",
                    0.85, [])

        out.sort(key=lambda f: (-SEVERITY_ORDER.get(f["severity"], 0), -f["confidence"]))
        return out

    # --------------------------------------------------------------------- risk
    def _risk(self, accounts: dict, exposure: dict, findings: list[dict], links: list[dict]) -> dict:
        score = 0.0
        drivers: list[str] = []

        if exposure["breach_count"]:
            score += 35
            drivers.append(f"{exposure['breach_count']} breach hit(s)")
        if exposure["paste_count"]:
            add = min(20, 5 * exposure["paste_count"])
            score += add
            drivers.append(f"{exposure['paste_count']} paste/archive mention(s)")
        if exposure["registration_count"]:
            add = min(20, 3 * exposure["registration_count"])
            score += add
            drivers.append(f"{exposure['registration_count']} service registration(s)")

        if any(f["category"] == "linkage" and f["severity"] in ("high", "critical") for f in findings):
            score += 15
            drivers.append("phone/identity linkage")

        corroborated = [a for a in accounts["confirmed"] if a["corroboration"] >= 2]
        if corroborated:
            score += min(15, 5 * len(corroborated))
            drivers.append(f"{len(corroborated)} corroborated account(s)")

        if any(str(l.get("confidence_tier")).upper() == "HIGH" for l in links):
            score += 10
            drivers.append("HIGH correlation link")

        score = round(min(100.0, score), 1)
        band = next(label for floor, label in RISK_BANDS if score >= floor)
        return {"score": score, "band": band, "drivers": drivers}

    # ----------------------------------------------------------- exposure score
    def _exposure_score(
        self, accounts: dict, identifiers: dict, exposure: dict, coverage: dict
    ) -> dict:
        """How *discoverable* the subject's footprint is (breadth/reach).

        Distinct from :meth:`_risk` (threat severity): a subject can be highly
        *exposed* (wide, easily-found presence) with low *risk*, or vice-versa.
        Score is capped at 100 and banded MINIMAL/MODERATE/BROAD/EXTENSIVE.
        """
        score = 0.0
        drivers: list[str] = []

        platform_count = accounts["platform_count"]
        if platform_count:
            add = min(40, 5 * platform_count)
            score += add
            drivers.append(f"{platform_count} platform(s) with presence")

        id_count = (
            len(identifiers["emails"])
            + len(identifiers["usernames"])
            + len(identifiers["phones"])
        )
        if id_count:
            score += min(20, 4 * id_count)
            drivers.append(f"{id_count} distinct identifier(s)")

        if exposure["breach_count"]:
            score += min(20, 10 * exposure["breach_count"])
            drivers.append(f"{exposure['breach_count']} breach exposure(s)")

        if exposure["registration_count"]:
            score += min(10, 2 * exposure["registration_count"])
            drivers.append(f"{exposure['registration_count']} service registration(s)")

        if exposure["paste_count"]:
            score += min(10, 3 * exposure["paste_count"])
            drivers.append(f"{exposure['paste_count']} paste/archive mention(s)")

        score = round(min(100.0, score), 1)
        band = next(label for floor, label in EXPOSURE_BANDS if score >= floor)
        reach = (
            round(coverage["tools_with_hits"] / coverage["tools_run"], 2)
            if coverage["tools_run"] else 0.0
        )
        return {"score": score, "band": band, "drivers": drivers, "tool_reach": reach}

    # -------------------------------------------------------------------- leads
    def _leads(self, live: list[dict], identifiers: dict, accounts: dict) -> list[dict]:
        """Newly surfaced handles/values worth feeding back into the pipeline."""
        seeds = set(identifiers["usernames"]) | set(identifiers["emails"]) | set(identifiers["phones"])
        seed_handles = [s.lower() for s in identifiers["usernames"] if s and len(s) >= 3]
        leads: dict[str, dict] = {}
        for e in live:
            # Leads are new *handles* to pivot on — recon hits and discovered
            # profiles, never email registrations (those are service domains).
            if e.get("result_type") not in (RECON_TYPES | {"account_found"}):
                continue
            raw_value = (e.get("result_value") or "").lower()
            # Same-subject account: the URL embeds a known seed handle (the tool
            # confirmed *this* subject on another site), so the last path token
            # (e.g. /gists, ?tab=filter) is page chrome, not a new identity.
            if any(sh in raw_value for sh in seed_handles):
                continue
            handle = _handle_from(e.get("result_value") or "")
            if not handle or handle.startswith("http") or len(handle) < 3:
                continue
            low = handle.lower()
            if low in seeds or low in _NOISE_HOSTS or low in _HANDLE_NOISE:
                continue
            # Drop bare service domains (e.g. amazon.com) — not pivotable handles.
            if re.search(r"\.(com|net|org|io|me|co|club|gg|tv|app|dev|info|xyz)$", low):
                continue
            # A handle is a single path token: reject dotted tokens, which are
            # almost always file names (advancedsearch.php) or API method paths
            # (app.bsky.actor.getProfile) rather than real usernames.
            if not re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", handle):
                continue
            lead = leads.setdefault(low, {
                "value": handle,
                "platform": _platform_of(e),
                "source_url": e.get("result_value"),
                "tools": set(),
            })
            lead["tools"].add(e.get("tool_name"))
        out = [
            {**l, "tools": sorted(t for t in l["tools"] if t)}
            for l in leads.values()
        ]
        out.sort(key=lambda l: (-len(l["tools"]), l["value"]))
        return out[:25]

    # ----------------------------------------------------------- recommended
    def _recommended_actions(
        self,
        exposure: dict,
        accounts: dict,
        leads: list[dict],
        identifiers: dict,
        links: list[dict],
    ) -> list[dict]:
        """Concrete, prioritised next steps for the analyst — derived from evidence.

        Each action carries a ``priority`` (high/medium/low), a ``category``
        (lawful-process / pivot / verification / monitoring), and a rationale.
        """
        out: list[dict] = []

        def add(priority, category, action, rationale):
            out.append({
                "priority": priority,
                "category": category,
                "action": action,
                "rationale": rationale,
            })

        # 1. Lawful process on confirmed first-party accounts.
        api_accounts = [a for a in accounts["confirmed"] if a["best_source_tier"] <= 1]
        if api_accounts:
            plats = sorted({a["platform"] for a in api_accounts})
            add("high", "lawful_process",
                f"Prepare a data-preservation / disclosure request for: {', '.join(plats[:6])}",
                "These accounts are confirmed via first-party APIs — strong candidates for "
                "lawful platform records (subscriber info, login IPs).")

        # 2. Breach exposure → credential / re-use review.
        if exposure["breach_count"]:
            add("high", "verification",
                "Review breach records for reused passwords and linked secondary emails",
                f"Subject appears in {exposure['breach_count']} breach record(s); breach data "
                "often exposes additional identifiers and reused credentials.")

        # 3. Paste/archive → manual review (volatile content).
        if exposure["paste_count"]:
            add("medium", "verification",
                "Manually review and re-preserve paste/archive hits before they disappear",
                f"{exposure['paste_count']} paste/archive mention(s) found — such content is "
                "volatile and should be captured immediately.")

        # 4. Pivot on the strongest new leads.
        for lead in leads[:3]:
            add("medium", "pivot",
                f"Run a fresh sweep on discovered handle '{lead['value']}'",
                f"Surfaced on {lead['platform']} by {', '.join(lead['tools']) or 'collection'}; "
                "not yet investigated as a seed.")

        # 5. Phone enrichment if a number is known but under-used.
        if identifiers["phones"]:
            add("medium", "pivot",
                f"Cross-reference phone {identifiers['phones'][0]} against messaging apps and "
                "caller-ID services",
                "A confirmed phone number is a high-value real-world pivot.")

        # 6. Verify single-source (reported) accounts.
        if accounts["reported"]:
            add("low", "verification",
                f"Manually verify {len(accounts['reported'])} single-source 'reported' account(s)",
                "These were seen by only one tool and may include false positives.")

        # 7. Resolve HIGH correlation links into a confirmed persona.
        high_links = [l for l in links if str(l.get("confidence_tier")).upper() == "HIGH"]
        if high_links:
            add("medium", "verification",
                f"Adjudicate {len(high_links)} HIGH-confidence identity link(s) in the review queue",
                "Confirming these links consolidates the subject's cross-platform persona.")

        # 8. Monitoring fallback when little was found.
        if not accounts["confirmed"] and not exposure["breach_count"]:
            add("low", "monitoring",
                "Set up periodic re-collection — current footprint is sparse",
                "Few confirmed signals; the subject may use uncommon handles or be low-activity.")

        prio = {"high": 0, "medium": 1, "low": 2}
        out.sort(key=lambda a: prio.get(a["priority"], 3))
        return out


    # ---------------------------------------------------------------- narrative
    def _narrative(self, identifiers, accounts, exposure, risk, exposure_score, coverage) -> str:
        ids = []
        if identifiers["emails"]:
            ids.append(f"{len(identifiers['emails'])} email(s)")
        if identifiers["usernames"]:
            ids.append(f"{len(identifiers['usernames'])} username(s)")
        if identifiers["phones"]:
            ids.append(f"{len(identifiers['phones'])} phone number(s)")
        id_str = ", ".join(ids) or "the supplied seed"

        conf = len(accounts["confirmed"])
        parts = [
            f"From {id_str}, the engine corroborated {conf} confirmed account(s) "
            f"across {accounts['platform_count']} platform(s)."
        ]
        if exposure["breach_count"] or exposure["paste_count"]:
            parts.append(
                f"Exposure signals were detected: {exposure['breach_count']} breach hit(s) "
                f"and {exposure['paste_count']} paste/archive mention(s)."
            )
        if exposure["registration_count"]:
            parts.append(
                f"The primary email is registered on {exposure['registration_count']} online service(s)."
            )
        parts.append(
            f"Overall threat risk is assessed {risk['band']} ({risk['score']}/100), "
            f"and footprint exposure {exposure_score['band']} ({exposure_score['score']}/100). "
            f"{coverage['tools_with_hits']} of {coverage['tools_run']} tools returned data."
        )
        return " ".join(parts)

    def _ai_narrative(
        self, identifiers, accounts, exposure, risk, exposure_score, coverage, findings, deterministic
    ) -> str | None:
        """Optional local-LLM polish of the narrative (grounded in the computed
        facts). Returns None when Ollama is disabled/unreachable so the caller
        keeps the deterministic narrative."""
        try:
            from api.services.llm_narrative import LLMNarrator
        except Exception:  # noqa: BLE001
            return None
        facts = {
            "identifier_counts": {
                "emails": len(identifiers.get("emails", [])),
                "usernames": len(identifiers.get("usernames", [])),
                "phones": len(identifiers.get("phones", [])),
            },
            "confirmed_accounts": len(accounts.get("confirmed", [])),
            "platform_count": accounts.get("platform_count"),
            "exposure": {
                "breach_count": exposure.get("breach_count"),
                "paste_count": exposure.get("paste_count"),
                "registration_count": exposure.get("registration_count"),
            },
            "threat_risk": {"band": risk.get("band"), "score": risk.get("score")},
            "footprint_exposure": {
                "band": exposure_score.get("band"),
                "score": exposure_score.get("score"),
            },
            "coverage": {
                "tools_with_hits": coverage.get("tools_with_hits"),
                "tools_run": coverage.get("tools_run"),
            },
            "key_findings": [
                f.get("title") for f in (findings or [])[:6] if isinstance(f, dict) and f.get("title")
            ],
            "deterministic_summary": deterministic,
        }
        instruction = (
            "Summarise this OSINT case assessment for an investigator: what the "
            "online footprint shows, the most salient exposure and risk signals, "
            "and how complete the collection is."
        )
        return LLMNarrator().generate(facts, instruction)

    # ----------------------------------------------------------------- coverage
    @staticmethod
    def _coverage(evidence: list[dict]) -> dict:
        by_tool: dict[str, list[dict]] = defaultdict(list)
        for e in evidence:
            by_tool[e.get("tool_name") or "?"].append(e)

        run = len(by_tool)
        with_hits, unavailable = 0, 0
        for units in by_tool.values():
            types = {u.get("result_type") for u in units}
            if types - NULL_TYPES:
                with_hits += 1
            elif "unavailable" in types or "blocked" in types:
                unavailable += 1
        return {
            "tools_run": run,
            "tools_with_hits": with_hits,
            "tools_empty": run - with_hits - unavailable,
            "tools_unavailable": unavailable,
            "total_evidence_units": len(evidence),
        }

    # -------------------------------------------------------- SDM leads
    @staticmethod
    def _sdm_leads(live: list[dict]) -> tuple[list, list, list]:
        """Extract SDM behavioral/community/network leads from evidence."""
        behavioral_leads: list[dict] = []
        community_leads: list[dict] = []
        network_leads: list[dict] = []

        for e in live:
            rt = e.get("result_type", "")
            notes = e.get("notes") or ""
            enrich = e.get("platform_enrichment")
            if not isinstance(enrich, dict):
                continue
            platform = _platform_of(e)

            if rt == "behavioral_insight" or "[behavioral-inferred]" in notes:
                if enrich.get("inferred_timezone"):
                    tz = enrich["inferred_timezone"]
                    offset = tz.get("utc_offset_point", 0)
                    behavioral_leads.append({
                        "type": "timezone_inference",
                        "title": f"Inferred timezone: UTC{'+' if offset >= 0 else ''}{offset}",
                        "platform": platform,
                        "confidence": tz.get("confidence_raw", 0),
                        "detail": (
                            f"Sleep trough detected, "
                            f"{tz.get('sample_size', 0)} posts analysed."
                        ),
                        "basis": "posting hour-of-day distribution [behavioral-inferred]",
                    })
                for brk in enrich.get("rhythm_breaks", []):
                    behavioral_leads.append({
                        "type": "rhythm_break",
                        "title": (
                            f"Activity silence: {brk.get('start_date', '?')} "
                            f"\u2192 {brk.get('end_date', '?')}"
                        ),
                        "platform": platform,
                        "confidence": 0.40,
                        "detail": f"{brk.get('duration_days', '?')}-day gap.",
                        "basis": "posting rhythm analysis [behavioral-inferred]",
                    })
                for spk in enrich.get("velocity_spikes", []):
                    behavioral_leads.append({
                        "type": "velocity_spike",
                        "title": (
                            f"Posting surge: {spk.get('start_date', '?')} "
                            f"\u2192 {spk.get('end_date', '?')}"
                        ),
                        "platform": platform,
                        "confidence": 0.40,
                        "detail": (
                            f"{spk.get('posts_in_window', '?')} posts vs "
                            f"baseline {spk.get('baseline_rate', '?')}/window."
                        ),
                        "basis": "posting velocity analysis [behavioral-inferred]",
                    })

            if rt == "community_membership_found":
                comm = enrich.get("community")
                if isinstance(comm, dict):
                    community_leads.append(comm)

            ig = enrich.get("interaction_graph")
            if ig and isinstance(ig, dict):
                for target, count in sorted(ig.items(), key=lambda kv: -kv[1])[:10]:
                    network_leads.append({
                        "target": target, "count": count, "platform": platform,
                    })

        return behavioral_leads, community_leads, network_leads
