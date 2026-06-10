"""MastOSINTAdapter — Tier 4 Mastodon profile/post search (Section 11.32).

Reimplemented key-less against the public Mastodon REST API
(``/api/v1/accounts/lookup``), which resolves a handle to a full public profile
without authentication. The adapter probes the largest open instances; a 200
with an account ``id`` is a confirmed account. The old adapter shelled out to a
``mastosint.py`` script that was never present, so it always returned empty.
"""
from __future__ import annotations

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

# Largest open instances to resolve a bare handle against.
_INSTANCES = (
    "mastodon.social",
    "mas.to",
    "mstdn.social",
    "infosec.exchange",
)
_LOOKUP = "https://{instance}/api/v1/accounts/lookup?acct={acct}"


class MastOSINTAdapter(ToolAdapter):
    """Keyless Mastodon account lookup across major open instances."""

    def name(self) -> str:
        return "mastosint"

    def version(self) -> str:
        return "masto-api"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        acct = (seed or "").strip().lstrip("@")
        if not acct:
            return []
        # If the seed already carries an instance (user@instance), query it first.
        instances = list(_INSTANCES)
        if "@" in acct:
            user, _, host = acct.partition("@")
            if host:
                instances = [host, *instances]
                acct = user
        hits: list[dict] = []
        for instance in instances:
            resp = http_get(_LOOKUP.format(instance=instance, acct=acct), timeout=12)
            if resp is None or resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data.get("id"):
                data["_instance"] = instance
                hits.append(data)
                break  # first confirmed instance is enough
        return hits

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            instance = data.get("_instance", "")
            acct = data.get("acct") or data.get("username") or ""
            enrichment = {
                "id": data.get("id"),
                "instance": instance,
                "acct": acct,
                "display_name": data.get("display_name"),
                "note": (data.get("note") or "")[:500],
                "followers": data.get("followers_count"),
                "following": data.get("following_count"),
                "statuses": data.get("statuses_count"),
                "created_at": data.get("created_at"),
                "avatar": data.get("avatar"),
            }
            units.append(
                self.make_evidence(
                    source_platform="mastodon",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=data.get("url") or f"https://{instance}/@{acct}",
                    platform_enrichment=enrichment,
                )
            )
        return units
