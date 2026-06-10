"""LinkedIn2UsernameAdapter — Tier 4 email/username pattern generation (Section 11.36).

Reimplemented as a pure, network-free generator: given a full name (or a
``first last`` seed), it emits the common corporate username/email permutations
(``jdoe``, ``john.doe``, ``doej``, ``j.doe`` …) that analysts pivot on. The old
adapter shelled out to a ``linkedin2username.py`` script that was never cloned,
so it always returned empty.
"""
from __future__ import annotations

import re

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class LinkedIn2UsernameAdapter(ToolAdapter):
    """Generates candidate username/email patterns from a person's name."""

    def name(self) -> str:
        return "linkedin2username"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    @staticmethod
    def _patterns(first: str, last: str) -> list[str]:
        f, l = first[0], last[0]
        seen: list[str] = []
        for cand in (
            f"{first}{last}",
            f"{first}.{last}",
            f"{f}{last}",
            f"{f}.{last}",
            f"{first}{l}",
            f"{first}.{l}",
            f"{first}_{last}",
            f"{last}{first}",
            f"{last}.{first}",
            f"{last}{f}",
            first,
            last,
        ):
            if cand and cand not in seen:
                seen.append(cand)
        return seen

    def run(self, seed: str) -> list[dict]:
        tokens = [t for t in re.split(r"[\s._\-]+", (seed or "").strip().lower()) if t.isalpha()]
        if len(tokens) < 2:
            return []
        first, last = tokens[0], tokens[-1]
        return [{"candidate": p} for p in self._patterns(first, last)]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            candidate = item.get("candidate", "")
            if not candidate:
                continue
            units.append(
                self.make_evidence(
                    source_platform="linkedin",
                    source_tier=4,
                    seed_value="",
                    result_type="account_found",
                    result_value=candidate,
                    notes="generated_pattern",
                )
            )
        return units
