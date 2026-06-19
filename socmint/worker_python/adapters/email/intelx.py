"""IntelXAdapter — Tier 2 leak / paste / darkweb reference search (Intelligence X).

Intelligence X (https://intelx.io) indexes leaks, pastes, whois, and darkweb
data. Its API takes a *selector* (email, username, domain, phone, IP, bitcoin
address …) and returns documents that reference it — directly serving the core
feature's requirement to "analyse breach/leaked data references to identify
exposed usernames and email-username associations".

The API is two-phase: POST a search to get an id, then GET the result endpoint
(polling while the backend is still gathering). This adapter is **gated on
``INTELX_API_KEY``** — when the key is absent it reports unhealthy and degrades
to an ``unavailable`` marker, so it is invisible unless explicitly configured.
The free key works against ``2.intelx.io`` or ``free.intelx.io``; both are tried
(override with ``INTELX_BASE_URL``). Credentials are read from the environment
and never logged.
"""
from __future__ import annotations

import os
import time

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_UA = "socmint-osint/1.0"
_MAX_RESULTS = 10
_MAX_POLLS = 4
_POLL_SLEEP = 1.5


def _api_key() -> str:
    return os.environ.get("INTELX_API_KEY", "").strip()


def _base_urls() -> tuple[str, ...]:
    override = os.environ.get("INTELX_BASE_URL", "").strip().rstrip("/")
    if override:
        return (override,)
    return ("https://2.intelx.io", "https://free.intelx.io")


class IntelXAdapter(ToolAdapter):
    """Keyed leak/paste/darkweb reference search for any identifier seed."""

    def name(self) -> str:
        return "intelx"

    def version(self) -> str:
        return "intelx-api"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct egress — the request is authenticated by API key

    def health_check(self) -> bool:
        return bool(_api_key())

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        seed = (seed or "").strip()
        key = _api_key()
        if not seed or not key:
            return []

        headers = {
            "x-key": key,
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "term": seed,
            "buckets": [],
            "lookuplevel": 0,
            "maxresults": _MAX_RESULTS,
            "timeout": 0,
            "datefrom": "",
            "dateto": "",
            "sort": 4,        # most relevant first
            "media": 0,
            "terminate": [],
        }
        for base in _base_urls():
            records = self._search(base, headers, body)
            if records is not None:
                return records
        return []

    def _search(self, base: str, headers: dict, body: dict) -> list[dict] | None:
        """Run one POST-then-poll search cycle against a base URL.

        Returns the records list on success, or ``None`` when this base URL was
        unauthorised/unreachable so the caller can try the next candidate.
        """
        import httpx

        try:
            with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
                resp = client.post(f"{base}/intelligent/search", json=body)
                if resp.status_code in (401, 402, 403):
                    return None  # wrong base / unauthorised — try the next one
                if resp.status_code != 200:
                    return None
                try:
                    search_id = (resp.json() or {}).get("id")
                except ValueError:
                    return None
                if not search_id:
                    return []

                records: dict[str, dict] = {}
                for _ in range(_MAX_POLLS):
                    rr = client.get(
                        f"{base}/intelligent/search/result",
                        params={
                            "id": search_id,
                            "limit": _MAX_RESULTS,
                            "statistics": 0,
                            "previewlines": 0,
                        },
                    )
                    if rr.status_code != 200:
                        break
                    try:
                        data = rr.json() or {}
                    except ValueError:
                        break
                    for rec in data.get("records") or []:
                        if isinstance(rec, dict):
                            records[str(rec.get("systemid") or len(records))] = rec
                    status = data.get("status")
                    # 0 = results in this batch, 1 = no more, 2 = id not found,
                    # 3 = nothing yet (keep polling).
                    if status in (1, 2) or len(records) >= _MAX_RESULTS:
                        break
                    time.sleep(_POLL_SLEEP)

                # Best-effort terminate so we free the server-side search slot.
                try:
                    client.get(
                        f"{base}/intelligent/search/terminate",
                        params={"id": search_id},
                    )
                except Exception:  # noqa: BLE001
                    pass
                return list(records.values())
        except Exception:  # noqa: BLE001 — network/transport failure → try next base
            return None

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for rec in raw:
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("name") or "").strip()
            bucket = str(rec.get("bucket") or "").strip()
            value = name[:200] if name else (f"intelx:{bucket}" if bucket else "intelx-record")
            units.append(
                self.make_evidence(
                    source_platform="breach",
                    source_tier=3,
                    seed_value=self._seed_value,
                    result_type="breach_hit",
                    result_value=value,
                    notes=self._format_notes(rec, bucket),
                )
            )
        return units

    @staticmethod
    def _format_notes(rec: dict, bucket: str) -> str:
        parts = ["source=intelx"]
        if bucket:
            parts.append(f"bucket={bucket}")
        if rec.get("date"):
            parts.append(f"date={rec['date']}")
        if rec.get("type") is not None:
            parts.append(f"type={rec['type']}")
        return " ".join(parts)[:2000]
