"""DnstwistAdapter — Tier 4 look-alike / typosquat domain detection (dnstwist).

For a discovered domain, dnstwist generates permutations (homoglyph, typo,
TLD-swap, etc.) and reports those that are actually registered. Registered
look-alikes are useful leads for impersonation, phishing infrastructure, or
alternate domains operated by the same subject.
"""
from __future__ import annotations

import json
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class DnstwistAdapter(ToolAdapter):
    """Wraps dnstwist (dnstwist -r -f json {domain}); parses registered permutations."""

    def name(self) -> str:
        return "dnstwist"

    def version(self) -> str:
        return "dnstwist"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return shutil.which("dnstwist") is not None

    def run(self, seed: str) -> list[dict]:
        stdout, _stderr, _code = self.run_subprocess(
            ["dnstwist", "-r", "-f", "json", seed], timeout=300
        )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            domain = item.get("domain") or item.get("domain-name") or ""
            fuzzer = item.get("fuzzer", "")
            if not domain or fuzzer in ("*original", "original"):
                continue
            units.append(
                self.make_evidence(
                    source_platform="domain",
                    source_tier=4,
                    seed_value="",
                    result_type="domain_hit",
                    result_value=domain,
                    notes=f"look-alike domain (dnstwist, fuzzer={fuzzer})" if fuzzer else "look-alike domain (dnstwist)",
                )
            )
        return units
