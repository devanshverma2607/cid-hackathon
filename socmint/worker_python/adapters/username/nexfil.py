"""NexfilAdapter — Tier 2 username search (Section 11.5)."""
from __future__ import annotations

import re
import shutil

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

URL_RE = re.compile(r"https?://\S+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class NexfilAdapter(ToolAdapter):
    """Wraps nexfil (nexfil -u {username}); parses stdout for found URLs."""

    def name(self) -> str:
        return "nexfil"

    def version(self) -> str:
        return "git"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        return shutil.which("nexfil") is not None

    def run(self, seed: str) -> list[dict]:
        stdout, stderr, code = self.run_subprocess(["nexfil", "-u", seed], timeout=300)
        results: list[dict] = []
        seen: set[str] = set()
        for line in stdout.splitlines():
            stripped = _ANSI_RE.sub("", line).strip()
            # nexfil prints each found profile as a bare URL on its own line;
            # the ``[+]``/``[!]``/``[-]`` lines are status banners, not hits.
            if not stripped.startswith(("http://", "https://")):
                continue
            match = URL_RE.match(stripped)
            if not match:
                continue
            url = match.group(0)
            if url in seen:
                continue
            seen.add(url)
            results.append({"url": url})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            platform = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                )
            )
        return units
