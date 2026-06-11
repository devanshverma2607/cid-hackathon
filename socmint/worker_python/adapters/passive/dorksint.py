"""DorksintAdapter — Tier 3 exposure dorking via keyless DuckDuckGo.

No installable ``dorksint`` CLI exists; this adapter delivers the same intent —
surfacing credential/secret/file exposure that mentions the seed — through
DuckDuckGo HTML dork queries (no API key, Tor-routed with direct fallback).
"""
from __future__ import annotations

import re

from worker_python.adapters._net import ddg_search, select_dorks
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class DorksintAdapter(ToolAdapter):
    """Exposure/leak dorking for {seed} via DuckDuckGo (Tor, direct fallback)."""

    MAX_DORKS = 6

    # Generic exposure dorks (run for every seed type).
    BASE_DORKS = (
        '"{q}" (intext:password OR intext:passwd OR intext:credentials)',
        '"{q}" (ext:txt OR ext:log OR ext:env OR ext:sql OR ext:json OR ext:csv)',
        '"{q}" (site:pastebin.com OR site:ghostbin.com OR site:rentry.co '
        "OR site:gist.github.com OR site:controlc.com)",
    )
    # Email-centric: breach/leak/dump mentions and paste-site exposure.
    EMAIL_DORKS = (
        '"{q}" (intext:"data breach" OR intext:leaked OR intext:dump OR intext:combolist)',
        '"{q}" (site:pastebin.com OR site:throwbin.io OR site:psbdmp.ws OR site:dpaste.org)',
        '"{q}" (ext:sql OR ext:env OR ext:json) (intext:email OR intext:user OR intext:login)',
    )
    # Username-centric: secret/token leaks in code and config dumps.
    USERNAME_DORKS = (
        'intext:"{q}" (intext:api_key OR intext:secret OR intext:token OR intext:apikey)',
        '"{q}" (site:gist.github.com OR site:pastebin.com) (intext:config OR intext:".env")',
    )
    # Phone-centric: leaked databases / contact dumps.
    PHONE_DORKS = (
        '"{q}" (intext:leaked OR intext:dump OR intext:database OR intext:breach)',
        '"{q}" (site:pastebin.com OR site:psbdmp.ws) (intext:phone OR intext:contact OR intext:sms)',
    )

    def name(self) -> str:
        return "dorksint"

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
        dorks = select_dorks(
            seed, self.BASE_DORKS,
            {"username": self.USERNAME_DORKS, "email": self.EMAIL_DORKS, "phone": self.PHONE_DORKS},
            self.MAX_DORKS,
        )
        for template in dorks:
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
