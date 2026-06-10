"""GhuntAdapter — Tier 2 Google account enrichment (Section 11.15).

Runs in an isolated venv. Weight +12 in correlation scoring. No decay.
"""
from __future__ import annotations

import json
import os
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class GhuntAdapter(ToolAdapter):
    """Wraps ghunt (ghunt email {email} --json); parses Google profile JSON."""

    def name(self) -> str:
        return "ghunt"

    def version(self) -> str:
        return "ghunt"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        if shutil.which("ghunt") is None:
            return False
        cookies = os.environ.get("GHUNT_COOKIES_PATH", "")
        return bool(cookies) and os.path.exists(cookies)

    def run(self, seed: str) -> list[dict]:
        out_path = "/tmp/ghunt_out.json"
        stdout, stderr, code = self.run_subprocess(
            ["ghunt", "email", seed, "--json", out_path], timeout=300
        )
        data = {}
        for source in (out_path,):
            try:
                with open(source, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                    break
            except (json.JSONDecodeError, FileNotFoundError):
                continue
        if not data:
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = {}
        return [data] if data else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict) or not data:
                continue
            profile = data.get("profile", data)
            gaia = profile.get("personId") or profile.get("gaiaID") or "google_account"
            units.append(
                self.make_evidence(
                    source_platform="google",
                    source_tier=1,
                    seed_value="",
                    result_type="google_hit",
                    result_value=str(gaia),
                    platform_enrichment=data,
                )
            )
        return units
