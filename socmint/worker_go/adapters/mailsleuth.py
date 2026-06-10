"""MailsleuthAdapter — Tier 2 Go email presence (Section 11.14)."""
from __future__ import annotations

import json
import os

from worker_python.adapters.base import ToolAdapter
from worker_go.adapters import go_binary
from api.models.evidence import EvidenceUnit


class MailsleuthAdapter(ToolAdapter):
    """Wraps mailsleuth (./tools/go/mailsleuth -e {email} --json)."""

    def name(self) -> str:
        return "mailsleuth"

    def version(self) -> str:
        return "go"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        path = go_binary("mailsleuth")
        return os.path.exists(path) and os.access(path, os.X_OK)

    def run(self, seed: str) -> list[dict]:
        stdout, stderr, code = self.run_subprocess(
            [go_binary("mailsleuth"), "-e", seed, "--json"], timeout=300
        )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            return data.get("results", [])
        return data if isinstance(data, list) else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            platform = (item.get("service") or item.get("name") or "unknown").lower()
            if item.get("exists") is False:
                continue
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value="",
                    result_type="email_registered",
                    result_value=item.get("url") or platform,
                )
            )
        return units
