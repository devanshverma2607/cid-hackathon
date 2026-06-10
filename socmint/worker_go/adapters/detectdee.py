"""DetectDeeAdapter — Tier 2 Go social OSINT (Section 11.9)."""
from __future__ import annotations

import os
import tempfile
from urllib.parse import urlparse

from worker_python.adapters.base import ToolAdapter
from worker_go.adapters import GO_TOOLS_DIR, go_binary
from api.models.evidence import EvidenceUnit


class DetectDeeAdapter(ToolAdapter):
    """Wraps DetectDee (`DetectDee detect -n {username}`).

    DetectDee is a Sherlock-style hunter. Its real CLI is
    ``DetectDee detect -n {username} -f {data.json} -o {result.txt}`` which
    writes the list of matched profile URLs (one per line) to the output file —
    it does NOT emit JSON on stdout. It needs its ``data.json`` site database;
    we use the copy baked next to the binary when present, otherwise we run
    ``DetectDee update`` once into a scratch directory to fetch it.
    """

    def name(self) -> str:
        return "detectdee"

    def version(self) -> str:
        return "go"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        path = go_binary("DetectDee")
        return os.path.exists(path) and os.access(path, os.X_OK)

    def _ensure_data_file(self, workdir: str) -> str | None:
        """Return a path to DetectDee's data.json, fetching it if needed."""
        bundled = os.path.join(GO_TOOLS_DIR, "DetectDee.data.json")
        if os.path.isfile(bundled):
            return bundled
        # Fall back to `DetectDee update`, which downloads data.json into cwd.
        self.run_subprocess(
            [go_binary("DetectDee"), "update"], timeout=120, cwd=workdir
        )
        fetched = os.path.join(workdir, "data.json")
        return fetched if os.path.isfile(fetched) else None

    def run(self, seed: str) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="detectdee_") as workdir:
            data_file = self._ensure_data_file(workdir)
            out_file = os.path.join(workdir, "result.txt")
            cmd = [go_binary("DetectDee"), "detect", "-n", seed, "-o", out_file, "-t", "10"]
            if data_file:
                cmd += ["-f", data_file]
            self.run_subprocess(cmd, timeout=300, cwd=workdir)

            try:
                with open(out_file, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                return []

        results: list[dict] = []
        seen: set[str] = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # result.txt rows are CSV: "name, site, url" (url may itself contain
            # commas, so split only on the first two separators).
            parts = [p.strip() for p in line.split(",", 2)]
            if len(parts) == 3:
                name, site, url = parts
            else:
                # Fallback: a bare URL line.
                url = line
                name = ""
                host = urlparse(url).hostname or ""
                site = host[4:] if host.startswith("www.") else host
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            results.append({"url": url, "site": site, "name": name})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("link") or ""
            if not url:
                continue
            units.append(
                self.make_evidence(
                    source_platform=(item.get("site") or item.get("name") or "unknown").lower(),
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                )
            )
        return units
