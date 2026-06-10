"""ZehefAdapter — Tier 1 email sweep (Section 11.12).

Reimplemented key-less. The upstream ``zehef`` project was never cloned into the
image (the old adapter shelled out to ``python zehef.py`` and silently returned
empty). This implementation surfaces *search-indexed* mentions of the email
across the public web (paste sites, forums, profile pages) via the keyless
search backend — a distinct signal from the direct registration probes
(``holehe``/``socialscan``).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from worker_python.adapters._net import ddg_search
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ZehefAdapter(ToolAdapter):
    """Keyless email→search-indexed presence sweep."""

    def name(self) -> str:
        return "zehef"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 1

    def get_proxy_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []
        seen: set[str] = set()
        results: list[dict] = []
        for hit in ddg_search(f'"{email}"', max_results=25):
            url = hit.get("url", "")
            host = urlparse(url).netloc.lower().lstrip("www.")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "email": email,
                    "url": url,
                    "domain": host,
                    "title": hit.get("title", ""),
                    "snippet": hit.get("snippet", ""),
                }
            )
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            units.append(
                self.make_evidence(
                    source_platform=item.get("domain", "web"),
                    source_tier=3,
                    seed_value=item.get("email", ""),
                    result_type="email_registered",
                    result_value=url,
                    notes=(item.get("title") or item.get("snippet") or "search_indexed")[:300],
                )
            )
        return units
