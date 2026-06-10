"""HashtrayAdapter — Tier 1 Gravatar correlation (Section 11.16).

Reimplemented key-less against Gravatar's public profile API. The upstream
``hashtray`` script was never cloned into the image, so the old adapter (which
shelled out to ``python hashtray.py``) always returned empty. Gravatar exposes a
JSON profile for any email whose MD5 hash is registered
(``https://en.gravatar.com/{md5}.json``); the response reveals the display name,
linked verified accounts and personal URLs — a genuine identity-correlation
signal.
"""
from __future__ import annotations

import hashlib
import re

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_GRAVATAR_JSON = "https://en.gravatar.com/{hash}.json"


class HashtrayAdapter(ToolAdapter):
    """Keyless email→Gravatar profile correlation."""

    def name(self) -> str:
        return "hashtray"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 1

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []
        digest = hashlib.md5(email.encode("utf-8")).hexdigest()  # noqa: S324 — Gravatar uses MD5
        resp = http_get(_GRAVATAR_JSON.format(hash=digest), timeout=15)
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        entries = data.get("entry") if isinstance(data, dict) else None
        if not entries:
            return []
        results: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            profile_url = entry.get("profileUrl") or f"https://gravatar.com/{digest}"
            accounts = [
                {"domain": a.get("domain"), "url": a.get("url"), "username": a.get("username")}
                for a in entry.get("accounts", [])
                if isinstance(a, dict)
            ]
            urls = [u.get("value") for u in entry.get("urls", []) if isinstance(u, dict)]
            results.append(
                {
                    "email": email,
                    "hash": digest,
                    "profile_url": profile_url,
                    "display_name": entry.get("displayName") or entry.get("preferredUsername"),
                    "name": (entry.get("name") or {}) if isinstance(entry.get("name"), dict) else {},
                    "accounts": accounts,
                    "urls": [u for u in urls if u],
                    "location": entry.get("currentLocation"),
                }
            )
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            units.append(
                self.make_evidence(
                    source_platform="gravatar",
                    source_tier=2,
                    seed_value=item.get("email", ""),
                    result_type="gravatar_hit",
                    result_value=item.get("profile_url", "gravatar"),
                    platform_enrichment=item,
                    notes=f"display_name={item.get('display_name')}; "
                          f"linked_accounts={len(item.get('accounts', []))}",
                )
            )
            # Surface each linked verified account as its own correlated unit.
            for acct in item.get("accounts", []):
                url = acct.get("url")
                if not url:
                    continue
                units.append(
                    self.make_evidence(
                        source_platform=(acct.get("domain") or "linked").lower(),
                        source_tier=2,
                        seed_value=item.get("email", ""),
                        result_type="account_found",
                        result_value=url,
                        notes="gravatar_linked_account",
                    )
                )
        return units
