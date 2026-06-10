"""DorksEyeAdapter — Tier 3 search-engine footprint via keyless DuckDuckGo dorks.

The upstream ``dorks-eye`` project is an *interactive* Google scraper that
cannot run unattended and is blocked over Tor. This adapter keeps the tool's
intent — surfacing a seed's public web footprint with dork queries — but drives
it through DuckDuckGo HTML (no API key, Tor-routed with automatic direct fallback).
"""
from __future__ import annotations

import re

from worker_python.adapters._net import ddg_search
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class DorksEyeAdapter(ToolAdapter):
    """Public-footprint dorking for {seed} via DuckDuckGo (Tor, direct fallback)."""

    DORKS = (
        '"{q}"',
        'intext:"{q}" (site:github.com OR site:gitlab.com OR site:reddit.com)',
        '"{q}" (site:linkedin.com OR site:twitter.com OR site:facebook.com '
        "OR site:instagram.com OR site:t.me)",
        '"{q}" (filetype:pdf OR filetype:xlsx OR filetype:doc OR filetype:csv)',
    )

    def name(self) -> str:
        return "dorks_eye"

    def version(self) -> str:
        return "ddg-keyless"

    def get_tool_tier(self) -> int:
        return 3

    def get_proxy_tier(self) -> int:
        return 1  # Tor

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        seed = (seed or "").strip()
        if not seed:
            return []
        seen: set[str] = set()
        out: list[dict] = []
        for template in self.DORKS:
            query = template.format(q=seed)
            for hit in ddg_search(query, max_results=10, use_tor=True):
                url = hit.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    out.append({"url": url, "title": hit.get("title", ""), "dork": query})
        return out

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            platform = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value="",
                    result_type="dork_hit",
                    result_value=url,
                    notes=(item.get("title") or item.get("dork") or "")[:500],
                )
            )
        return units
