"""HunterIOAdapter — Tier 2 email verification + Tier 4 domain email search.

Hunter.io (https://hunter.io) provides two endpoints that serve the SOCMINT
pipeline:

  * **Email Verifier** — validates an email address and returns deliverability
    status, web sources where the address was found, and disposition flags
    (disposable, webmail, gibberish).  Feeds Tier 2 email enrichment.
  * **Domain Search** — discovers email addresses associated with a domain,
    with names, positions, departments, and confidence scores. Feeds Tier 4
    domain enrichment (triggered after correlation discovers a domain).

Gated on ``HUNTERIO_API_KEY`` — absent key => unhealthy => graceful
``unavailable`` marker. The key is passed as a query parameter and never logged.
Free tier: 25 email verifications/month, 10 domain-search results/page.
"""
from __future__ import annotations

import os
import re

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_BASE = "https://api.hunter.io/v2"
_VERIFY_URL = f"{_BASE}/email-verifier"
_DOMAIN_SEARCH_URL = f"{_BASE}/domain-search"
_MAX_DOMAIN_EMAILS = 20  # cap to avoid burning credits
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _api_key() -> str:
    return os.environ.get("HUNTERIO_API_KEY", "").strip()


class HunterIOAdapter(ToolAdapter):
    """Keyed email verifier + domain email enumeration via Hunter.io."""

    def name(self) -> str:
        return "hunterio"

    def version(self) -> str:
        return "api-v2"

    def get_tool_tier(self) -> int:
        # Dynamically adapts: email seeds -> tier 2, domain seeds -> tier 4.
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct egress — authenticated by API key

    def health_check(self) -> bool:
        return bool(_api_key())

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        seed = (seed or "").strip()
        key = _api_key()
        if not seed or not key:
            return []

        # Decide which API to call based on seed type.
        if _EMAIL_RE.match(seed):
            return self._verify_email(seed, key)
        # If it looks like a domain (no @, has a dot), try domain search.
        if "." in seed and "@" not in seed:
            domain = self._clean_domain(seed)
            if domain:
                return self._search_domain(domain, key)
        return []

    def _verify_email(self, email: str, key: str) -> list[dict]:
        """Call the email-verifier endpoint."""
        import httpx

        try:
            with httpx.Client(timeout=25.0, follow_redirects=True) as client:
                resp = client.get(
                    _VERIFY_URL, params={"email": email, "api_key": key}
                )
            # 202 = still processing; treat as no result for now (transient).
            if resp.status_code not in (200,):
                return []
            data = (resp.json() or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(data, dict):
            return []

        sources = []
        for src in (data.get("sources") or [])[:10]:
            if isinstance(src, dict) and src.get("uri"):
                sources.append({
                    "domain": src.get("domain"),
                    "uri": src["uri"],
                    "extracted_on": src.get("extracted_on"),
                    "last_seen_on": src.get("last_seen_on"),
                })

        record = {
            "kind": "email_verify",
            "email": email,
            "status": data.get("status"),         # valid/invalid/accept_all/...
            "score": data.get("score"),            # 0-100
            "disposable": data.get("disposable"),
            "webmail": data.get("webmail"),
            "gibberish": data.get("gibberish"),
            "mx_records": data.get("mx_records"),
            "smtp_server": data.get("smtp_server"),
            "smtp_check": data.get("smtp_check"),
            "accept_all": data.get("accept_all"),
            "sources": sources,
        }
        return [record]

    def _search_domain(self, domain: str, key: str) -> list[dict]:
        """Call the domain-search endpoint -- returns discovered emails."""
        import httpx

        try:
            with httpx.Client(timeout=25.0, follow_redirects=True) as client:
                resp = client.get(
                    _DOMAIN_SEARCH_URL,
                    params={"domain": domain, "api_key": key, "limit": _MAX_DOMAIN_EMAILS},
                )
            if resp.status_code != 200:
                return []
            payload = resp.json() or {}
            data = payload.get("data") or {}
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(data, dict):
            return []

        org = data.get("organization") or ""
        pattern = data.get("pattern") or ""
        emails_raw = data.get("emails") or []

        records: list[dict] = []
        # Summary record for the domain itself.
        meta = payload.get("meta") or {}
        records.append({
            "kind": "domain_summary",
            "domain": domain,
            "organization": org,
            "pattern": pattern,
            "total_results": meta.get("results", 0),
            "disposable": data.get("disposable"),
            "webmail": data.get("webmail"),
            "accept_all": data.get("accept_all"),
        })

        for em in emails_raw[:_MAX_DOMAIN_EMAILS]:
            if not isinstance(em, dict) or not em.get("value"):
                continue
            records.append({
                "kind": "discovered_email",
                "email": em["value"],
                "type": em.get("type"),          # personal / generic
                "confidence": em.get("confidence"),
                "first_name": em.get("first_name"),
                "last_name": em.get("last_name"),
                "position": em.get("position"),
                "department": em.get("department"),
                "seniority": em.get("seniority"),
                "linkedin": em.get("linkedin"),
                "twitter": em.get("twitter"),
                "phone_number": em.get("phone_number"),
                "verification_status": (em.get("verification") or {}).get("status"),
            })
        return records

    @staticmethod
    def _clean_domain(seed: str) -> str:
        """Strip protocol/path from a URL or domain string."""
        d = seed.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if d.startswith(prefix):
                d = d[len(prefix):]
        d = d.split("/")[0].split("?")[0].strip()
        return d if "." in d else ""

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for rec in raw:
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind", "")

            if kind == "email_verify":
                units.append(self._parse_verify(rec))
            elif kind == "domain_summary":
                units.append(self._parse_domain_summary(rec))
            elif kind == "discovered_email":
                unit = self._parse_discovered_email(rec)
                if unit:
                    units.append(unit)
        return units

    def _parse_verify(self, rec: dict) -> EvidenceUnit:
        email = rec.get("email", "")
        status = rec.get("status", "unknown")
        score = rec.get("score", 0)
        parts = ["source=hunterio", f"status={status}", f"score={score}"]
        if rec.get("disposable"):
            parts.append("disposable=True")
        if rec.get("webmail"):
            parts.append("webmail=True")
        if rec.get("gibberish"):
            parts.append("gibberish=True")
        source_count = len(rec.get("sources") or [])
        if source_count:
            parts.append(f"web_sources={source_count}")

        return self.make_evidence(
            source_platform="email_intel",
            source_tier=2,
            result_type="email_registered" if status in ("valid", "accept_all") else "email_check",
            result_value=email,
            platform_enrichment=rec,
            notes=" ".join(parts)[:2000],
        )

    def _parse_domain_summary(self, rec: dict) -> EvidenceUnit:
        domain = rec.get("domain", "")
        org = rec.get("organization", "")
        total = rec.get("total_results", 0)
        parts = ["source=hunterio", f"domain={domain}"]
        if org:
            parts.append(f"org={org}")
        parts.append(f"emails_found={total}")
        if rec.get("pattern"):
            parts.append(f"pattern={rec['pattern']}")

        return self.make_evidence(
            source_platform="domain",
            source_tier=2,
            result_type="domain_hit",
            result_value=domain,
            platform_enrichment=rec,
            notes=" ".join(parts)[:2000],
        )

    def _parse_discovered_email(self, rec: dict) -> EvidenceUnit | None:
        email = rec.get("email", "")
        if not email:
            return None
        parts = ["source=hunterio"]
        if rec.get("first_name") and rec.get("last_name"):
            parts.append(f"name={rec['first_name']} {rec['last_name']}")
        if rec.get("position"):
            parts.append(f"position={rec['position']}")
        if rec.get("department"):
            parts.append(f"dept={rec['department']}")
        conf = rec.get("confidence")
        if conf is not None:
            parts.append(f"confidence={conf}")
        veri = rec.get("verification_status")
        if veri:
            parts.append(f"verified={veri}")

        return self.make_evidence(
            source_platform="email_intel",
            source_tier=3,
            result_type="email_registered",
            result_value=email,
            platform_enrichment=rec,
            notes=" ".join(parts)[:2000],
        )
