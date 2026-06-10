"""GitHoundAdapter — Tier 4 GitHub secret discovery (Section 11.39)."""
from __future__ import annotations

import os
import subprocess

from worker_python.adapters.base import ToolAdapter
from worker_go.adapters import go_binary
from api.models.evidence import EvidenceUnit


class GitHoundAdapter(ToolAdapter):
    """Wraps githound (echo {username} | ./tools/go/githound --dig --results-only)."""

    def name(self) -> str:
        return "githound"

    def version(self) -> str:
        return "go"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        path = go_binary("githound")
        return os.path.exists(path) and os.access(path, os.X_OK)

    def run(self, seed: str) -> list[dict]:
        env = os.environ.copy()
        try:
            completed = subprocess.run(
                [go_binary("githound"), "--dig", "--results-only"],
                input=seed,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                check=False,
            )
            stdout = completed.stdout or ""
        except subprocess.TimeoutExpired:
            stdout = ""
        return [{"finding": line.strip()} for line in stdout.splitlines() if line.strip()]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            finding = item.get("finding", "")
            if not finding:
                continue
            units.append(
                self.make_evidence(
                    source_platform="github",
                    source_tier=2,
                    seed_value="",
                    result_type="domain_hit",
                    result_value=finding[:500],
                    notes="github secret/sensitive string",
                )
            )
        return units
