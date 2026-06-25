"""SDM depth adapter — GitHub public API (keyless, rate-limited to 60/hr)."""
from __future__ import annotations

import logging
from typing import Optional

from worker_python.adapters.depth.base_depth import DepthAdapter

logger = logging.getLogger(__name__)


class GitHubDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_github_depth"

    def health_check(self) -> bool:
        return True

    def hydrate(self, username: str, url: str) -> Optional[dict]:
        api_url = f"https://api.github.com/users/{username}"
        data = self._safe_get(api_url)
        if not data or data.get("message"):
            return None
        return {
            "display_name": data.get("name") or "",
            "username": data.get("login") or username,
            "bio": data.get("bio") or "",
            "bio_link": data.get("blog") or "",
            "follower_count": data.get("followers", 0),
            "following_count": data.get("following", 0),
            "post_count": data.get("public_repos", 0),
            "created_at": data.get("created_at") or "",
            "verified": False,
            "visibility": "public",
            "location": data.get("location") or "",
            "website": data.get("blog") or "",
            "avatar_url": data.get("avatar_url") or "",
            "company": data.get("company") or "",
            "twitter_username": data.get("twitter_username") or "",
        }

    def collect_posts(self, username: str, url: str, max_posts: int = 200) -> Optional[list[dict]]:
        """Collect public event timestamps (pushes, issues, etc.)."""
        events: list[dict] = []
        for page in range(1, (max_posts // 30) + 2):
            api_url = f"https://api.github.com/users/{username}/events/public?page={page}&per_page=30"
            data = self._safe_get(api_url)
            if not data or not isinstance(data, list):
                break
            for event in data:
                ts = event.get("created_at")
                if ts:
                    events.append({
                        "timestamp": ts,
                        "post_type": event.get("type", "event"),
                    })
            if len(events) >= max_posts or len(data) < 30:
                break
        return events[:max_posts] if events else None

    def collect_interactions(self, username: str, url: str) -> Optional[dict]:
        return None  # GitHub events don't carry @-mention targets reliably

    def collect_communities(self, username: str, url: str) -> Optional[list[dict]]:
        """Collect public org memberships."""
        api_url = f"https://api.github.com/users/{username}/orgs"
        data = self._safe_get(api_url)
        if not data or not isinstance(data, list):
            return None
        return [{"name": org.get("login", ""), "platform": "github"} for org in data if org.get("login")]
