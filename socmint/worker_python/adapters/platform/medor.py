"""MedorAdapter — Tier 4 Instagram + email recon (Section 3.5 / MODULE 2).

The upstream ``medor`` tool probes Instagram's account-recovery flow, which is
heavily bot-protected and unusable unattended from a datacenter IP. This
key-less reimplementation instead surfaces *publicly indexed* links tying the
email to an Instagram (or other social) presence via the shared keyless web
search backend, and — when the email local-part maps to a handle — checks the
public Instagram web profile for existence. The old adapter shelled out to a
``medor.py`` script that was never cloned, so it always returned empty.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import ddg_search, http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_IG_PROFILE = "https://www.instagram.com/{user}/"


class MedorAdapter(ToolAdapter):
    """Keyless email→Instagram/social presence enricher."""

    def name(self) -> str:
        return "medor"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []
        results: list[dict] = []

        # 1. Publicly indexed links that mention the email on Instagram.
        for hit in ddg_search(f'"{email}" site:instagram.com', max_results=8):
            url = hit.get("url", "")
            if "instagram.com" in url:
                results.append(
                    {"type": "indexed_link", "url": url, "title": hit.get("title", "")}
                )

        # 2. Candidate handle from the email local-part → public IG profile probe.
        local = email.split("@", 1)[0]
        handle = re.sub(r"[^a-z0-9._]", "", local)
        if handle:
            resp = http_get(_IG_PROFILE.format(user=handle), timeout=12)
            if resp is not None and resp.status_code == 200 and (
                f'"username":"{handle}"' in resp.text
                or f'@{handle}' in resp.text
            ):
                results.append(
                    {
                        "type": "candidate_profile",
                        "url": _IG_PROFILE.format(user=handle),
                        "handle": handle,
                    }
                )
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            units.append(
                self.make_evidence(
                    source_platform="instagram",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=data.get("url", "instagram"),
                    platform_enrichment=data,
                    notes=data.get("type", ""),
                )
            )
        return units
