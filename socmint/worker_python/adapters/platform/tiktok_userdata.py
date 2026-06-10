"""TikTokUserDataAdapter — Tier 4 TikTok metadata (Section 11.31).

Reimplemented key-less against the public TikTok web profile page. TikTok
aggressively fingerprints datacenter traffic, so this is a best-effort enricher:
when the embedded ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` JSON survives, we lift
the nickname / follower / heart / video counts; otherwise a 200 with the handle
present in the page still confirms the account exists. The old adapter shelled
out to a ``tiktok_userdata.py`` script that was never cloned, so it was always
empty.
"""
from __future__ import annotations

import json
import re

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_PROFILE_URL = "https://www.tiktok.com/@{user}"
_REHYDRATION_RE = re.compile(
    r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S
)


class TikTokUserDataAdapter(ToolAdapter):
    """Keyless best-effort TikTok public-profile lookup."""

    def name(self) -> str:
        return "tiktok_userdata"

    def version(self) -> str:
        return "tiktok-web"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    @staticmethod
    def _extract(html: str) -> dict:
        match = _REHYDRATION_RE.search(html or "")
        if not match:
            return {}
        try:
            blob = json.loads(match.group(1))
        except Exception:  # noqa: BLE001
            return {}
        try:
            detail = blob["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]
        except (KeyError, TypeError):
            return {}
        user = detail.get("user", {}) if isinstance(detail, dict) else {}
        stats = detail.get("stats", {}) if isinstance(detail, dict) else {}
        return {
            "unique_id": user.get("uniqueId"),
            "nickname": user.get("nickname"),
            "signature": (user.get("signature") or "")[:500],
            "verified": user.get("verified"),
            "private": user.get("privateAccount"),
            "followers": stats.get("followerCount"),
            "following": stats.get("followingCount"),
            "hearts": stats.get("heartCount"),
            "videos": stats.get("videoCount"),
        }

    def run(self, seed: str) -> list[dict]:
        user = (seed or "").strip().lstrip("@")
        if not user:
            return []
        resp = http_get(_PROFILE_URL.format(user=user), timeout=15)
        if resp is None or resp.status_code != 200:
            return []
        data = self._extract(resp.text)
        # Confirm existence even when anti-bot strips the JSON.
        if not data and f"@{user}".lower() not in (resp.text or "").lower():
            return []
        data["username"] = data.get("unique_id") or user
        data["profile_url"] = _PROFILE_URL.format(user=user)
        return [data]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            units.append(
                self.make_evidence(
                    source_platform="tiktok",
                    source_tier=1,
                    seed_value="",
                    result_type="account_found",
                    result_value=data.get("profile_url", "tiktok"),
                    platform_enrichment=data,
                )
            )
        return units
