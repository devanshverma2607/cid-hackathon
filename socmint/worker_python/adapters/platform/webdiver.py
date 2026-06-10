"""WebdiverAdapter — Tier 4 website fingerprinting via direct keyless HTTP."""
from __future__ import annotations

import re

from worker_python.adapters._net import clean_domain, http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_GENERATOR_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', re.I
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class WebdiverAdapter(ToolAdapter):
    """Fetches {domain} and extracts server/title/tech/emails (Tor, direct fallback)."""

    def name(self) -> str:
        return "webdiver"

    def version(self) -> str:
        return "http-keyless"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        return 1  # Tor

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        domain = clean_domain(seed)
        if not domain:
            return []
        resp = None
        for scheme in ("https://", "http://"):
            resp = http_get(scheme + domain, use_tor=True)
            if resp is not None:
                break
        if resp is None:
            return []
        text = resp.text or ""
        title_match = _TITLE_RE.search(text)
        title = (
            re.sub(r"\s+", " ", title_match.group(1)).strip()[:300] if title_match else ""
        )
        generator_match = _GENERATOR_RE.search(text)
        emails = sorted({e.lower() for e in _EMAIL_RE.findall(text)})[:25]
        data = {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "server": resp.headers.get("server", ""),
            "powered_by": resp.headers.get("x-powered-by", ""),
            "content_type": resp.headers.get("content-type", ""),
            "title": title,
            "generator": generator_match.group(1)[:200] if generator_match else "",
            "emails": emails,
            "final_host": clean_domain(str(resp.url)),
        }
        return [data]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            units.append(
                self.make_evidence(
                    source_platform="domain",
                    source_tier=2,
                    seed_value="",
                    result_type="domain_hit",
                    result_value=str(data.get("url", "domain")),
                    platform_enrichment=data,
                )
            )
        return units
