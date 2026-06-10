"""Sublist3rAdapter — Tier 4 subdomain enumeration for a discovered domain.

When the pipeline surfaces a domain (e.g. an email provider or a personal
website linked to the subject), sublist3r enumerates its subdomains via public
search engines, expanding the infrastructure footprint that can be correlated
back to the individual.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class Sublist3rAdapter(ToolAdapter):
    """Wraps sublist3r (sublist3r -d {domain} -o {tmp}); one subdomain per line."""

    def name(self) -> str:
        return "sublist3r"

    def version(self) -> str:
        return "sublist3r"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return shutil.which("sublist3r") is not None

    def run(self, seed: str) -> list[dict]:
        out = tempfile.NamedTemporaryFile(prefix="sublist3r_", suffix=".txt", delete=False)
        out.close()
        subdomains: list[str] = []
        try:
            self.run_subprocess(["sublist3r", "-d", seed, "-o", out.name], timeout=300)
            try:
                with open(out.name, "r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        host = line.strip().lower()
                        if host and "." in host:
                            subdomains.append(host)
            except FileNotFoundError:
                pass
        finally:
            try:
                os.unlink(out.name)
            except OSError:
                pass
        return [{"host": host} for host in dict.fromkeys(subdomains)]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            host = item.get("host", "")
            if not host:
                continue
            units.append(
                self.make_evidence(
                    source_platform="domain",
                    source_tier=4,
                    seed_value="",
                    result_type="domain_hit",
                    result_value=host,
                    notes="subdomain (sublist3r)",
                )
            )
        return units
