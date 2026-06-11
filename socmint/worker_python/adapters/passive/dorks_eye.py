"""DorksEyeAdapter — Tier 3 search-engine footprint via keyless DuckDuckGo dorks.

The upstream ``dorks-eye`` project is an *interactive* Google scraper that
cannot run unattended and is blocked over Tor. This adapter keeps the tool's
intent — surfacing a seed's public web footprint with dork queries — but drives
it through DuckDuckGo HTML (no API key, Tor-routed with automatic direct fallback).
"""
from __future__ import annotations

import re

from worker_python.adapters._net import ddg_search, select_dorks
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class DorksEyeAdapter(ToolAdapter):
    """Public-footprint dorking for {seed} via DuckDuckGo (Tor, direct fallback)."""

    MAX_DORKS = 6

    # Generic footprint dorks (run for every seed type).
    BASE_DORKS = (
        '"{q}"',
        '"{q}" (site:linkedin.com OR site:twitter.com OR site:x.com '
        "OR site:facebook.com OR site:instagram.com OR site:t.me OR site:reddit.com)",
        '"{q}" (filetype:pdf OR filetype:xlsx OR filetype:doc OR filetype:csv)',
    )
    # Handle-centric: profile pages, link aggregators, dev/community platforms.
    USERNAME_DORKS = (
        'intext:"{q}" (site:github.com OR site:gitlab.com OR site:medium.com OR site:reddit.com)',
        'inurl:{q} (site:about.me OR site:keybase.io OR site:linktr.ee OR site:gravatar.com)',
        '"{q}" (intitle:profile OR intext:"user profile" OR intext:"member since")',
    )
    # Email-centric: contact pages, code commits, people-search aggregators.
    EMAIL_DORKS = (
        '"{q}" (site:github.com OR site:gist.github.com OR site:gitlab.com)',
        '"{q}" (intext:contact OR intext:email OR intext:"reach me" OR intext:"get in touch")',
        '"{q}" (site:hunter.io OR site:rocketreach.co OR site:apollo.io OR site:signalhire.com)',
    )
    # Phone-centric: caller-ID directories, messaging/contact pages.
    PHONE_DORKS = (
        '"{q}" (intext:whatsapp OR intext:telegram OR intext:contact OR intext:mobile)',
        '"{q}" (site:truecaller.com OR site:whocallsme.com OR site:sync.me OR site:shouldianswer.com)',
        '"{q}" (intext:phone OR intext:"call me" OR intext:"reach me")',
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
