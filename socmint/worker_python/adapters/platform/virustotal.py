"""VirusTotalAdapter — Tier 4 domain intelligence (VirusTotal API v3).

Enriches a discovered domain with VirusTotal's passive intelligence: resolved
DNS records, site categories, reputation, the analysis verdict spread, and
registrar/creation data. Fires from the Tier-4 ``domain`` trigger matrix on
domains discovered during a case. Gated on ``VIRUSTOTAL_API_KEY`` — absent key ⇒
unhealthy ⇒ graceful ``unavailable`` marker. The key is sent in the ``x-apikey``
header and never logged.
"""
from __future__ import annotations

import os

from worker_python.adapters._net import clean_domain
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_API = "https://www.virustotal.com/api/v3/domains/{domain}"
_MAX_DNS = 25


def _api_key() -> str:
    return os.environ.get("VIRUSTOTAL_API_KEY", "").strip()


class VirusTotalAdapter(ToolAdapter):
    """Keyed passive domain intelligence (DNS, categories, reputation)."""

    def name(self) -> str:
        return "virustotal"

    def version(self) -> str:
        return "api-v3"

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

        import httpx

        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                resp = client.get(
                    _API.format(domain=domain),
                    headers={"x-apikey": key, "User-Agent": "socmint-osint/1.0"},
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception:  # noqa: BLE001 — network/parse failure degrades gracefully
            return []

        attrs = (((data or {}).get("data") or {}).get("attributes")) or {}
        if not isinstance(attrs, dict):
            return []

        dns = []
        for rec in (attrs.get("last_dns_records") or [])[:_MAX_DNS]:
            if isinstance(rec, dict) and rec.get("type") and rec.get("value"):
                dns.append(f"{rec['type']}:{rec['value']}")

        categories = attrs.get("categories") or {}
        cat_values = sorted({str(v) for v in categories.values()}) if isinstance(categories, dict) else []

        summary = {
            "kind": "summary",
            "target": domain,
            "reputation": attrs.get("reputation"),
            "analysis_stats": attrs.get("last_analysis_stats") or {},
            "categories": cat_values[:10],
            "registrar": attrs.get("registrar"),
            "creation_date": attrs.get("creation_date"),
            "dns_records": dns,
            "tags": attrs.get("tags") or [],
        }
        return [summary]

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict):
                continue
            target = str(data.get("target") or "domain")
            units.append(
                self.make_evidence(
                    source_platform="domain",
                    source_tier=2,
                    seed_value="",
                    result_type="domain_hit",
                    result_value=target,
                    platform_enrichment=data,
                    notes=self._format_notes(data),
                )
            )
        return units

    @staticmethod
    def _format_notes(data: dict) -> str:
        parts = ["source=virustotal"]
        stats = data.get("analysis_stats") or {}
        if stats:
            mal = stats.get("malicious", 0)
            susp = stats.get("suspicious", 0)
            total = sum(v for v in stats.values() if isinstance(v, int))
            parts.append(f"malicious={mal} suspicious={susp} of {total}")
        if data.get("reputation") is not None:
            parts.append(f"reputation={data['reputation']}")
        if data.get("registrar"):
            parts.append(f"registrar={data['registrar']}")
        if data.get("categories"):
            parts.append("categories=" + ",".join(data["categories"][:5]))
        return " ".join(parts)[:2000]
