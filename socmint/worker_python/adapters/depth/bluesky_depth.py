"""SDM depth adapter — Bluesky AT Protocol public API (keyless)."""
from __future__ import annotations
import logging
from typing import Optional
from worker_python.adapters.depth.base_depth import DepthAdapter

logger = logging.getLogger(__name__)
_BSKY_API = "https://public.api.bsky.app"


class BlueskyDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_bluesky_depth"

    def health_check(self) -> bool:
        return True

    def _resolve_handle(self, username: str) -> str:
        h = username.lstrip("@")
        return f"{h}.bsky.social" if "." not in h else h

    def hydrate(self, username: str, url: str) -> Optional[dict]:
        h = self._resolve_handle(username)
        data = self._safe_get(f"{_BSKY_API}/xrpc/app.bsky.actor.getProfile?actor={h}")
        if not data or data.get("error"):
            return None
        return {
            "display_name": data.get("displayName") or "",
            "username": data.get("handle") or h,
            "bio": data.get("description") or "",
            "bio_link": "", "follower_count": data.get("followersCount", 0),
            "following_count": data.get("followsCount", 0),
            "post_count": data.get("postsCount", 0),
            "created_at": data.get("createdAt") or "",
            "verified": False, "visibility": "public",
            "location": "", "website": "",
            "avatar_url": data.get("avatar") or "",
        }

    def collect_posts(self, username: str, url: str, max_posts: int = 200) -> Optional[list[dict]]:
        h = self._resolve_handle(username)
        posts, cursor = [], None
        for _ in range(max_posts // 50 + 1):
            u = f"{_BSKY_API}/xrpc/app.bsky.feed.getAuthorFeed?actor={h}&limit=50"
            if cursor:
                u += f"&cursor={cursor}"
            data = self._safe_get(u)
            if not data or data.get("error"):
                break
            for item in data.get("feed", []):
                ts = item.get("post", {}).get("record", {}).get("createdAt")
                if ts:
                    posts.append({"timestamp": ts, "post_type": "repost" if item.get("reason") else "text"})
            cursor = data.get("cursor")
            if not cursor or len(posts) >= max_posts:
                break
        return posts[:max_posts] if posts else None

    def collect_interactions(self, username: str, url: str) -> Optional[dict]:
        return None
