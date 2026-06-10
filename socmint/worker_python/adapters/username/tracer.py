"""TracerAdapter — Tier 2 identity-reuse tracking (Section 11.7).

Reimplemented key-less. The upstream ``tracer`` project was never cloned into
the image (the old adapter shelled out to ``python tracer.py`` and silently
returned empty). This implementation tracks handle *reuse* across the web via
the keyless search backend: it searches the quoted username and surfaces the
distinct domains where that exact handle is indexed — a search-engine angle that
complements the direct-probe username tools.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from worker_python.adapters._net import ddg_search, is_username
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class TracerAdapter(ToolAdapter):
    """Keyless username→search-indexed identity-reuse tracker."""

    def name(self) -> str:
        return "tracer"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        handle = (seed or "").strip().lstrip("@")
        if not is_username(handle):
            return []
        seen_domains: set[str] = set()
        results: list[dict] = []
        for query in (f'"{handle}" profile', f'intitle:"{handle}"'):
            for hit in ddg_search(query, max_results=20):
                url = hit.get("url", "")
                host = urlparse(url).netloc.lower().lstrip("www.")
                if not host or host in seen_domains:
                    continue
                # Require the handle to actually appear in the URL or title so a
                # generic search result is not mistaken for identity reuse.
                hay = f"{url} {hit.get('title', '')}".lower()
                if not re.search(rf"(?<![a-z0-9]){re.escape(handle.lower())}(?![a-z0-9])", hay):
                    continue
                seen_domains.add(host)
                results.append({"url": url, "domain": host, "title": hit.get("title", "")})
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
                    source_tier=4,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                    notes=item.get("title") or "search_indexed",
                )
            )
        return units
