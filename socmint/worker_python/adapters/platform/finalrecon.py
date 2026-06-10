"""FinalReconAdapter — Tier 4 website recon via keyless DNS / TLS / crt.sh."""
from __future__ import annotations

from worker_python.adapters._net import (
    clean_domain,
    crtsh_subdomains,
    dns_a_records,
    http_get,
    ssl_cert_info,
)
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_SECURITY_HEADERS = (
    "strict-transport-security",
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
)


class FinalReconAdapter(ToolAdapter):
    """Infra recon for {domain}: DNS, TLS cert, security headers, CT subdomains."""

    def name(self) -> str:
        return "finalrecon"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        return 1  # Tor

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        domain = clean_domain(seed)
        if not domain:
            return []
        resp = http_get(f"https://{domain}", use_tor=True) or http_get(
            f"http://{domain}", use_tor=False
        )
        security: dict = {}
        server = ""
        if resp is not None:
            server = resp.headers.get("server", "")
            security = {h: resp.headers.get(h, "") for h in _SECURITY_HEADERS}
        subdomains = crtsh_subdomains(domain)
        summary = {
            "kind": "summary",
            "target": domain,
            "a_records": dns_a_records(domain),
            "server": server,
            "ssl": ssl_cert_info(domain),
            "security_headers": security,
            "subdomain_count": len(subdomains),
        }
        out: list[dict] = [summary]
        for sub in subdomains[:60]:
            if sub != domain:
                out.append({"kind": "subdomain", "value": sub})
        return out

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
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
                        notes="crt.sh subdomain",
                    )
                )
            else:
                units.append(
                    self.make_evidence(
                        source_platform="domain",
                        source_tier=2,
                        seed_value="",
                        result_type="domain_hit",
                        result_value=str(data.get("target", "domain")),
                        platform_enrichment=data,
                    )
                )
        return units
