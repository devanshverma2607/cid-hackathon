"""OSINTSkyAdapter — Tier 4 Bluesky investigation (Section 11.33).

Reimplemented key-less against the public Bluesky AppView API
(``app.bsky.actor.getProfile``), which needs no authentication and returns the
full public profile (DID, display name, follower/following counts, bio). The old
adapter shelled out to an ``osintssky.py`` script that was never cloned into the
image, so it always degraded to an empty result.
"""
from __future__ import annotations

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_PROFILE_API = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={actor}"


class OSINTSkyAdapter(ToolAdapter):
    """Keyless Bluesky profile lookup via the public AppView API."""

    def name(self) -> str:
        return "osintssky"

    def version(self) -> str:
        return "bsky-api"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    @staticmethod
    def _candidates(seed: str) -> list[str]:
        handle = (seed or "").strip().lstrip("@").lower()
        if not handle:
            return []
        if "." in handle:
            return [handle]
        # Bare handle → default Bluesky managed domain.
        return [f"{handle}.bsky.social", handle]

    def run(self, seed: str) -> list[dict]:
        for actor in self._candidates(seed):
            resp = http_get(_PROFILE_API.format(actor=actor), timeout=15)
            if resp is None or resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data.get("did"):
                data["_query_actor"] = actor
                return [data]
        return []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            handle = data.get("handle") or data.get("_query_actor") or ""
            enrichment = {
                "did": data.get("did"),
                "handle": handle,
                "display_name": data.get("displayName"),
                "description": (data.get("description") or "")[:500],
                "followers": data.get("followersCount"),
                "following": data.get("followsCount"),
                "posts": data.get("postsCount"),
                "avatar": data.get("avatar"),
            }
            units.append(
                self.make_evidence(
                    source_platform="bluesky",
                    source_tier=1,
                    seed_value="",
                    result_type="account_found",
                    result_value=f"https://bsky.app/profile/{handle}" if handle else "bluesky",
                    platform_enrichment=enrichment,
                )
            )
        return units
