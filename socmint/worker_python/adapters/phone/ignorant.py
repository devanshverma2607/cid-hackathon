"""IgnorantAdapter — Tier 1 phone-number registration check (megadose/ignorant).

ignorant checks whether a phone number is registered on a set of platforms
(Instagram, Amazon, Snapchat) without sending an SMS or alerting the target.
It is the phone-seed counterpart of holehe (same author, same '[+]' output
convention), filling the previously-empty phone capability of the pipeline.
"""
from __future__ import annotations

import os
import re
import shutil

import phonenumbers

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

# ignorant prints one line per platform: '[+]' = registered, '[-]' = free,
# '[x]' = rate-limited/error. We only care about confirmed registrations.
FOUND_RE = re.compile(r"^\[\+\]\s+(\S+)")

# Default region for numbers entered without an international prefix. The system
# is primarily operated in India; override via the DEFAULT_PHONE_REGION env var.
DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IN").upper()


class IgnorantAdapter(ToolAdapter):
    """Wraps ignorant (ignorant {country_code} {number}); parses '[+]' lines."""

    def name(self) -> str:
        return "ignorant"

    def version(self) -> str:
        return "ignorant"

    def get_tool_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return shutil.which("ignorant") is not None

    @staticmethod
    def _split(seed: str) -> tuple[str, str]:
        """Split a phone seed into (country_code, national_number) digit strings.

        ignorant takes the country code and the national number as two separate
        positional arguments, so we normalise the free-form seed first. We try
        E.164 parsing (handles '+91…'), then assume the default region (India,
        matching the pipeline's primary jurisdiction), then fall back to a
        length-based split.
        """
        candidate = seed.strip()
        for region in (None, DEFAULT_PHONE_REGION):
            try:
                parsed = phonenumbers.parse(candidate, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_valid_number(parsed) or phonenumbers.is_possible_number(parsed):
                return str(parsed.country_code), str(parsed.national_number)
        digits = re.sub(r"\D", "", candidate)
        if len(digits) > 10:
            return digits[:-10], digits[-10:]
        return "91", digits

    def run(self, seed: str) -> list[dict]:
        country, number = self._split(seed)
        if not number:
            return []
        stdout, _stderr, _code = self.run_subprocess(
            ["ignorant", country, number, "--no-color", "--no-clear"], timeout=120
        )
        results: list[dict] = []
        for line in stdout.splitlines():
            match = FOUND_RE.match(line.strip())
            if match:
                domain = match.group(1).lower()
                # Real platform hits are domains (instagram.com, amazon.com, …);
                # ignorant also prints a non-domain '[+] phone …' summary line.
                if "." not in domain:
                    continue
                results.append({"platform": domain.split(".")[0], "domain": domain})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            platform = item.get("platform", "unknown")
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=1,
                    seed_value="",
                    result_type="account_found",
                    result_value=item.get("domain", platform),
                    notes="phone number registered on platform (ignorant)",
                )
            )
        return units
