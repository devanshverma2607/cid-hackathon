"""BlackbirdAdapter — Tier 1 fast username/email sweep (Section 11.3)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import time

from worker_python.adapters.base import ToolAdapter, resolve_tool_script
from api.models.evidence import EvidenceUnit


class BlackbirdAdapter(ToolAdapter):
    """Wraps Blackbird (python blackbird.py -u {username} --json).

    Blackbird writes its results to a dated JSON *file* under ``results/`` (it
    never prints JSON to stdout — stdout is a rich progress UI). It also tries to
    refresh its site list over the network at startup, which can hang inside a
    sandboxed worker; ``--no-update`` disables that. The adapter therefore runs
    blackbird from its own repo, then reads back the freshly-written JSON file.
    """

    def name(self) -> str:
        return "blackbird"

    def version(self) -> str:
        return "git"

    def get_tool_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        has_python = shutil.which("python3") is not None or shutil.which("python") is not None
        return has_python and resolve_tool_script("blackbird.py")[0] is not None

    def run(self, seed: str) -> list[dict]:
        python = shutil.which("python3") or "python"
        flag = "-e" if self._seed_type == "email" else "-u"
        script_dir, _ = resolve_tool_script("blackbird.py")
        results_dir = os.path.join(script_dir, "results") if script_dir else ""

        # Snapshot existing result files so we can identify the new one.
        before = set(glob.glob(os.path.join(results_dir, "**", "*.json"), recursive=True)) if results_dir else set()
        started = time.time() - 1

        stdout, stderr, code = self.run_subprocess(
            [
                python, "blackbird.py", flag, seed, "--json",
                "--no-update", "--no-nsfw",
                "--timeout", "10", "--max-concurrent-requests", "50",
            ],
            timeout=170,
            use_tor=False,
        )

        if not results_dir or not os.path.isdir(results_dir):
            return []

        # Prefer a result file created/modified by *this* run.
        candidates = glob.glob(os.path.join(results_dir, "**", "*.json"), recursive=True)
        fresh = [p for p in candidates if p not in before or os.path.getmtime(p) >= started]
        target_pool = fresh or candidates
        if not target_pool:
            return []
        newest = max(target_pool, key=os.path.getmtime)

        try:
            with open(newest, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            return []

        # Blackbird JSON is a flat list of {name, url, category, status, metadata}.
        if isinstance(data, dict):
            data = data.get("results", data.get("accounts", []))
        return data if isinstance(data, list) else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "FOUND")).upper()
            if status and status not in ("FOUND", "CLAIMED", "TRUE"):
                continue
            url = item.get("url") or item.get("link") or ""
            platform = (item.get("name") or item.get("site") or "unknown").lower()
            if not url or url in seen:
                continue
            seen.add(url)
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value=item.get("seed", ""),
                    result_type="account_found",
                    result_value=url,
                    notes=f"category={item.get('category')}" if item.get("category") else None,
                    confidence_raw=None,
                )
            )
        return units
