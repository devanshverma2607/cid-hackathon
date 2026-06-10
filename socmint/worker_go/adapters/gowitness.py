"""GoWitnessAdapter — screenshot capture on every hit (Section 11.26).

Used by the Preservation Service. run() accepts an optional output_path.
"""
from __future__ import annotations

import os
from typing import Optional

from worker_python.adapters.base import ToolAdapter
from worker_go.adapters import go_binary
from api.models.evidence import EvidenceUnit


class GoWitnessAdapter(ToolAdapter):
    """Wraps gowitness (./tools/go/gowitness single --url {url} --screenshot-path {out})."""

    def name(self) -> str:
        return "gowitness"

    def version(self) -> str:
        return "go"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        path = go_binary("gowitness")
        return os.path.exists(path) and os.access(path, os.X_OK)

    def run(self, seed: str, output_path: Optional[str] = None) -> list[dict]:
        out = output_path or "/tmp/gowitness_screenshot.png"
        out_dir = os.path.dirname(out) or "."
        os.makedirs(out_dir, exist_ok=True)
        stdout, stderr, code = self.run_subprocess(
            [go_binary("gowitness"), "single", "--url", seed, "--screenshot-path", out_dir],
            timeout=120,
        )
        exists = os.path.exists(out)
        return [{"screenshot_path": out, "captured": exists, "stdout": stdout[:500]}]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        # GoWitness produces artifacts, not identity evidence; no EvidenceUnits.
        return []
