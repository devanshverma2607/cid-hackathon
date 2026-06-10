"""SnapIntelAdapter — Tier 4 Snapchat lookup (Section 11.28).

Reimplemented key-less against the public Snapchat web profile endpoint
(``snapchat.com/add/{username}``). A 200 response indicates the public profile
exists; the page also embeds a JSON blob from which the display name and Bitmoji
/ Snapcode can be extracted. The old adapter shelled out to a ``snapintel.py``
script that was never cloned, so it always returned empty.
"""
from __future__ import annotations

import json
import re

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_PROFILE_URL = "https://www.snapchat.com/add/{user}"
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


class SnapIntelAdapter(ToolAdapter):
    """Keyless Snapchat public-profile existence + metadata lookup."""

    def name(self) -> str:
        return "snapintel"

    def version(self) -> str:
        return "snap-web"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    @staticmethod
    def _extract_profile(html: str) -> dict:
        match = _NEXT_DATA_RE.search(html or "")
        if not match:
            return {}
        try:
            blob = json.loads(match.group(1))
        except Exception:  # noqa: BLE001
            return {}
        try:
            page = blob["props"]["pageProps"]
        except (KeyError, TypeError):
            return {}
        info = page.get("userProfile", {}).get("publicProfileInfo", {}) if isinstance(page, dict) else {}
        if not isinstance(info, dict):
            return {}
        return {
            "title": info.get("title"),
            "username": info.get("username"),
            "snapcode_url": info.get("snapcodeImageUrl"),
            "bitmoji_url": (info.get("bitmoji3d") or {}).get("avatarImage", {}).get("url")
            if isinstance(info.get("bitmoji3d"), dict)
            else None,
            "subscriber_count": info.get("subscriberCount"),
        }

    def run(self, seed: str) -> list[dict]:
        user = (seed or "").strip().lstrip("@")
        if not user:
            return []
        resp = http_get(_PROFILE_URL.format(user=user), timeout=15)
        if resp is None or resp.status_code != 200:
            return []
        profile = self._extract_profile(resp.text)
        profile["username"] = profile.get("username") or user
        profile["profile_url"] = _PROFILE_URL.format(user=user)
        return [profile]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            units.append(
                self.make_evidence(
                    source_platform="snapchat",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=data.get("profile_url", "snapchat"),
                    platform_enrichment=data,
                )
            )
        return units
