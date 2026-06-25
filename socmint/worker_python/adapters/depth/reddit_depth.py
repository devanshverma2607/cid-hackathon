"""SDM depth adapter — Reddit public JSON endpoints (keyless)."""
from __future__ import annotations

import logging
from typing import Optional

from worker_python.adapters.depth.base_depth import DepthAdapter

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; SOCMINT-SDM/1.0)"


class RedditDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_reddit_depth"

    def health_check(self) -> bool:
        return True

    def hydrate(self, username: str, url: str) -> Optional[dict]:
        api_url = f"https://www.reddit.com/user/{username}/about.json"
        data = self._safe_get(api_url)
        if not data:
            return None
        user = data.get("data") or data
        if user.get("is_suspended"):
            return None
        return {
            "display_name": user.get("subreddit", {}).get("title") or username,
            "username": user.get("name") or username,
            "bio": user.get("subreddit", {}).get("public_description") or "",
            "bio_link": "",
            "follower_count": user.get("subreddit", {}).get("subscribers", 0),
            "following_count": 0,
            "post_count": user.get("link_karma", 0) + user.get("comment_karma", 0),
            "created_at": "",  # Reddit returns epoch; handled below
            "verified": user.get("verified", False),
            "visibility": "public",
            "location": "",
            "website": "",
            "avatar_url": user.get("icon_img") or user.get("snoovatar_img") or "",
        }

    def collect_posts(self, username: str, url: str, max_posts: int = 200) -> Optional[list[dict]]:
        posts: list[dict] = []
        after = ""
        for _ in range(max_posts // 25 + 1):
            api_url = f"https://www.reddit.com/user/{username}/submitted.json?limit=25&raw_json=1"
            if after:
                api_url += f"&after={after}"
            data = self._safe_get(api_url)
            if not data or "data" not in data:
                break
            children = data["data"].get("children", [])
            for child in children:
                d = child.get("data", {})
                created = d.get("created_utc")
                if created:
                    from datetime import datetime, timezone
                    ts = datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
                    posts.append({"timestamp": ts, "post_type": d.get("post_hint", "text")})
            after = data["data"].get("after")
            if not after or len(posts) >= max_posts:
                break
        return posts[:max_posts] if posts else None

    def collect_interactions(self, username: str, url: str) -> Optional[dict]:
        """Collect subreddits the user posts in as pseudo-interaction targets."""
        api_url = f"https://www.reddit.com/user/{username}/submitted.json?limit=100&raw_json=1"
        data = self._safe_get(api_url)
        if not data or "data" not in data:
            return None
        subs: dict[str, int] = {}
        for child in data["data"].get("children", []):
            sub = child.get("data", {}).get("subreddit")
            if sub:
                subs[sub] = subs.get(sub, 0) + 1
        return subs if subs else None

    def collect_communities(self, username: str, url: str) -> Optional[list[dict]]:
        """Derive community list from posting history subreddits."""
        interactions = self.collect_interactions(username, url)
        if not interactions:
            return None
        return [{"name": f"r/{sub}", "platform": "reddit"} for sub in sorted(interactions, key=interactions.get, reverse=True)[:20]]
