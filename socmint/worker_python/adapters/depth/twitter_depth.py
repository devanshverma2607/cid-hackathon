"""SDM depth adapter — Twitter/X public scraping (keyless, best-effort).

Twitter's public availability is heavily restricted; this adapter uses the
syndication / guest API endpoints that return limited public data. Falls back
to unavailable gracefully when blocked.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from worker_python.adapters.depth.base_depth import DepthAdapter

logger = logging.getLogger(__name__)


class TwitterDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_twitter_depth"

    def health_check(self) -> bool:
        return True

    def get_proxy_tier(self) -> int:
        return 1  # Route through Tor — Twitter blocks aggressively

    def hydrate(self, username: str, url: str) -> Optional[dict]:
        """Try the syndication API for basic profile data."""
        syn_url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        html = self._safe_get_text(syn_url)
        if not html:
            return None
        try:
            # Extract the embedded JSON from the syndication page
            match = re.search(r'data-props="({.*?})"', html)
            if not match:
                match = re.search(r'<script[^>]*>.*?(\{.*?"user".*?\}).*?</script>', html, re.S)
            if not match:
                return None
            data = json.loads(match.group(1).replace("&quot;", '"'))
            user = data.get("user") or data.get("props", {}).get("pageProps", {}).get("user") or {}
            if not user:
                return None
            return {
                "display_name": user.get("name") or "",
                "username": user.get("screen_name") or username,
                "bio": user.get("description") or "",
                "bio_link": "",
                "follower_count": user.get("followers_count", 0),
                "following_count": user.get("friends_count", 0),
                "post_count": user.get("statuses_count", 0),
                "created_at": user.get("created_at") or "",
                "verified": user.get("verified", False),
                "visibility": "private" if user.get("protected") else "public",
                "location": user.get("location") or "",
                "website": "",
                "avatar_url": user.get("profile_image_url_https") or "",
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("twitter hydration parse failed: %s", exc)
            return None

    def collect_posts(self, username: str, url: str, max_posts: int = 200) -> Optional[list[dict]]:
        # Twitter severely limits public timeline access without auth
        return None

    def collect_interactions(self, username: str, url: str) -> Optional[dict]:
        return None
