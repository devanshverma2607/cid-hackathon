"""DnsDumpsterAdapter — Tier 4 subdomain/DNS recon (DNS Dumpster API).

Official JSON API: GET https://api.dnsdumpster.com/domain/{domain}
Header: X-API-Key: {key}. Free tier: 50 records/request.
Gated on DNSDUMPSTER_API_KEY.
"""
from __future__ import annotations
import os
from worker_python.adapters._net import clean_domain
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_API_URL = "https://api.dnsdumpster.com/domain/{domain}"

def _api_key() -> str:
    return os.environ.get("DNSDUMPSTER_API_KEY", "").strip()

class DnsDumpsterAdapter(ToolAdapter):
    def name(self) -> str: return "dnsdumpster"
    def version(self) -> str: return "api-v1"
    def get_tool_tier(self) -> int: return 4
    def get_proxy_tier(self) -> int: return 2
    def health_check(self) -> bool: return bool(_api_key())

    def run(self, seed: str) -> list[dict]:
        domain = clean_domain(seed)
        key = _api_key()
        if not domain or not key:
            return []
        import httpx
        headers = {"X-API-Key": key, "Accept": "application/json", "User-Agent": "socmint-osint/1.0"}
        try:
            with httpx.Client(timeout=25.0, headers=headers, follow_redirects=True) as client:
                resp = client.get(_API_URL.format(domain=domain))
                if resp.status_code != 200: return []
                try: data = resp.json()
                except ValueError: return []
                if not isinstance(data, dict): return []
        except Exception:
            return []
        out: list[dict] = []
        for rt in ("dns_records", "a_records", "mx_records", "ns_records"):
            for rec in (data.get(rt) or [])[:30]:
                if isinstance(rec, dict):
                    host = rec.get("host") or rec.get("domain") or ""
                    value = rec.get("value") or rec.get("ip") or ""
                    if host or value:
                        out.append({"kind": "dns_record", "host": host, "value": value})
        for h in (data.get("hosts") or data.get("subdomains") or [])[:50]:
            if isinstance(h, str): out.append({"kind": "subdomain", "value": h})
            elif isinstance(h, dict):
                hn = h.get("host") or h.get("domain") or ""
                if hn: out.append({"kind": "subdomain", "value": hn})
        if out:
            out.insert(0, {"kind": "summary", "target": domain, "record_count": len(out)})
        return out

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict): continue
            kind = data.get("kind", "")
            if kind == "subdomain":
                sub = data.get("value", "")
                if sub:
                    units.append(self.make_evidence(source_platform=sub, source_tier=1,
                        seed_value="", result_type="domain_hit", result_value=sub,
                        notes="dnsdumpster subdomain"))
            elif kind == "summary":
                target = data.get("target", "domain")
                units.append(self.make_evidence(source_platform="domain", source_tier=1,
                    seed_value="", result_type="domain_hit", result_value=target,
                    platform_enrichment=data,
                    notes=f"source=dnsdumpster records={data.get('record_count',0)}"[:2000]))
        return units
