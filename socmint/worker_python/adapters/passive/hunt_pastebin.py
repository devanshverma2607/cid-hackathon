"""HuntPastebinAdapter — Tier 3 paste-site leak sweep via keyless DuckDuckGo.

There is no installable ``huntpastebin`` CLI; this adapter searches the major
public paste sites for the seed through DuckDuckGo HTML (no API key, Tor-routed
with direct fallback) and records each hit as an archive_hit.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import ddg_search
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

PASTE_SITES = (
    "pastebin.com",
    "ghostbin.com",
    "rentry.co",
    "throwbin.io",
    "controlc.com",
    "paste.ee",
    "justpaste.it",
    "0bin.net",
    "hastebin.com",
    "dpaste.com",
)


class HuntPastebinAdapter(ToolAdapter):
    """Paste-site leak sweep for {seed} via DuckDuckGo (Tor, direct fallback)."""

    def name(self) -> str:
        return "huntpastebin"

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
        sites = " OR ".join(f"site:{s}" for s in PASTE_SITES)
        seen: set[str] = set()
        out: list[dict] = []
        for query in (f'"{seed}" ({sites})', f"{seed} ({sites})"):
            for hit in ddg_search(query, max_results=15, use_tor=True):
                url = hit.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    out.append({"url": url, "title": hit.get("title", "")})
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
                    source_platform=platform or "pastebin",
                    source_tier=3,
                    seed_value="",
                    result_type="archive_hit",
                    result_value=url,
                    notes=(item.get("title") or "")[:500],
                )
            )
        return units
