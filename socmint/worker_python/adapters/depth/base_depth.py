"""Base class for Social Depth Module per-platform depth adapters.

Every depth adapter extends ToolAdapter and adds three SDM-specific hooks:
  - hydrate()   — full profile snapshot
  - collect_posts()   — public post timestamps (NOT content)
  - collect_interactions()   — @-mention / reply graph

All methods return plain dicts; the Celery task layer handles persistence.
Adapters must handle private/blocked profiles by returning ``None`` — never
by raising.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from worker_python.adapters.base import ToolAdapter

logger = logging.getLogger(__name__)

# Hard cap on posts per platform (env-overridable).
MAX_POSTS = int(os.environ.get("SDM_MAX_POSTS_PER_PLATFORM", "200"))
MAX_INTERACTIONS = int(os.environ.get("SDM_MAX_INTERACTION_TARGETS", "20"))
MAX_COMMENTS = int(os.environ.get("SDM_MAX_COMMENT_THREADS", "50"))


class DepthAdapter(ToolAdapter):
    """Abstract base for SDM depth adapters."""

    # ---- ToolAdapter abstract interface (defaults for depth adapters) --------
    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        return 2  # direct by default; override to 1 for Tor

    def version(self) -> str:
        return "sdm-1.0"

    def run(self, seed: str) -> list[dict]:
        """Not used directly by SDM — prefer hydrate/collect_posts."""
        return []

    def parse(self, raw: list[dict]) -> list:
        """Not used directly by SDM."""
        return []

    # ---- SDM-specific hooks -------------------------------------------------
    def hydrate(self, username: str, url: str) -> Optional[dict]:
        """Collect full public profile snapshot.

        Returns a dict with keys: display_name, bio, bio_link, follower_count,
        following_count, post_count, created_at, verified, visibility, location,
        website.  Returns None if the profile is private/unavailable.
        """
        return None

    def collect_posts(self, username: str, url: str, max_posts: int = MAX_POSTS) -> Optional[list[dict]]:
        """Collect post timestamps (ISO 8601) from public profile.

        Returns a list of {timestamp, post_type} dicts.  Does NOT collect post
        content (content is personal data; timestamps are metadata sufficient
        for behavioral analysis).  Returns None if unavailable.
        """
        return None

    def collect_interactions(self, username: str, url: str) -> Optional[dict]:
        """Collect @-mention / reply interaction graph from public posts.

        Returns {target_username: interaction_count} map.  Returns None if
        unavailable.
        """
        return None

    def collect_communities(self, username: str, url: str) -> Optional[list[dict]]:
        """Collect publicly visible community memberships.

        Returns a list of {name, platform} dicts.  Returns None if unavailable.
        """
        return None

    # ---- helpers ------------------------------------------------------------
    def _safe_get(self, url: str, **kwargs) -> Optional[dict]:
        """SSRF-safe HTTP GET returning parsed JSON or None."""
        try:
            from worker_python.adapters._net import http_get, is_safe_url
            if not is_safe_url(url):
                return None
            resp = http_get(url, **kwargs)
            if resp is None:
                return None
            if isinstance(resp, dict):
                return resp
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("depth adapter GET failed for %s: %s", url, exc)
            return None

    def _safe_get_text(self, url: str, **kwargs) -> Optional[str]:
        """SSRF-safe HTTP GET returning raw text or None."""
        try:
            from worker_python.adapters._net import is_safe_url
            if not is_safe_url(url):
                return None
            import httpx
            with httpx.Client(timeout=25.0, follow_redirects=True) as client:
                resp = client.get(url, **kwargs)
                if resp.status_code == 200:
                    return resp.text
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("depth adapter text GET failed for %s: %s", url, exc)
            return None
