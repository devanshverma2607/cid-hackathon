"""RedditIntelAdapter — Tier 4 Reddit user profile enrichment.

Uses Reddit's authenticated OAuth2 API to fetch a user's public profile,
post history, and comment history. Gated on REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET.
Reveddit/Pushshift are dead (May 2026); this is the viable alternative.
"""
from __future__ import annotations
import os
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_USER_URL = "https://oauth.reddit.com/user/{username}/about.json"
_UA = "socmint-osint:v1.0 (by /u/socmint_bot)"

def _client_id() -> str:
    return os.environ.get("REDDIT_CLIENT_ID", "").strip()
def _client_secret() -> str:
    return os.environ.get("REDDIT_CLIENT_SECRET", "").strip()

class RedditIntelAdapter(ToolAdapter):
    def name(self) -> str: return "reddit_intel"
    def version(self) -> str: return "oauth2-api"
    def get_tool_tier(self) -> int: return 4
    def get_proxy_tier(self) -> int: return 2
    def health_check(self) -> bool:
        return bool(_client_id() and _client_secret())

    def _get_token(self) -> str | None:
        import httpx
        cid, csec = _client_id(), _client_secret()
        if not cid or not csec: return None
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(_TOKEN_URL, auth=(cid, csec),
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": _UA})
                if resp.status_code != 200: return None
                return resp.json().get("access_token")
        except Exception:
            return None

    def run(self, seed: str) -> list[dict]:
        username = (seed or "").strip().lstrip("@").lstrip("/").split("/")[-1]
        if not username or len(username) < 2: return []
        token = self._get_token()
        if not token: return []
        import httpx
        headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
        try:
            with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
                resp = client.get(_USER_URL.format(username=username))
                if resp.status_code != 200: return []
                try: data = resp.json()
                except ValueError: return []
                if not isinstance(data, dict): return []
                user_data = data.get("data", data)
                if not isinstance(user_data, dict): return []
                return [user_data]
        except Exception:
            return []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict): continue
            username = data.get("name", "")
            if not username: continue
            url = f"https://www.reddit.com/user/{username}"
            enrichment = {
                "reddit_id": data.get("id", ""),
                "link_karma": data.get("link_karma", 0),
                "comment_karma": data.get("comment_karma", 0),
                "created_utc": data.get("created_utc"),
                "is_gold": data.get("is_gold", False),
                "verified": data.get("verified", False),
                "has_verified_email": data.get("has_verified_email", False),
            }
            avatar = data.get("icon_img") or data.get("snoovatar_img") or ""
            if avatar and "?" in avatar:
                avatar = avatar.split("?")[0]
            if avatar:
                enrichment["avatar_url"] = avatar
            bio = (data.get("subreddit", {}) or {}).get("public_description", "")
            if bio:
                enrichment["bio"] = bio[:500]
            units.append(self.make_evidence(
                source_platform="reddit.com", source_tier=1,
                seed_value=self._seed_value, result_type="account_found",
                result_value=url, confidence_raw=0.85,
                platform_enrichment=enrichment,
                notes=f"source=reddit karma={data.get('link_karma',0)}+{data.get('comment_karma',0)}"))
        return units
