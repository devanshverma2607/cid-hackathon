"""TeleGramSintAdapter — Tier 4 Telegram user/channel data (Section 11.30)."""
from __future__ import annotations

import json
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class TeleGramSintAdapter(ToolAdapter):
    """Wraps telegramsint (python telegramsint.py -u {username})."""

    def name(self) -> str:
        return "telegramsint"

    def version(self) -> str:
        return "git"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        # Honest check: this git tool is not provisioned into the image. Report
        # unavailable rather than claiming health just because a python
        # interpreter exists. Superseded by TelegramIntelAdapter (Telethon).
        from worker_python.adapters.base import tool_script_available
        return tool_script_available("telegramsint.py")

    def run(self, seed: str) -> list[dict]:
        python = shutil.which("python3") or "python"
        stdout, stderr, code = self.run_subprocess([python, "telegramsint.py", "-u", seed], timeout=180)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = {"raw": stdout[:2000]} if stdout.strip() else {}
        return [data] if data else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            units.append(
                self.make_evidence(
                    source_platform="telegram",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value="telegram",
                    platform_enrichment=data,
                )
            )
        return units
