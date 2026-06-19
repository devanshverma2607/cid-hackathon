"""ForumSweepAdapter — Tier 3 forum / blog / comment-section footprint sweep.

The feature requires collecting publicly accessible data from *forums, blogs,
comment sections, and open web sources* — not just social-network profile pages.
This adapter drives the shared key-less search backend (DuckDuckGo HTML with a
Mojeek fallback, Tor-routed) with ``site:`` dorks scoped to the major community,
Q&A, blogging and comment platforms, recording every public page that mentions
the seed as a ``dork_hit``.

It complements ``dorks_eye`` (generic footprint) and ``hunt_pastebin`` (paste
leaks) by specifically targeting discussion/authorship surfaces where a subject
participates under a reused identifier.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import ddg_search, select_dorks
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

# Community / forum / Q&A platforms.
_FORUM_SITES = (
    "reddit.com", "news.ycombinator.com", "quora.com", "stackexchange.com",
    "stackoverflow.com", "forums.somethingawful.com", "4chan.org",
    "community.spiceworks.com", "discourse.org", "lemmy.world",
)
# Blogging / publishing platforms.
_BLOG_SITES = (
    "medium.com", "substack.com", "dev.to", "wordpress.com", "blogspot.com",
    "tumblr.com", "hashnode.dev", "ghost.io", "livejournal.com", "telegra.ph",
)
# Comment / discussion overlay systems.
_COMMENT_SITES = ("disqus.com", "intensedebate.com", "news.ycombinator.com")


def _site_clause(sites: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"site:{s}" for s in sites) + ")"


class ForumSweepAdapter(ToolAdapter):
    """Forum / blog / comment-section presence sweep for {seed} (keyless DDG)."""

    MAX_DORKS = 6

    BASE_DORKS = (
        '"{q}" ' + _site_clause(_FORUM_SITES),
        '"{q}" ' + _site_clause(_BLOG_SITES),
        '"{q}" ' + _site_clause(_COMMENT_SITES),
    )
    USERNAME_DORKS = (
        'inurl:{q} ' + _site_clause(_FORUM_SITES),
        '"{q}" (intext:"posted by" OR intext:"author" OR intext:"member since" OR intext:"comments")',
        'inurl:user "{q}" ' + _site_clause(_BLOG_SITES),
    )
    EMAIL_DORKS = (
        '"{q}" (intext:comment OR intext:"posted by" OR intext:author)',
        '"{q}" ' + _site_clause(_BLOG_SITES + _FORUM_SITES),
    )
    PHONE_DORKS = (
        '"{q}" (intext:contact OR intext:comment OR intext:forum)',
    )

    def name(self) -> str:
        return "forum_sweep"

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
                    source_platform=platform,
                    source_tier=2,
                    seed_value="",
                    result_type="dork_hit",
                    result_value=url,
                    notes=(item.get("title") or "")[:500],
                )
            )
        return units
