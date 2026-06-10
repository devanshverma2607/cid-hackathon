"""SocidExtractorAdapter — Tier 4 structured-identifier extraction (soxoj/socid-extractor).

Given a *confirmed* profile URL, socid_extractor parses the page into a flat,
machine-readable record: display name, alternate usernames, account-creation
date, external links and — most valuable — the platform's stable internal IDs
(GAIA id, Facebook UID, Instagram pk, Yandex public id, …) that survive renames
and deletions.

This is pure enrichment that strengthens the correlation brain: every alternate
username, email and linked profile it surfaces is emitted as its own evidence
unit so the Pivot Engine re-seeds it, and the internal IDs are attached to the
account record for high-confidence cross-platform identity matching.
"""
from __future__ import annotations

import ast
import re

import httpx

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")

# Keys whose values are alternate handles we can re-seed as username pivots.
_USERNAME_KEYS = ("username", "name", "screen_name", "nickname")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SocidExtractorAdapter(ToolAdapter):
    """Extracts structured account fields + internal IDs from a profile URL."""

    def name(self) -> str:
        return "socid_extractor"

    def version(self) -> str:
        return "socid"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        try:
            import socid_extractor  # noqa: F401
        except Exception:
            return False
        return True

    def run(self, seed: str) -> list[dict]:
        url = (seed or "").strip()
        if "://" not in url:
            # socid_extractor operates on profile URLs, not bare handles.
            return []

        import socid_extractor

        try:
            with httpx.Client(
                timeout=30.0, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client:
                resp = client.get(url)
            if resp.status_code != 200 or not resp.text:
                return []
            info = socid_extractor.extract(resp.text)
        except Exception:
            return []

        if not info or not isinstance(info, dict):
            return []
        return [{"url": url, "info": info}]

    @staticmethod
    def _parse_links(value) -> list[str]:
        """socid returns 'links' as a list or a stringified list — normalise it."""
        if isinstance(value, list):
            items = value
        elif isinstance(value, str):
            try:
                items = ast.literal_eval(value)
                if not isinstance(items, list):
                    items = _URL_RE.findall(value)
            except (ValueError, SyntaxError):
                items = _URL_RE.findall(value)
        else:
            return []
        return [str(i).strip() for i in items if isinstance(i, (str, bytes)) and str(i).strip()]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for rec in raw:
            url = rec.get("url", "")
            info = rec.get("info") or {}

            discovered_usernames: list[str] = []
            emails: list[str] = []
            for key, val in info.items():
                if not isinstance(val, str):
                    continue
                low_key = key.lower()
                if low_key in _USERNAME_KEYS or low_key.endswith("_username"):
                    handle = val.strip().lstrip("@")
                    if handle and " " not in handle and handle not in discovered_usernames:
                        discovered_usernames.append(handle)
                # Any field value may embed an email address.
                emails.extend(_EMAIL_RE.findall(val))

            links = self._parse_links(info.get("links"))

            enrichment = {
                "socid": {k: v for k, v in info.items() if k != "links"},
                "fullname": info.get("fullname") or info.get("name"),
                "username": info.get("username"),
                "created_at": info.get("created_at"),
                "discovered_usernames": discovered_usernames or None,
                "links": links or None,
            }

            units.append(
                self.make_evidence(
                    source_platform="socid_extractor",
                    source_tier=2,
                    result_type="account_found",
                    result_value=url,
                    platform_enrichment=enrichment,
                    notes="structured profile record (socid_extractor)",
                )
            )

            # Each external linked profile → its own account hit (pivot + url overlap).
            seen_links: set[str] = set()
            for link in links:
                if "://" not in link or link in seen_links or link.rstrip("/") == url.rstrip("/"):
                    continue
                seen_links.add(link)
                units.append(
                    self.make_evidence(
                        source_platform=_host_of(link),
                        source_tier=2,
                        result_type="account_found",
                        result_value=link,
                        notes="linked profile via socid_extractor",
                    )
                )

            # Emails exposed in the profile → email pivots for the brain.
            seen_emails: set[str] = set()
            for email in emails:
                email = email.lower()
                if email in seen_emails:
                    continue
                seen_emails.add(email)
                units.append(
                    self.make_evidence(
                        source_platform="socid_extractor",
                        source_tier=2,
                        result_type="email_registered",
                        result_value=email,
                        notes="email exposed in profile (socid_extractor)",
                    )
                )
        return units


def _host_of(url: str) -> str:
    """Best-effort platform label from a URL host (instagram.com → instagram)."""
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] if host else "unknown"
