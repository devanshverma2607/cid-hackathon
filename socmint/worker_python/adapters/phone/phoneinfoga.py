"""PhoneInfogaAdapter — Tier 1 phone reconnaissance (sundowndev/phoneinfoga).

PhoneInfoga is the de-facto phone-number OSINT framework. We invoke its keyless
``scan`` (the local libphonenumber scanner runs without any API key) and capture
two things the offline ``phone_enrich`` adapter does not:

* an independent country / carrier / line-type read for cross-validation, and
* any OSINT *footprint* URLs the tool generates (search-engine / reputation
  dorks), which we emit as ``dork_hit`` evidence so they feed the correlation
  engine's dork signal and the pivot brain's domain expansion.

PhoneInfoga is currently stable-but-unmaintained upstream; if the binary is not
provisioned the adapter simply degrades to an 'unavailable' unit like any other.
"""
from __future__ import annotations

import os
import re
import shutil

import phonenumbers

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IN").upper()

_URL_RE = re.compile(r"https?://[^\s'\"<>)]+")


class PhoneInfogaAdapter(ToolAdapter):
    """Wraps ``phoneinfoga scan -n <number>``; parses the local-scanner output."""

    def name(self) -> str:
        return "phoneinfoga"

    def version(self) -> str:
        return "phoneinfoga/v2"

    def get_tool_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return shutil.which("phoneinfoga") is not None

    @staticmethod
    def _to_e164(seed: str) -> str:
        """Normalise a free-form seed to E.164 (PhoneInfoga wants +<cc><number>)."""
        candidate = (seed or "").strip()
        for region in (None, DEFAULT_PHONE_REGION):
            try:
                parsed = phonenumbers.parse(candidate, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_possible_number(parsed) or phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        digits = re.sub(r"\D", "", candidate)
        return ("+" + digits) if digits else ""

    def run(self, seed: str) -> list[dict]:
        number = self._to_e164(seed)
        if not number:
            return []

        stdout, _stderr, _code = self.run_subprocess(
            ["phoneinfoga", "scan", "-n", number], timeout=180
        )
        if not stdout.strip():
            return []

        country = carrier = line_type = None
        footprints: list[str] = []
        for line in stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            if ":" in text:
                key, _, value = text.partition(":")
                k = key.strip().lower()
                v = value.strip()
                if v and not v.lower().startswith("http"):
                    if k.endswith("country"):
                        country = country or v
                    elif "carrier" in k:
                        carrier = carrier or v
                    elif "line type" in k or k == "line":
                        line_type = line_type or v
            for url in _URL_RE.findall(text):
                footprints.append(url.rstrip(".,);"))

        # De-duplicate footprints while preserving order.
        seen: set[str] = set()
        footprints = [u for u in footprints if not (u in seen or seen.add(u))]

        if not any((country, carrier, line_type, footprints)):
            return []

        return [{
            "number": number,
            "country": country,
            "carrier": carrier,
            "line_type": line_type,
            "footprints": footprints,
        }]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for rec in raw:
            enrichment = {
                "e164": rec.get("number"),
                "country": rec.get("country"),
                "carrier": rec.get("carrier"),
                "line_type": rec.get("line_type"),
                "footprint_count": len(rec.get("footprints") or []),
            }
            units.append(
                self.make_evidence(
                    source_platform="phoneinfoga",
                    source_tier=2,
                    result_type="phone_intel",
                    result_value=rec.get("number") or "",
                    platform_enrichment=enrichment,
                    notes="phoneinfoga scan (local scanner)",
                )
            )
            # Each generated footprint becomes a dork hit (correlation + pivot).
            for url in rec.get("footprints") or []:
                units.append(
                    self.make_evidence(
                        source_platform="phoneinfoga",
                        source_tier=2,
                        result_type="dork_hit",
                        result_value=url,
                        notes="phoneinfoga footprint",
                    )
                )
        return units
