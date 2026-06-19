"""HudsonRockAdapter — Tier 2 infostealer-infection lookup (keyless Cavalier API).

Hudson Rock's Cavalier "osint-tools" endpoints (https://cavalier.hudsonrock.com)
are free and key-less. They report whether an email, username, or domain appears
in Hudson Rock's database of machines compromised by info-stealer malware. Each
hit reveals high-value linkage intelligence that ordinary breach feeds do not:

* the *number of user/corporate services* whose credentials were stolen from the
  same machine (``linked platform references``), and
* a partial, API-masked sample of the logins/emails saved on that machine
  (``email-username associations`` for the same individual).

The API masks passwords and most of each login itself, so this adapter never
handles plaintext credentials. Queried directly (Cloudflare-fronted) and
degrades gracefully to an ``unavailable`` marker on any failure.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import clean_domain, http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{1,38})$")

_BASE = "https://cavalier.hudsonrock.com/api/json/v2/osint-tools"
_BY_EMAIL = _BASE + "/search-by-email?email={q}"
_BY_USERNAME = _BASE + "/search-by-username?username={q}"
_BY_DOMAIN = _BASE + "/search-by-domain?domain={q}"

_MAX_STEALERS = 25
_MAX_SAMPLE_LOGINS = 5


class HudsonRockAdapter(ToolAdapter):
    """Keyless info-stealer infection lookup for email / username / domain seeds."""

    def name(self) -> str:
        return "hudsonrock"

    def version(self) -> str:
        return "cavalier-keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct egress — Cloudflare challenges Tor exit nodes

    def health_check(self) -> bool:
        return True

    # ---- collection ---------------------------------------------------------
    def _build_url(self, seed: str) -> tuple[str, str] | None:
        """Return (url, seed_kind) for a valid seed, else None."""
        s = (seed or "").strip()
        if _EMAIL_RE.match(s.lower()):
            return _BY_EMAIL.format(q=s.lower()), "email"
        if "@" not in s and clean_domain(s):
            return _BY_DOMAIN.format(q=clean_domain(s)), "domain"
        if _USERNAME_RE.match(s):
            return _BY_USERNAME.format(q=s), "username"
        return None

    def run(self, seed: str) -> list[dict]:
        built = self._build_url(seed)
        if built is None:
            return []
        url, seed_kind = built
        resp = http_get(url, use_tor=False, timeout=25)
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []

        stealers = data.get("stealers")
        out: list[dict] = []
        if isinstance(stealers, list) and stealers:
            for stealer in stealers[:_MAX_STEALERS]:
                if not isinstance(stealer, dict):
                    continue
                logins = stealer.get("top_logins") or []
                sample = [str(x) for x in logins if str(x).strip()][:_MAX_SAMPLE_LOGINS]
                out.append(
                    {
                        "kind": "stealer",
                        "seed": seed,
                        "seed_kind": seed_kind,
                        "date": str(stealer.get("date_compromised") or "").strip(),
                        "os": str(stealer.get("operating_system") or "").strip(),
                        "user_services": stealer.get("total_user_services"),
                        "corp_services": stealer.get("total_corporate_services"),
                        "sample_logins": sample,
                    }
                )
        return out

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            date = item.get("date") or "unknown-date"
            units.append(
                self.make_evidence(
                    source_platform="breach",
                    source_tier=3,
                    seed_value=item.get("seed", ""),
                    result_type="breach_hit",
                    result_value=f"infostealer:{date}",
                    notes=self._format_notes(item),
                )
            )
        return units

    @staticmethod
    def _format_notes(item: dict) -> str:
        parts = ["source=hudsonrock-infostealer"]
        if item.get("date"):
            parts.append(f"compromised={item['date']}")
        if item.get("os"):
            parts.append(f"os={item['os']}")
        if item.get("user_services") is not None:
            parts.append(f"user_services={item['user_services']}")
        if item.get("corp_services") is not None:
            parts.append(f"corp_services={item['corp_services']}")
        sample = item.get("sample_logins") or []
        if sample:
            # The API already masks these logins; record them as linked-identifier
            # leads (email-username associations from the same infected machine).
            parts.append("linked_logins=" + ", ".join(sample))
        return " ".join(parts)[:2000]
