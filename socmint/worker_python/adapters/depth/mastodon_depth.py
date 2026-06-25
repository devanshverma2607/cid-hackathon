"""SDM depth adapter — Mastodon public API (keyless, federated)."""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from worker_python.adapters.depth.base_depth import DepthAdapter

logger = logging.getLogger(__name__)


class MastodonDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_mastodon_depth"

    def health_check(self) -> bool:
        return True

    def _instance_and_user(self, username: str, url: str) -> tuple[str, str]:
        """Extract (instance_url, account_handle) from a profile URL."""
        if url and "://" in url:
            parsed = urlparse(url)
            instance = f"{parsed.scheme}://{parsed.netloc}"
            handle = parsed.path.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
            return instance, handle
        return "https://mastodon.social", username.lstrip("@")

    def _lookup_account_id(self, instance: str, handle: str) -> Optional[str]:
        api_url = f"{instance}/api/v1/accounts/lookup?acct={handle}"
        data = self._safe_get(api_url)
        if data and data.get("id"):
            return str(data["id"])
        return None

    def hydrate(self, username: str, url: str) -> Optional[dict]:
        instance, handle = self._instance_and_user(username, url)
        api_url = f"{instance}/api/v1/accounts/lookup?acct={handle}"
        data = self._safe_get(api_url)
        if not data or not data.get("id"):
            return None
        return {
            "display_name": data.get("display_name") or "",
            "username": data.get("acct") or handle,
            "bio": data.get("note") or "",
            "bio_link": "",
            "follower_count": data.get("followers_count", 0),
            "following_count": data.get("following_count", 0),
            "post_count": data.get("statuses_count", 0),
            "created_at": data.get("created_at") or "",
            "verified": False,
            "visibility": "public" if not data.get("locked") else "private",
            "location": "",
            "website": data.get("url") or "",
            "avatar_url": data.get("avatar") or "",
        }

    def collect_posts(self, username: str, url: str, max_posts: int = 200) -> Optional[list[dict]]:
        instance, handle = self._instance_and_user(username, url)
        acct_id = self._lookup_account_id(instance, handle)
        if not acct_id:
            return None
        posts: list[dict] = []
        api_url = f"{instance}/api/v1/accounts/{acct_id}/statuses?limit=40&exclude_replies=false"
        max_id = None
        for _ in range(max_posts // 40 + 1):
            page_url = api_url + (f"&max_id={max_id}" if max_id else "")
            data = self._safe_get(page_url)
            if not data or not isinstance(data, list) or len(data) == 0:
                break
            for status in data:
                ts = status.get("created_at")
                if ts:
                    reblog = "reblog" if status.get("reblog") else "text"
                    posts.append({"timestamp": ts, "post_type": reblog})
                max_id = status.get("id")
            if len(posts) >= max_posts:
                break
        return posts[:max_posts] if posts else None

    def collect_interactions(self, username: str, url: str) -> Optional[dict]:
        """Extract mentions from collected statuses."""
        instance, handle = self._instance_and_user(username, url)
        acct_id = self._lookup_account_id(instance, handle)
        if not acct_id:
            return None
        mentions: dict[str, int] = {}
        api_url = f"{instance}/api/v1/accounts/{acct_id}/statuses?limit=40"
        data = self._safe_get(api_url)
        if not data or not isinstance(data, list):
            return None
        for status in data:
            for mention in status.get("mentions", []):
                acct = mention.get("acct")
                if acct:
                    mentions[acct] = mentions.get(acct, 0) + 1
        return mentions if mentions else None
