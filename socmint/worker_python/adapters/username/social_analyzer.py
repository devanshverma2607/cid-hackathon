"""SocialAnalyzerAdapter — Tier 2 username search with metadata (Section 11.6).

Reimplemented key-less. The upstream ``social-analyzer`` Python module is not
installed in the image (the old adapter shelled out to ``python -m
social_analyzer`` and silently returned empty). This implementation performs a
direct, curated multi-platform existence sweep via the shared keyless backend —
confirming which public profiles exist for the handle, with near-zero false
positives.
"""
from __future__ import annotations

from worker_python.adapters._net import username_profiles
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class SocialAnalyzerAdapter(ToolAdapter):
    """Keyless username→public-profile existence sweep."""

    def name(self) -> str:
        return "social_analyzer"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 1  # route the sweep through Tor when available

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        return username_profiles(seed, use_tor=True)

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            units.append(
                self.make_evidence(
                    source_platform=(item.get("platform") or "unknown").lower(),
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                )
            )
        return units
