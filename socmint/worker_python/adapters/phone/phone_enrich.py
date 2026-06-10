"""PhoneEnrichAdapter — Tier 1 offline phone-number intelligence.

Pure-Python enrichment built entirely on Google's libphonenumber data bundled
with the ``phonenumbers`` PyPI package (already a worker dependency). It needs
NO network and NO API key, so it is the always-available anchor for the phone
capability: every phone seed yields a deterministic record of validity, the
original-allocation carrier (e.g. Airtel / Jio / Vi / BSNL — the strongest
India-specific anchor), line type, country, timezone and region. For the precise
Indian telecom *circle / state* it can optionally consult a vendored InMobPrefix
prefix->circle dataset (path via the PHONE_CIRCLE_DATA env var); without that
dataset the circle falls back to libphonenumber's geographic description, which
is country-level for Indian mobiles (so we never pass off "India" as a circle).

The record is emitted as a single ``phone_intel`` EvidenceUnit whose
``platform_enrichment`` carries the structured fields for the case file, the
identity graph and the report. It deliberately does not re-expose the seed
number under a pivotable key, so it never re-seeds the number it just described.
"""
from __future__ import annotations

import csv
import functools
import os
import re

import phonenumbers
from phonenumbers import PhoneNumberType, carrier, geocoder, timezone

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

# Default region for numbers entered without an international prefix. The system
# is primarily operated in India; override via the DEFAULT_PHONE_REGION env var.
DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IN").upper()

# Optional InMobPrefix-style dataset mapping an Indian mobile prefix to its
# telecom circle/state (+ allocated operator). CSV columns: prefix,circle,operator.
# Absent by default — the adapter simply omits the precise circle when missing.
INDIA_CIRCLE_DATA = os.environ.get(
    "PHONE_CIRCLE_DATA",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "in_mobile_circles.csv"),
)


@functools.lru_cache(maxsize=1)
def _india_circles() -> tuple[tuple[str, str, str], ...]:
    """Load (prefix, circle, operator) rows from the optional dataset, longest first."""
    path = os.path.abspath(INDIA_CIRCLE_DATA)
    if not os.path.isfile(path):
        return ()
    rows: list[tuple[str, str, str]] = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                prefix = (r.get("prefix") or "").strip()
                circle = (r.get("circle") or "").strip()
                operator = (r.get("operator") or "").strip()
                if prefix and circle:
                    rows.append((prefix, circle, operator))
    except Exception:  # noqa: BLE001 — a bad dataset must not break enrichment
        return ()
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    return tuple(rows)


def _lookup_india_circle(national_number: str) -> tuple[str | None, str | None]:
    for prefix, circle, operator in _india_circles():
        if national_number.startswith(prefix):
            return circle, (operator or None)
    return None, None

# Human-readable line type for each libphonenumber PhoneNumberType value.
_LINE_TYPE = {
    PhoneNumberType.FIXED_LINE: "fixed_line",
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.PREMIUM_RATE: "premium_rate",
    PhoneNumberType.SHARED_COST: "shared_cost",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.PERSONAL_NUMBER: "personal_number",
    PhoneNumberType.PAGER: "pager",
    PhoneNumberType.UAN: "uan",
    PhoneNumberType.VOICEMAIL: "voicemail",
    PhoneNumberType.UNKNOWN: "unknown",
}


class PhoneEnrichAdapter(ToolAdapter):
    """Offline phone enrichment (carrier / circle / line type) via phonenumbers."""

    def name(self) -> str:
        return "phone_enrich"

    def version(self) -> str:
        return f"libphonenumber/{getattr(phonenumbers, '__version__', 'bundled')}"

    def get_tool_tier(self) -> int:
        return 1

    def get_proxy_tier(self) -> int:
        # Fully offline — no egress at all.
        return 2

    def health_check(self) -> bool:
        # The data ships inside the phonenumbers wheel, so a successful import is
        # the only requirement; this adapter is effectively always available.
        return phonenumbers is not None

    @staticmethod
    def _parse(seed: str):
        """Parse a free-form seed into a libphonenumber PhoneNumber object."""
        candidate = (seed or "").strip()
        if not candidate:
            return None
        for region in (None, DEFAULT_PHONE_REGION):
            try:
                parsed = phonenumbers.parse(candidate, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_possible_number(parsed) or phonenumbers.is_valid_number(parsed):
                return parsed
        # Last resort: keep digits only and assume the default region.
        digits = re.sub(r"\D", "", candidate)
        if not digits:
            return None
        try:
            return phonenumbers.parse("+" + digits if candidate.strip().startswith("+") else digits,
                                      DEFAULT_PHONE_REGION)
        except phonenumbers.NumberParseException:
            return None

    def run(self, seed: str) -> list[dict]:
        parsed = self._parse(seed)
        if parsed is None:
            return []

        region_code = phonenumbers.region_code_for_number(parsed) or ""
        line_type = _LINE_TYPE.get(phonenumbers.number_type(parsed), "unknown")
        record = {
            "e164": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
            "international": phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
            ),
            "country_code": parsed.country_code,
            "national_number": parsed.national_number,
            "region": region_code,
            "is_valid": phonenumbers.is_valid_number(parsed),
            "line_type": line_type,
            # Original-allocation carrier (pre-portability), e.g. "Airtel".
            "carrier": carrier.name_for_number(parsed, "en") or None,
            "timezones": list(timezone.time_zones_for_number(parsed)) or None,
        }
        # Geographic description from libphonenumber (country-level for Indian
        # mobiles). Kept separately so it is never mistaken for a telecom circle.
        geo = geocoder.description_for_number(parsed, "en") or None
        record["geo_description"] = geo

        # Precise Indian circle/state only when the optional dataset is vendored;
        # else fall back to the geocoder text unless it is just the country name.
        circle = circle_operator = None
        if region_code == "IN":
            circle, circle_operator = _lookup_india_circle(str(parsed.national_number))
        if not circle and geo and geo.strip().lower() not in ("india", ""):
            circle = geo
        record["circle"] = circle
        if circle_operator:
            record["circle_operator"] = circle_operator
        return [record]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for rec in raw:
            if not rec.get("is_valid") and not rec.get("e164"):
                continue
            summary_bits = [
                rec.get("carrier") or "carrier unknown",
                rec.get("line_type") or "line unknown",
            ]
            if rec.get("circle"):
                summary_bits.append(rec["circle"])
            units.append(
                self.make_evidence(
                    source_platform="phone_intel",
                    source_tier=1,
                    result_type="phone_intel",
                    result_value=rec.get("e164") or "",
                    platform_enrichment=rec,
                    notes="offline phone enrichment (libphonenumber): "
                    + ", ".join(summary_bits),
                )
            )
        return units
