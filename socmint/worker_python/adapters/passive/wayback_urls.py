"""WayBackURLsAdapter — Tier 3 historical URL discovery (Section 11.22)."""
from __future__ import annotations

import json
import re

from worker_python.adapters._net import clean_domain, http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

URL_RE = re.compile(r"https?://\S+")

# Bounded page cap. ``--known_urls`` enumerates *every* archived URL which, for a
# large domain (e.g. github.com), never completes inside the subprocess timeout.
# The CDX API with collapse+limit returns a representative, deduplicated slice
# fast and deterministically.
_MAX_URLS = 300
_CDX_API = "https://web.archive.org/cdx/search/cdx"


class WayBackURLsAdapter(ToolAdapter):
    """Queries the Wayback Machine CDX API for archived URLs under a domain.

    Uses the documented CDX endpoint (the same data source waybackpy wraps) with
    ``collapse=urlkey`` + ``limit`` so results are deduplicated and bounded,
    avoiding the unbounded ``--known_urls`` enumeration that hangs on large sites.
    """

    def name(self) -> str:
        return "waybackurls"

    def version(self) -> str:
        return "wayback-cdx"

    def get_tool_tier(self) -> int:
        return 3

    def health_check(self) -> bool:
        # Pure HTTP passive tool; no binary required. Degrades gracefully when
        # the network/CDX endpoint is unreachable (run() returns []).
        return True

    def run(self, seed: str) -> list[dict]:
        domain = clean_domain(seed)
        if not domain:
            return []
        params = (
            f"?url={domain}/*&output=json&collapse=urlkey&limit={_MAX_URLS}"
            f"&fl=original,timestamp,statuscode&filter=statuscode:200"
        )
        resp = http_get(_CDX_API + params, use_tor=False, timeout=60)
        rows: list[list[str]] = []
        if resp is not None and resp.status_code == 200:
            try:
                rows = json.loads(resp.text or "[]")
            except (json.JSONDecodeError, ValueError):
                rows = []

        results: list[dict] = []
        seen: set[str] = set()
        # First row is the CDX header (["original","timestamp","statuscode"]).
        for row in rows[1:] if rows else []:
            if not row:
                continue
            url = row[0]
            timestamp = row[1] if len(row) > 1 else ""
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            results.append({"url": url, "timestamp": timestamp})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            platform = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
            timestamp = item.get("timestamp", "")
            units.append(
                self.make_evidence(
                    source_platform=platform,
                    source_tier=3,
                    seed_value="",
                    result_type="archive_hit",
                    result_value=url,
                    notes=f"archived={timestamp}" if timestamp else None,
                )
            )
        return units
