"""AbstractPhoneAdapter — Tier 1 live phone validation (AbstractAPI).

Complements the offline ``phone_enrich`` adapter with a live carrier/line-type
lookup from AbstractAPI's phone-validation service
(https://www.abstractapi.com/api/phone-validation-api). Gated on
``ABSTRACTAPI_PHONE_KEY`` — absent key ⇒ unhealthy ⇒ graceful ``unavailable``
marker, so it is invisible unless configured. The key is read from the
environment and passed as a request parameter, never logged.
"""
from __future__ import annotations

import os

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "IN").upper()
_ENDPOINT = "https://phonevalidation.abstractapi.com/v1/"


def _api_key() -> str:
    return os.environ.get("ABSTRACTAPI_PHONE_KEY", "").strip()


class AbstractPhoneAdapter(ToolAdapter):
    """Keyed live phone validation: carrier, line type, country, region."""

    def name(self) -> str:
        return "abstractapi_phone"

    def version(self) -> str:
        return "abstract-v1"

    def get_tool_tier(self) -> int:
        return 1

    def get_proxy_tier(self) -> int:
        return 2  # direct egress — authenticated by API key

    def health_check(self) -> bool:
        return bool(_api_key())

    @staticmethod
    def _to_e164(seed: str) -> str:
        """Best-effort E.164 normalisation via libphonenumber; raw on failure."""
        seed = (seed or "").strip()
        try:
            import phonenumbers

            parsed = phonenumbers.parse(seed, DEFAULT_PHONE_REGION)
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
        except Exception:  # noqa: BLE001 — fall back to the raw seed
            return seed

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        key = _api_key()
        number = self._to_e164(seed)
        if not key or not number:
            return []

        import httpx

        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                resp = client.get(
                    _ENDPOINT, params={"api_key": key, "phone": number}
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception:  # noqa: BLE001 — network/parse failure degrades gracefully
            return []
        if not isinstance(data, dict) or not data.get("valid"):
            return []

        country = data.get("country") or {}
        fmt = data.get("format") or {}
        record = {
            "e164": (fmt.get("international") or number).replace(" ", ""),
            "valid": bool(data.get("valid")),
            "line_type": data.get("type"),
            "carrier": data.get("carrier"),
            "country": country.get("name") if isinstance(country, dict) else None,
            "country_code": country.get("code") if isinstance(country, dict) else None,
            "location": data.get("location"),
        }
        return [record]

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for rec in raw:
            if not isinstance(rec, dict) or not rec.get("e164"):
                continue
            summary_bits = [
                rec.get("carrier") or "carrier unknown",
                rec.get("line_type") or "line unknown",
            ]
            if rec.get("country"):
                summary_bits.append(rec["country"])
            units.append(
                self.make_evidence(
                    source_platform="phone_intel",
                    source_tier=1,
                    result_type="phone_intel",
                    result_value=rec.get("e164") or "",
                    platform_enrichment=rec,
                    notes="live phone validation (abstractapi): "
                    + ", ".join(str(b) for b in summary_bits),
                )
            )
        return units
