"""SDM depth adapters — Instagram, TikTok, LinkedIn, Telegram, YouTube.

Each adapter provides best-effort keyless public data collection.
All return None on private/blocked profiles for graceful degradation.
"""
from __future__ import annotations
import json, logging, re
from typing import Optional
from worker_python.adapters.depth.base_depth import DepthAdapter

logger = logging.getLogger(__name__)


class InstagramDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_instagram_depth"
    def health_check(self) -> bool:
        return True
    def hydrate(self, username: str, url: str) -> Optional[dict]:
        html = self._safe_get_text(f"https://www.instagram.com/{username}/?__a=1&__d=dis")
        if not html:
            return None
        try:
            data = json.loads(html)
            user = data.get("graphql", {}).get("user") or data.get("user") or {}
            if not user or user.get("is_private"):
                return None
            return {
                "display_name": user.get("full_name") or "",
                "username": user.get("username") or username,
                "bio": user.get("biography") or "",
                "bio_link": user.get("external_url") or "",
                "follower_count": user.get("edge_followed_by", {}).get("count", 0),
                "following_count": user.get("edge_follow", {}).get("count", 0),
                "post_count": user.get("edge_owner_to_timeline_media", {}).get("count", 0),
                "created_at": "", "verified": user.get("is_verified", False),
                "visibility": "private" if user.get("is_private") else "public",
                "location": "", "website": user.get("external_url") or "",
                "avatar_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url") or "",
            }
        except Exception:
            return None
    def collect_posts(self, username, url, max_posts=200):
        return None  # Instagram requires auth for timeline
    def collect_interactions(self, username, url):
        return None


class TikTokDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_tiktok_depth"
    def health_check(self) -> bool:
        return True
    def hydrate(self, username: str, url: str) -> Optional[dict]:
        html = self._safe_get_text(f"https://www.tiktok.com/@{username}")
        if not html:
            return None
        try:
            m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
            if not m:
                return None
            data = json.loads(m.group(1))
            user_info = data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {})
            user = user_info.get("user", {})
            stats = user_info.get("stats", {})
            if not user:
                return None
            return {
                "display_name": user.get("nickname") or "",
                "username": user.get("uniqueId") or username,
                "bio": user.get("signature") or "",
                "bio_link": user.get("bioLink", {}).get("link") or "",
                "follower_count": stats.get("followerCount", 0),
                "following_count": stats.get("followingCount", 0),
                "post_count": stats.get("videoCount", 0),
                "created_at": "", "verified": user.get("verified", False),
                "visibility": "private" if user.get("privateAccount") else "public",
                "location": "", "website": "",
                "avatar_url": user.get("avatarLarger") or user.get("avatarMedium") or "",
            }
        except Exception:
            return None
    def collect_posts(self, username, url, max_posts=200):
        return None
    def collect_interactions(self, username, url):
        return None


class LinkedInDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_linkedin_depth"
    def health_check(self) -> bool:
        return True
    def hydrate(self, username: str, url: str) -> Optional[dict]:
        # LinkedIn blocks unauthenticated scraping heavily; best-effort only
        target = url if url and "linkedin.com" in url else f"https://www.linkedin.com/in/{username}"
        html = self._safe_get_text(target)
        if not html or "authwall" in html.lower():
            return None
        try:
            name_m = re.search(r'<title>(.*?)[\|–]', html)
            display_name = name_m.group(1).strip() if name_m else ""
            bio_m = re.search(r'"headline":"(.*?)"', html)
            bio = bio_m.group(1) if bio_m else ""
            return {
                "display_name": display_name, "username": username,
                "bio": bio, "bio_link": "",
                "follower_count": 0, "following_count": 0, "post_count": 0,
                "created_at": "", "verified": False,
                "visibility": "public", "location": "", "website": "",
                "avatar_url": "",
            }
        except Exception:
            return None
    def collect_posts(self, username, url, max_posts=200):
        return None
    def collect_interactions(self, username, url):
        return None


class TelegramDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_telegram_depth"
    def health_check(self) -> bool:
        return True
    def hydrate(self, username: str, url: str) -> Optional[dict]:
        html = self._safe_get_text(f"https://t.me/{username}")
        if not html:
            return None
        try:
            name_m = re.search(r'<div class="tgme_page_title[^"]*"><span[^>]*>(.*?)</span>', html)
            bio_m = re.search(r'<div class="tgme_page_description[^"]*">(.*?)</div>', html, re.S)
            members_m = re.search(r'<div class="tgme_page_extra">(.*?)</div>', html)
            if not name_m:
                return None
            return {
                "display_name": re.sub(r'<[^>]+>', '', name_m.group(1)).strip(),
                "username": username, "bio": re.sub(r'<[^>]+>', '', bio_m.group(1)).strip() if bio_m else "",
                "bio_link": "", "follower_count": 0, "following_count": 0,
                "post_count": 0, "created_at": "", "verified": False,
                "visibility": "public", "location": "", "website": "",
                "avatar_url": "",
            }
        except Exception:
            return None
    def collect_posts(self, username, url, max_posts=200):
        return None
    def collect_interactions(self, username, url):
        return None


class YouTubeDepthAdapter(DepthAdapter):
    def name(self) -> str:
        return "sdm_youtube_depth"
    def health_check(self) -> bool:
        return True
    def hydrate(self, username: str, url: str) -> Optional[dict]:
        target = url if url and "youtube.com" in url else f"https://www.youtube.com/@{username}"
        html = self._safe_get_text(target)
        if not html:
            return None
        try:
            title_m = re.search(r'"channelMetadataRenderer":\{"title":"(.*?)"', html)
            desc_m = re.search(r'"description":"(.*?)"', html)
            subs_m = re.search(r'"subscriberCountText":\{"simpleText":"(.*?)"', html)
            return {
                "display_name": title_m.group(1) if title_m else "",
                "username": username,
                "bio": desc_m.group(1)[:500] if desc_m else "",
                "bio_link": "", "follower_count": 0,
                "following_count": 0, "post_count": 0,
                "created_at": "", "verified": False,
                "visibility": "public", "location": "", "website": "",
                "avatar_url": "",
            }
        except Exception:
            return None
    def collect_posts(self, username, url, max_posts=200):
        return None
    def collect_interactions(self, username, url):
        return None
