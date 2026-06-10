"""SocialScanAdapter — Tier 1 username/email availability (Section 11.18)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class SocialScanAdapter(ToolAdapter):
    """Wraps socialscan (socialscan {query} --json {file}).

    socialscan's ``--json`` flag requires a *filename* argument (it does not
    print JSON to stdout), and the written file encodes booleans as the strings
    ``"True"``/``"False"`` under a ``{query: [results]}`` mapping. A handle/email
    that is *unavailable* (``available == "False"``) with a successful probe
    (``success == "True"``) is one that is already registered.
    """

    def name(self) -> str:
        return "socialscan"

    def version(self) -> str:
        return "socialscan"

    def get_tool_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return shutil.which("socialscan") is not None

    def run(self, seed: str) -> list[dict]:
        out = tempfile.NamedTemporaryFile(prefix="ss_", suffix=".json", delete=False)
        out.close()
        try:
            self.run_subprocess(["socialscan", seed, "--json", out.name], timeout=180)
            try:
                with open(out.name, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, FileNotFoundError):
                return []
        finally:
            try:
                os.unlink(out.name)
            except OSError:
                pass

        results: list[dict] = []
        if isinstance(data, dict):
            for query, rows in data.items():
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict):
                        row.setdefault("query", query)
                        results.append(row)
        elif isinstance(data, list):
            results = [r for r in data if isinstance(r, dict)]
        return results

    @staticmethod
    def _is_true(value) -> bool:
        return str(value).strip().lower() == "true"

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            # success True + available False ⇒ the handle/email is taken.
            if not (self._is_true(item.get("success")) and not self._is_true(item.get("available"))):
                continue
            platform = (item.get("platform") or "unknown").lower()
            value = item.get("query") or platform
            url = item.get("link") or value
            result_type = "email_registered" if "@" in str(value) else "account_found"
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value=str(value),
                    result_type=result_type,
                    result_value=str(url),
                    notes=item.get("message"),
                )
            )
        return units
