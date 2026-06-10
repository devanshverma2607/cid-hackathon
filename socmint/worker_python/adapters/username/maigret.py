"""MaigretAdapter — Tier 2 username deep sweep (Section 11.2)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class MaigretAdapter(ToolAdapter):
    """Wraps Maigret (maigret {username} --json --folderoutput {tmpdir})."""

    def name(self) -> str:
        return "maigret"

    def version(self) -> str:
        return "maigret"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        return shutil.which("maigret") is not None

    def run(self, seed: str) -> list[dict]:
        tmpdir = tempfile.mkdtemp(prefix="maigret_")
        stdout, stderr, code = self.run_subprocess(
            ["maigret", seed, "--json", "simple", "--folderoutput", tmpdir], timeout=600
        )
        results: list[dict] = []
        for path in glob.glob(os.path.join(tmpdir, "*.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, OSError):
                continue
            for site, info in data.items():
                status = info.get("status", {}) if isinstance(info, dict) else {}
                if isinstance(status, dict) and status.get("status") == "Claimed":
                    results.append({"site": site, "url": info.get("url_user", "")})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            units.append(
                self.make_evidence(
                    source_platform=item.get("site", "unknown").lower(),
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                )
            )
        return units
