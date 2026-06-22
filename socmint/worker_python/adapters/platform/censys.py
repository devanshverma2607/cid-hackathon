"""CensysAdapter — Tier 4 domain/host intelligence (Censys Search API v2).

Censys indexes internet hosts, TLS certificates, and service banners.  This
adapter queries the v2 hosts search endpoint for a domain to discover exposed
services and certificates — complementary to the existing Shodan adapter
(different index/coverage, not a replacement).

Auth: **HTTP Basic** with ``CENSYS_API_ID`` + ``CENSYS_API_SECRET`` (the legacy
v2 scheme, still functional).  Gated on both env vars — absent ⇒ ``unavailable``
marker.

Endpoint: ``GET https://search.censys.io/api/v2/hosts/search``
"""
from __future__ import annotations

import base64
import os

from worker_python.adapters._net import clean_domain, is_safe_url
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_SEARCH_URL = "https://search.censys.io/api/v2/hosts/search"
_MAX_RESULTS = 25


def _api_id() -> str:
    return os.environ.get("CENSYS_API_ID", "").strip()


def _api_secret() -> str:
    return os.environ.get("CENSYS_API_SECRET", "").strip()


class CensysAdapter(ToolAdapter):
    """Keyed domain/host intelligence via Censys Search API v2."""

    def name(self) -> str:
        return "censys"

    def version(self) -> str:
        return "api-v2"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        return 2  # direct — authenticated by API key

    def health_check(self) -> bool:
        return bool(_api_id() and _api_secret())

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        domain = clean_domain(seed)
        api_id = _api_id()
        api_secret = _api_secret()
        if not domain or not api_id or not api_secret:
            return []

        import httpx

        creds = base64.b64encode(f"{api_id}:{api_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
            "User-Agent": "socmint-osint/1.0",
        }

        try:
            with httpx.Client(timeout=25.0, headers=headers, follow_redirects=True) as client:
                resp = client.get(
                    _SEARCH_URL,
                    params={"q": domain, "per_page": _MAX_RESULTS},
                )
                if resp.status_code != 200:
                    return []
                try:
                    data = resp.json()
                except ValueError:
                    return []
                if not isinstance(data, dict):
                    return []
        except Exception:  # noqa: BLE001
            return []

        result = data.get("result", {})
        hits = result.get("hits") or []
        out: list[dict] = []
        for hit in hits[:_MAX_RESULTS]:
            if not isinstance(hit, dict):
                continue
            out.append({
                "ip": hit.get("ip", ""),
                "services": [
                    {
                        "port": svc.get("port"),
                        "service_name": svc.get("service_name"),
                        "transport_protocol": svc.get("transport_protocol"),
                    }
                    for svc in (hit.get("services") or [])[:10]
                    if isinstance(svc, dict)
                ],
                "location": hit.get("location", {}),
                "autonomous_system": hit.get("autonomous_system", {}),
            })

        # Prepend a summary record
        if out:
            out.insert(0, {
                "kind": "summary",
                "target": domain,
                "host_count": len(out),
                "total": result.get("total", len(out)),
            })
        return out

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            if not isinstance(data, dict):
                continue

            if data.get("kind") == "summary":
                target = data.get("target", "domain")
                notes = (
                    f"source=censys hosts={data.get('host_count', 0)} "
                    f"total={data.get('total', 0)}"
                )
                units.append(
                    self.make_evidence(
                        source_platform="domain",
                        source_tier=1,  # first-party API
                        seed_value="",
                        result_type="domain_hit",
                        result_value=target,
                        platform_enrichment=data,
                        notes=notes[:2000],
                    )
                )
            else:
                ip = data.get("ip", "")
                if not ip:
                    continue
                services = data.get("services") or []
                ports = [str(s.get("port")) for s in services if s.get("port")]
                notes = f"source=censys ip={ip}"
                if ports:
                    notes += f" ports={','.join(ports[:10])}"
                units.append(
                    self.make_evidence(
                        source_platform=ip,
                        source_tier=1,
                        seed_value="",
                        result_type="domain_hit",
                        result_value=ip,
                        platform_enrichment=data,
                        notes=notes[:2000],
                    )
                )
        return units
