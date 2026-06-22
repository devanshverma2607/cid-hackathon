"""EmailRepAdapter — Tier 2 email reputation signal (EmailRep.io).

EmailRep.io returns risk/reputation flags for an email address: whether it is
suspicious, associated with malicious activity, leaked in breaches, listed in
spam databases, or blacklisted.  This is a *reputation* signal, not an identity
signal — it explicitly should NOT feed correlation or persona scoring (it says
nothing about account linkage).  It feeds the Insight Engine's risk scoring as
a new ``email_reputation`` result type.

Auth: ``GET https://emailrep.io/{email}`` with optional ``Key`` header.
Free tier: 250 queries/month (10/day).  When ``EMAILREP_API_KEY`` is unset the
adapter still works (keyless, lower rate limit).

Every request must include a ``User-Agent`` header.
"""
from __future__ import annotations

import os

from worker_python.adapters._net import http_get, is_safe_url
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_ENDPOINT = "https://emailrep.io/{email}"
_UA = "socmint-osint/1.0"


def _api_key() -> str:
    return os.environ.get("EMAILREP_API_KEY", "").strip()


class EmailRepAdapter(ToolAdapter):
    """Email reputation lookup — risk signal only, not identity correlation."""

    def name(self) -> str:
        return "emailrep"

    def version(self) -> str:
        return "emailrep-api"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct — low risk, optional API key

    def health_check(self) -> bool:
        # Works keyless at low volume; always healthy
        return True

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        seed = (seed or "").strip().lower()
        if not seed or "@" not in seed:
            return []

        url = _ENDPOINT.format(email=seed)
        if not is_safe_url(url):
            return []

        import httpx

        headers = {"User-Agent": _UA, "Accept": "application/json"}
        key = _api_key()
        if key:
            headers["Key"] = key

        try:
            with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    return []
                try:
                    data = resp.json()
                except ValueError:
                    return []
                if not isinstance(data, dict):
                    return []
                return [data]
        except Exception:  # noqa: BLE001
            return []

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict):
                continue
            email = data.get("email", "")
            reputation = data.get("reputation", "none")
            suspicious = data.get("suspicious", False)
            malicious = data.get("details", {}).get("malicious_activity", False)
            credentials_leaked = data.get("details", {}).get("credentials_leaked", False)
            spam = data.get("details", {}).get("spam", False)
            blacklisted = data.get("details", {}).get("blacklisted", False)

            flags = []
            if suspicious:
                flags.append("suspicious")
            if malicious:
                flags.append("malicious_activity")
            if credentials_leaked:
                flags.append("credentials_leaked")
            if spam:
                flags.append("spam")
            if blacklisted:
                flags.append("blacklisted")

            notes = f"source=emailrep reputation={reputation}"
            if flags:
                notes += " flags=" + ",".join(flags)

            # Confidence: high when flags present, moderate for neutral
            confidence = 0.8 if flags else 0.4

            units.append(
                self.make_evidence(
                    source_platform="emailrep",
                    source_tier=1,  # first-party API
                    seed_value=self._seed_value,
                    result_type="email_reputation",
                    result_value=email or self._seed_value,
                    confidence_raw=confidence,
                    platform_enrichment={
                        "reputation": reputation,
                        "suspicious": suspicious,
                        "flags": flags,
                        "references": data.get("references", 0),
                    },
                    notes=notes[:2000],
                )
            )
        return units
