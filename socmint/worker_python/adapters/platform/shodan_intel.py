"""ShodanIntelAdapter — Tier 4 domain/subdomain intelligence (Shodan API).

Uses Shodan's free-membership ``/dns/domain/{domain}`` endpoint to enumerate a
domain's subdomains and DNS records — extending the keyless ``finalrecon`` /
``crt.sh`` subdomain discovery with Shodan's own passive dataset. Fires from the
Tier-4 ``domain`` trigger matrix. Gated on ``SHODAN_API_KEY`` — absent key ⇒
unhealthy ⇒ graceful ``unavailable`` marker. The key is read from the
environment and passed as a request parameter, never logged.
"""
from __future__ import annotations

import os

from worker_python.adapters._net import clean_domain, http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_API = "https://api.shodan.io/dns/domain/{domain}?key={key}"
_MAX_SUBDOMAINS = 80


def _api_key() -> str:
    return os.environ.get("SHODAN_API_KEY", "").strip()


class ShodanIntelAdapter(ToolAdapter):
    """Keyed domain/subdomain + DNS enumeration via Shodan."""

    def name(self) -> str:
        return "shodan"

    def version(self) -> str:
        return "api-dns"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        return 2  # direct egress — authenticated by API key

    def health_check(self) -> bool:
        return bool(_api_key())

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        domain = clean_domain(seed)
        key = _api_key()
        if not domain or not key:
            return []

        resp = http_get(_API.format(domain=domain, key=key), use_tor=False, timeout=25)
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []

        subdomains = [s for s in (data.get("subdomains") or []) if isinstance(s, str)]
        records = data.get("data") or []
        out: list[dict] = [
            {
                "kind": "summary",
                "target": domain,
                "tags": data.get("tags") or [],
                "subdomain_count": len(subdomains),
                "record_count": len(records) if isinstance(records, list) else 0,
            }
        ]
        for sub in subdomains[:_MAX_SUBDOMAINS]:
            fqdn = f"{sub}.{domain}" if sub and sub != "@" else domain
            out.append({"kind": "subdomain", "value": fqdn})
        return out

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict):
                continue
            if data.get("kind") == "subdomain":
                sub = data.get("value", "")
                if not sub:
                    continue
                units.append(
                    self.make_evidence(
                        source_platform=sub,
                        source_tier=3,
                        seed_value="",
                        result_type="domain_hit",
                        result_value=sub,
                        notes="shodan subdomain",
                    )
                )
            else:
                target = str(data.get("target") or "domain")
                tags = data.get("tags") or []
                notes = f"source=shodan subdomains={data.get('subdomain_count', 0)}"
                if tags:
                    notes += " tags=" + ",".join(str(t) for t in tags[:5])
                units.append(
                    self.make_evidence(
                        source_platform="domain",
                        source_tier=2,
                        seed_value="",
                        result_type="domain_hit",
                        result_value=target,
                        platform_enrichment=data,
                        notes=notes[:2000],
                    )
                )
        return units
