"""GeogramintAdapter — Tier 4 Telegram phone/account correlation (Section 11.29)."""
from __future__ import annotations

import json
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class GeogramintAdapter(ToolAdapter):
    """Wraps geogramint (python geogramint.py -p {phone_or_username})."""

    def name(self) -> str:
        return "geogramint"

    def version(self) -> str:
        return "git"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        # Honest check: this git tool is not provisioned into the image (it is a
        # PyQt desktop GUI with no CLI). Report unavailable rather than claiming
        # health just because a python interpreter exists. Superseded by
        # TelegramIntelAdapter (Telethon).
        from worker_python.adapters.base import tool_script_available
        return tool_script_available("geogramint.py")

    def run(self, seed: str) -> list[dict]:
        python = shutil.which("python3") or "python"
        stdout, stderr, code = self.run_subprocess([python, "geogramint.py", "-p", seed], timeout=180)
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
