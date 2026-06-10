"""TheHarvesterAdapter — Tier 4 domain email/host harvest via keyless sources.

theHarvester ships no usable console entry-point in this image, so this adapter
reproduces its key-free signal: hosts/subdomains from crt.sh certificate
transparency and e-mail addresses discovered via DuckDuckGo HTML (Tor-routed,
direct fallback). No API keys required.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import clean_domain, crtsh_subdomains, ddg_search
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class TheHarvesterAdapter(ToolAdapter):
    """Emails + hosts for {domain} from crt.sh + DuckDuckGo (no API key)."""

    def name(self) -> str:
        return "theharvester"

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
        results: list[dict] = []
        for host in crtsh_subdomains(domain)[:80]:
            results.append({"type": "host", "value": host})
        email_re = re.compile(r"[A-Za-z0-9._%+-]+@" + re.escape(domain))
        emails: set[str] = set()
        for query in (f'"@{domain}"', f'intext:"@{domain}"', f'"{domain}" (email OR contact)'):
            for hit in ddg_search(query, max_results=12, use_tor=True):
                blob = " ".join(
                    (hit.get("title", ""), hit.get("snippet", ""), hit.get("url", ""))
                )
                for match in email_re.findall(blob):
                    emails.add(match.lower())
        for email in sorted(emails):
            results.append({"type": "email", "value": email})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            value = item.get("value", "")
            if not value:
                continue
            units.append(
                self.make_evidence(
                    source_platform="domain",
                    source_tier=2,
                    seed_value="",
                    result_type="domain_hit",
                    result_value=value,
                    notes=item.get("type"),
                )
            )
        return units
