"""AhmiaAdapter — Tier 3 passive dark-web recon via the Ahmia onion search index.

Ahmia (https://ahmia.fi) is a clearnet gateway to a curated Tor hidden-service
index.  This adapter queries it as a search engine — strictly passive (reading
Ahmia's existing index, never crawling onion sites) — and emits ``onion_hit``
evidence units for pages mentioning the seed.

Routed through Tor (``get_proxy_tier() == 1``) for anonymised egress, same
pattern as the existing dork tools.  Keyless, no API — HTML scrape of the
search results page.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote_plus

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_SEARCH_URL = "https://ahmia.fi/search/?q={query}"
_MAX_RESULTS = 15

_TAG_RE = re.compile(r"<[^>]+>")
# Ahmia results are in <li class="result"> blocks with <a href="..."> and <p> snippet
_RESULT_RE = re.compile(
    r'<li[^>]*class="[^"]*result[^"]*"[^>]*>.*?'
    r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<p[^>]*>(.*?)</p>',
    re.I | re.S,
)
# Fallback: simpler link extraction
_LINK_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]*\.onion[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S,
)


def _strip_html(fragment: str) -> str:
    return unescape(_TAG_RE.sub("", fragment or "")).strip()


class AhmiaAdapter(ToolAdapter):
    """Tor-routed dark-web passive search via Ahmia's clearnet index."""

    def name(self) -> str:
        return "ahmia"

    def version(self) -> str:
        return "ahmia-search"

    def get_tool_tier(self) -> int:
        return 3

    def get_proxy_tier(self) -> int:
        return 1  # Tor-routed for anonymised egress

    def health_check(self) -> bool:
        return True  # keyless; availability checked at runtime

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        seed = (seed or "").strip()
        if not seed:
            return []

        url = _SEARCH_URL.format(query=quote_plus(seed))
        resp = http_get(url, use_tor=True, timeout=30)
        if resp is None or resp.status_code != 200:
            return []

        html = resp.text or ""
        if not html:
            return []

        results: list[dict] = []
        seen: set[str] = set()

        # Try structured result blocks first
        for href, title, snippet in _RESULT_RE.findall(html):
            if len(results) >= _MAX_RESULTS:
                break
            href = href.strip()
            if href in seen or not href:
                continue
            seen.add(href)
            results.append({
                "url": href,
                "title": _strip_html(title)[:200],
                "snippet": _strip_html(snippet)[:500],
            })

        # Fallback: extract .onion links directly
        if not results:
            for href, title in _LINK_RE.findall(html):
                if len(results) >= _MAX_RESULTS:
                    break
                href = href.strip()
                if href in seen or not href:
                    continue
                seen.add(href)
                results.append({
                    "url": href,
                    "title": _strip_html(title)[:200],
                    "snippet": "",
                })

        return results

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "").strip()
            title = item.get("title", "").strip()
            snippet = item.get("snippet", "").strip()
            if not url:
                continue

            display = title or url[:100]
            notes_parts = ["source=ahmia"]
            if title:
                notes_parts.append(f"title={title[:80]}")
            if snippet:
                notes_parts.append(f"snippet={snippet[:200]}")

            units.append(
                self.make_evidence(
                    source_platform="darkweb",
                    source_tier=3,  # archive/index
                    seed_value=self._seed_value,
                    result_type="onion_hit",
                    result_value=url[:500],
                    confidence_raw=0.35,
                    notes=" ".join(notes_parts)[:2000],
                )
            )
        return units
