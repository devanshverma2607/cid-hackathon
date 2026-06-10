"""SherlockAdapter — Tier 2 username deep sweep (Section 11.1)."""
from __future__ import annotations

import re
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class SherlockAdapter(ToolAdapter):
    """Wraps Sherlock v0.16 (sherlock {username} --print-found --no-color).

    Sherlock streams found accounts to stdout as ``[+] Site Name: URL`` lines.
    (Note: in v0.16 ``--json`` *loads* a data file, it does not write output,
    and the ``--output`` txt file is only flushed on completion, so stdout is
    the reliable source.)
    """

    _FOUND_RE = re.compile(r"^\[\+\]\s+(?P<site>.+?):\s+(?P<url>https?://\S+)\s*$")

    def name(self) -> str:
        return "sherlock"

    def version(self) -> str:
        return "sherlock-project"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        return shutil.which("sherlock") is not None

    def run(self, seed: str) -> list[dict]:
        stdout, _stderr, _code = self.run_subprocess(
            ["sherlock", seed, "--print-found", "--no-color", "--no-txt",
             "--timeout", "30"],
            timeout=600,
        )
        results: list[dict] = []
        for line in (stdout or "").splitlines():
            match = self._FOUND_RE.match(line.strip())
            if match:
                results.append({"site": match.group("site").strip(),
                                "url": match.group("url").strip()})
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
