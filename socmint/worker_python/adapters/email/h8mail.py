"""H8mailAdapter — Tier 2 breach correlation (Section 11.11)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class H8mailAdapter(ToolAdapter):
    """Wraps h8mail (h8mail -t {email} -sk {api_keys_file}); parses JSON output."""

    def name(self) -> str:
        return "h8mail"

    def version(self) -> str:
        return "h8mail"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        return shutil.which("h8mail") is not None

    def _write_keys_file(self) -> str:
        keys = []
        if os.environ.get("H8MAIL_API_KEY"):
            keys.append(f"h8mail_api_key={os.environ['H8MAIL_API_KEY']}")
        if os.environ.get("HIBP_API_KEY"):
            keys.append(f"hibp={os.environ['HIBP_API_KEY']}")
        tmp = tempfile.NamedTemporaryFile(prefix="h8keys_", suffix=".ini", delete=False, mode="w")
        tmp.write("\n".join(keys))
        tmp.close()
        return tmp.name

    def run(self, seed: str) -> list[dict]:
        keys_file = self._write_keys_file()
        out = tempfile.NamedTemporaryFile(prefix="h8out_", suffix=".json", delete=False)
        out.close()
        try:
            cmd = ["h8mail", "-t", seed, "-jo", out.name]
            if os.path.getsize(keys_file) > 0:
                cmd += ["-sk", keys_file]
            self.run_subprocess(cmd, timeout=300)
            try:
                with open(out.name, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, FileNotFoundError):
                data = {}
        finally:
            for path in (keys_file, out.name):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        targets = data.get("targets", []) if isinstance(data, dict) else []
        results: list[dict] = []
        for target in targets:
            for hit in target.get("data", []):
                results.append({"source": str(hit), "email": target.get("target", seed)})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            units.append(
                self.make_evidence(
                    source_platform="breach",
                    source_tier=3,
                    seed_value=item.get("email", ""),
                    result_type="breach_hit",
                    result_value=item.get("source", "breach"),
                )
            )
        return units
