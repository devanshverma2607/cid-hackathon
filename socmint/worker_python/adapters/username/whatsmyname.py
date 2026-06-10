"""WhatsMyNameAdapter — Tier 1 username footprinting (Section 11.4).

The upstream WebBreacher/WhatsMyName project is a *data* project: it ships a
``wmn-data.json`` site catalogue but **no** ``whatsmyname.py`` runner. The old
adapter shelled out to that non-existent script, so the tool was reported
"healthy" yet always returned zero rows.

This reimplementation is fully key-less and self-contained: it loads the local
``wmn-data.json`` (732 sites) and, for each site, requests
``uri_check`` with ``{account}`` substituted and records a hit when the response
status equals the site's ``e_code`` **and** the site's ``e_string`` appears in
the body (the exact existence rule WhatsMyName defines). Requests run
concurrently with a shared timeout-bounded HTTP client and an overall wall-clock
deadline so the sweep can never hang the worker.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic

from worker_python.adapters._net import is_safe_url
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

# Candidate locations for the WhatsMyName site catalogue (baked into the image).
_DATA_CANDIDATES = (
    "/tools/python/whatsmyname/wmn-data.json",
    "/opt/tools/python/whatsmyname/wmn-data.json",
    "/tools/python/WhatsMyName/wmn-data.json",
)
_MAX_WORKERS = 40
_PER_REQUEST_TIMEOUT = 8.0
_OVERALL_DEADLINE = 150.0  # seconds — hard cap for the whole sweep


class WhatsMyNameAdapter(ToolAdapter):
    """Keyless username sweep over the WhatsMyName site catalogue."""

    def name(self) -> str:
        return "whatsmyname"

    def version(self) -> str:
        return "wmn-data"

    def get_tool_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return self._dataset_path() is not None

    # ---- internals ----------------------------------------------------------
    @staticmethod
    def _dataset_path() -> str | None:
        env_path = os.environ.get("WMN_DATA_PATH", "").strip()
        for path in (env_path, *_DATA_CANDIDATES):
            if path and os.path.isfile(path):
                return path
        return None

    def _load_sites(self) -> list[dict]:
        path = self._dataset_path()
        if not path:
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        sites = data.get("sites", []) if isinstance(data, dict) else data
        return [s for s in sites if isinstance(s, dict) and s.get("uri_check")]

    @staticmethod
    def _check_site(client, site: dict, seed: str) -> dict | None:
        """Return a hit dict when the account exists on this site, else None."""
        uri_check = site.get("uri_check") or ""
        url = uri_check.replace("{account}", seed)
        if not is_safe_url(url):
            return None
        try:
            e_code = int(site.get("e_code"))
        except (TypeError, ValueError):
            e_code = 200
        e_string = site.get("e_string") or ""
        try:
            resp = client.get(url, timeout=_PER_REQUEST_TIMEOUT)
        except Exception:  # noqa: BLE001 — network error / timeout → no hit
            return None
        if resp.status_code != e_code:
            return None
        if e_string and e_string not in resp.text:
            return None
        return {
            "name": site.get("name") or "unknown",
            "url": url,
            "uri_pretty": (site.get("uri_pretty") or uri_check).replace("{account}", seed),
            "category": site.get("cat") or "unknown",
            "account": seed,
        }

    # ---- ToolAdapter API ----------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        import httpx

        seed = (seed or "").strip()
        if not seed:
            return []
        sites = self._load_sites()
        if not sites:
            raise RuntimeError("wmn-data.json catalogue not found or empty")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
        hits: list[dict] = []
        deadline = monotonic() + _OVERALL_DEADLINE
        with httpx.Client(
            follow_redirects=True, headers=headers, timeout=_PER_REQUEST_TIMEOUT
        ) as client:
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
                futures = {
                    pool.submit(self._check_site, client, site, seed): site
                    for site in sites
                }
                for future in as_completed(futures):
                    if monotonic() >= deadline:
                        break
                    try:
                        result = future.result()
                    except Exception:  # noqa: BLE001
                        result = None
                    if result:
                        hits.append(result)
        return hits

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = item.get("uri_pretty") or item.get("url") or ""
            platform = (item.get("name") or "unknown").lower()
            if not url:
                continue
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=2,
                    seed_value=item.get("account", ""),
                    result_type="account_found",
                    result_value=url,
                    notes=f"category={item.get('category', 'unknown')}",
                )
            )
        return units
