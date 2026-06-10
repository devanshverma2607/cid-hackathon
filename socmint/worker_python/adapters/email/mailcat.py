"""MailcatAdapter — Tier 2 email→account discovery (Section 11.13).

Reimplemented key-less. The upstream ``mailcat`` CLI is not installed in the
image (the old adapter shelled out to ``mailcat``/``mailcat.py`` and silently
returned empty). This implementation performs the tool's core pivot: derive the
candidate handle from the email local-part and confirm which public profiles
exist for it via the shared keyless existence backend.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import username_profiles
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class MailcatAdapter(ToolAdapter):
    """Keyless email→handle→public-profile pivot."""

    def name(self) -> str:
        return "mailcat"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []
        local = email.split("@", 1)[0]
        handle = re.sub(r"[^a-z0-9._-]", "", local)
        if not handle:
            return []
        results = username_profiles(handle, use_tor=True)
        for row in results:
            row["email"] = email
            row["handle"] = handle
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            units.append(
                self.make_evidence(
                    source_platform=(item.get("platform") or "unknown").lower(),
                    source_tier=2,
                    seed_value=item.get("email", ""),
                    result_type="account_found",
                    result_value=url,
                    notes=f"derived_handle={item.get('handle')}",
                )
            )
        return units
