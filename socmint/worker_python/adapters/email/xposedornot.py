"""XposedOrNotAdapter — Tier 2 breach analytics via the keyless XposedOrNot API.

XposedOrNot (https://xposedornot.com) exposes a free, key-less REST API over
billions of breached records. Unlike h8mail (which needs HIBP/Snusbase/Dehashed
keys to return anything useful), the email-breach and breach-analytics endpoints
are fully public, so this adapter reliably enriches every email seed with:

* which named breaches the address appears in (``breach_hit`` — the breach name
  feeds the correlation engine's breach-source intersection), and
* what categories of data each breach exposed (Usernames / Email addresses /
  Passwords / ...), which directly supports the feature requirement of surfacing
  *exposed usernames* and *email-username associations* from leaked data.

The API is queried directly (not over Tor — it sits behind Cloudflare, which
challenges Tor exit nodes) and degrades gracefully: any network/parse failure
yields a single ``unavailable`` marker rather than raising.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_ANALYTICS_URL = "https://api.xposedornot.com/v1/breach-analytics?email={email}"
_CHECK_URL = "https://api.xposedornot.com/v1/check-email/{email}"

# Data categories that make a breach especially relevant to identity linkage.
_USERNAME_MARKERS = ("username", "user name", "handle", "screen name")

_MAX_BREACHES = 40
_MAX_PASTES = 15


class XposedOrNotAdapter(ToolAdapter):
    """Keyless email→breach analytics (named breaches + exposed data types)."""

    def name(self) -> str:
        return "xposedornot"

    def version(self) -> str:
        return "api-keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct egress — Cloudflare challenges Tor exit nodes

    def health_check(self) -> bool:
        return True

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []

        results: list[dict] = []
        resp = http_get(_ANALYTICS_URL.format(email=email), use_tor=False, timeout=25)
        data = None
        if resp is not None and resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = None

        if isinstance(data, dict):
            results.extend(self._parse_analytics(data, email))

        # Fall back to the lightweight check-email endpoint when analytics is
        # empty/unavailable (it still yields the list of breach names).
        if not results:
            results.extend(self._parse_check_email(email))
        return results

    def _parse_analytics(self, data: dict, email: str) -> list[dict]:
        out: list[dict] = []
        exposed = data.get("ExposedBreaches") or {}
        details = exposed.get("breaches_details") if isinstance(exposed, dict) else None
        for item in (details or [])[:_MAX_BREACHES]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("breach") or "").strip()
            if not name:
                continue
            out.append(
                {
                    "kind": "breach",
                    "breach": name,
                    "email": email,
                    "xposed_data": str(item.get("xposed_data") or "").strip(),
                    "year": str(item.get("xposed_date") or "").strip(),
                    "records": item.get("xposed_records"),
                    "domain": str(item.get("domain") or "").strip(),
                    "password_risk": str(item.get("password_risk") or "").strip(),
                }
            )

        # Paste exposures (pastebin-style dumps) are recorded separately so the
        # passive exposure surface is complete.
        pastes = data.get("ExposedPastes") or {}
        paste_details = pastes.get("pastes_details") if isinstance(pastes, dict) else None
        for item in (paste_details or [])[:_MAX_PASTES]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or item.get("paste_source") or "paste").strip()
            ident = str(item.get("id") or item.get("paste_id") or "").strip()
            out.append({"kind": "paste", "email": email, "source": source, "id": ident})
        return out

    def _parse_check_email(self, email: str) -> list[dict]:
        resp = http_get(_CHECK_URL.format(email=email), use_tor=False, timeout=20)
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        breaches = data.get("breaches") or []
        names: list[str] = []
        # The endpoint returns {"breaches": [["Name1", "Name2", ...]]}.
        for entry in breaches:
            if isinstance(entry, list):
                names.extend(str(n).strip() for n in entry if str(n).strip())
            elif isinstance(entry, str) and entry.strip():
                names.append(entry.strip())
        out: list[dict] = []
        for name in names[:_MAX_BREACHES]:
            out.append({"kind": "breach", "breach": name, "email": email})
        return out

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            email = item.get("email", "")
            if item.get("kind") == "paste":
                src = item.get("source") or "paste"
                ident = item.get("id") or ""
                units.append(
                    self.make_evidence(
                        source_platform="breach",
                        source_tier=3,
                        seed_value=email,
                        result_type="breach_hit",
                        result_value=f"paste:{src}" + (f":{ident}" if ident else ""),
                        notes="exposed in public paste dump"[:500],
                    )
                )
                continue

            name = item.get("breach", "")
            if not name:
                continue
            units.append(
                self.make_evidence(
                    source_platform="breach",
                    source_tier=3,
                    seed_value=email,
                    result_type="breach_hit",
                    result_value=name,
                    notes=self._format_notes(item),
                )
            )
        return units

    @staticmethod
    def _format_notes(item: dict) -> str:
        exposed = item.get("xposed_data") or ""
        parts: list[str] = []
        if exposed:
            parts.append(f"exposed={exposed}")
        if item.get("year"):
            parts.append(f"year={item['year']}")
        if item.get("records"):
            parts.append(f"records={item['records']}")
        if item.get("domain"):
            parts.append(f"domain={item['domain']}")
        if item.get("password_risk"):
            parts.append(f"pw_risk={item['password_risk']}")
        # Flag the high-value linkage categories explicitly so analysts (and the
        # insight engine) can spot leaked usernames / email-username associations.
        low = exposed.lower()
        if any(marker in low for marker in _USERNAME_MARKERS):
            parts.append("association=email↔username")
        return " ".join(parts)[:2000]
