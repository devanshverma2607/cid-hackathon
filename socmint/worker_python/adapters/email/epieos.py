"""EpieosAdapter — Tier 2 reverse-email-to-linked-accounts lookup (Epieos).

Epieos (https://epieos.com) discovers service accounts linked to an email
address (Google, PayPal, Skype, Amazon, etc.) and sometimes an avatar URL.

**No public API exists** (enterprise-only).  This adapter queries the public
web interface at ``https://epieos.com/?q={email}`` and scrapes linked-account
data from the HTML response.  If the site blocks automated access (CAPTCHA,
403, etc.) the adapter degrades to ``unavailable`` — never crashes.

Each discovered linked account is emitted as a standard ``account_found``
evidence unit so it flows into correlation, persona resolution, and the pivot
engine exactly like any other discovered account.  If an avatar URL is returned,
it is injected into ``platform_enrichment`` for the photo-hash pipeline.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_URL = "https://epieos.com"
_QUERY_URL = "https://epieos.com/?q={email}"

# Patterns to extract linked service names from the results page.
_SERVICE_RE = re.compile(
    r'class="[^"]*service[^"]*"[^>]*>\s*(?:<[^>]+>)*\s*([A-Za-z0-9 .]+)',
    re.I,
)
_AVATAR_RE = re.compile(
    r'(?:avatar|profile[_-]?(?:pic|image|photo))["\']?\s*[:=]\s*["\']?(https?://[^\s"\'<>]+)',
    re.I,
)


class EpieosAdapter(ToolAdapter):
    """Keyless reverse-email → linked-account discovery via Epieos web scraping."""

    def name(self) -> str:
        return "epieos"

    def version(self) -> str:
        return "web-scrape"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct egress

    def health_check(self) -> bool:
        return True  # keyless; availability checked at runtime

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        seed = (seed or "").strip().lower()
        if not seed or "@" not in seed:
            return []

        url = _QUERY_URL.format(email=seed)
        resp = http_get(url, use_tor=False, timeout=20)
        if resp is None or resp.status_code != 200:
            return []

        html = resp.text or ""
        if not html or "epieos" not in html.lower():
            return []

        # Extract linked services from the HTML
        services = _SERVICE_RE.findall(html)
        # Extract avatar URL if present
        avatar_match = _AVATAR_RE.search(html)
        avatar_url = avatar_match.group(1) if avatar_match else None

        results: list[dict] = []
        seen: set[str] = set()
        for svc in services:
            svc_clean = svc.strip()
            if not svc_clean or len(svc_clean) < 2:
                continue
            svc_lower = svc_clean.lower()
            if svc_lower in seen:
                continue
            seen.add(svc_lower)
            results.append({
                "service": svc_clean,
                "avatar_url": avatar_url,
            })

        return results

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            service = item.get("service", "").strip()
            if not service:
                continue

            platform = service.lower().replace(" ", "_")
            enrichment: dict = {"discovered_via": "epieos"}
            avatar = item.get("avatar_url")
            if avatar:
                enrichment["avatar_url"] = avatar

            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,  # public web scrape
                    seed_value=self._seed_value,
                    result_type="account_found",
                    result_value=f"{self._seed_value} ({service})",
                    confidence_raw=0.55,
                    platform_enrichment=enrichment,
                    notes=f"source=epieos service={service}",
                )
            )
        return units
