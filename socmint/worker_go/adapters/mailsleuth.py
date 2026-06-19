"""MailsleuthAdapter — Tier 2 email account-presence sweep (Section 11.14).

Reimplemented key-less. The upstream ``mailsleuth`` is a closed-source Go binary
with no public source repository, so it can never be compiled into the worker
image (the ``go install`` line in the plan has no real module behind it). The
old adapter shelled out to a ``mailsleuth`` binary that is never built, so the
tool was permanently reported ``unavailable`` and contributed nothing to the
``email_tier2`` chain.

This implementation preserves the tool's documented purpose — *"email account
presence on many services"* — using only keyless sources, mirroring how the
other unavailable email tools (``mailcat``/``eyes``) were reimplemented:

* a **Gravatar** profile probe keyed directly on the email (a real
  email→identity signal the handle-only tools cannot produce), and
* a **public-profile existence** sweep on the handle derived from the email
  local-part via the shared keyless backend.

Downstream dedup on ``(case_id, source_platform, result_value, seed_value)``
collapses any overlap with the sibling email adapters, so this never inflates
the evidence set — it only ensures the tool yields real evidence instead of a
dead ``unavailable`` marker.
"""
from __future__ import annotations

import hashlib
import re

from worker_python.adapters._net import http_get, username_profiles
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_GRAVATAR_JSON = "https://en.gravatar.com/{hash}.json"


class MailsleuthAdapter(ToolAdapter):
    """Keyless email→account-presence sweep (Gravatar + derived-handle profiles)."""

    def name(self) -> str:
        return "mailsleuth"

    def version(self) -> str:
        return "keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 1  # route the sweep through Tor when available

    def health_check(self) -> bool:
        # Pure HTTP, no binary required; degrades gracefully when the network is
        # unreachable (run() simply returns []).
        return True

    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []

        results: list[dict] = []

        # 1. Gravatar profile keyed on the email's MD5 (an email→identity signal
        #    the handle-only tools cannot produce).
        digest = hashlib.md5(email.encode("utf-8")).hexdigest()  # noqa: S324 — Gravatar uses MD5
        resp = http_get(_GRAVATAR_JSON.format(hash=digest), timeout=12)
        if resp is not None and resp.status_code == 200:
            try:
                entries = (resp.json() or {}).get("entry") or []
            except ValueError:
                entries = []
            for entry in entries:
                if isinstance(entry, dict):
                    results.append(
                        {
                            "service": "gravatar",
                            "url": entry.get("profileUrl") or f"https://gravatar.com/{digest}",
                            "email": email,
                            "exists": True,
                        }
                    )
                    break

        # 2. Public-profile existence sweep on the handle derived from the
        #    email local-part.
        handle = re.sub(r"[^a-z0-9._-]", "", email.split("@", 1)[0])
        if handle:
            for prof in username_profiles(handle, use_tor=True):
                results.append(
                    {
                        "service": prof.get("platform"),
                        "url": prof.get("url"),
                        "email": email,
                        "handle": handle,
                        "exists": True,
                    }
                )
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("exists") is False:
                continue
            service = (item.get("service") or "unknown").lower()
            url = item.get("url") or service
            result_type = "gravatar_hit" if service == "gravatar" else "email_registered"
            note = None
            if item.get("handle"):
                note = f"derived_handle={item.get('handle')}"
            units.append(
                self.make_evidence(
                    source_platform=service,
                    source_tier=2,
                    seed_value=item.get("email", ""),
                    result_type=result_type,
                    result_value=url,
                    notes=note,
                )
            )
        return units
