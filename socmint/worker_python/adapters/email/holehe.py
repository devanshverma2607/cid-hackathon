"""HoleheAdapter — Tier 2 email registration check (Section 11.10)."""
from __future__ import annotations

import re
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

FOUND_RE = re.compile(r"^\[\+\]\s+(\S+)")


class HoleheAdapter(ToolAdapter):
    """Wraps holehe (holehe {email} --only-used --no-color); parses '[+]' lines."""

    def name(self) -> str:
        return "holehe"

    def version(self) -> str:
        return "holehe"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        return shutil.which("holehe") is not None

    def run(self, seed: str) -> list[dict]:
        stdout, stderr, code = self.run_subprocess(
            ["holehe", seed, "--only-used", "--no-color"], timeout=300
        )
        results: list[dict] = []
        for line in stdout.splitlines():
            match = FOUND_RE.match(line.strip())
            if match:
                results.append({"platform": match.group(1).lower()})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            platform = item.get("platform", "unknown")
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value="",
                    result_type="email_registered",
                    result_value=platform,
                )
            )
        return units
