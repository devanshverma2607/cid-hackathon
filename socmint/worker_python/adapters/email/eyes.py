"""EyesAdapter — Tier 2 email social presence recon (Section 11.17).

Reimplemented key-less. The upstream ``eyes`` script imports OpenCV (``cv2``)
for its facial-recognition module, which fails to bootstrap in this headless
image (missing native ``libGL``), so the old adapter always errored out. This
implementation keeps the tool's core purpose — building an *identity card* from
an email — using only keyless sources: the Gravatar profile/avatar plus a
public-profile existence check on the handle derived from the email local-part.
"""
from __future__ import annotations

import hashlib
import re

from worker_python.adapters._net import http_get, username_profiles
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_GRAVATAR_JSON = "https://en.gravatar.com/{hash}.json"
_GRAVATAR_AVATAR = "https://www.gravatar.com/avatar/{hash}?d=404"


class EyesAdapter(ToolAdapter):
    """Keyless email→identity recon (Gravatar avatar/profile + handle presence)."""

    def name(self) -> str:
        return "eyes"

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
        digest = hashlib.md5(email.encode("utf-8")).hexdigest()  # noqa: S324 — Gravatar uses MD5
        results: list[dict] = []

        # 1. Public Gravatar avatar (d=404 ⇒ 200 only when a real avatar exists).
        avatar = http_get(_GRAVATAR_AVATAR.format(hash=digest), timeout=12)
        if avatar is not None and avatar.status_code == 200:
            results.append(
                {
                    "kind": "avatar",
                    "platform": "gravatar",
                    "url": _GRAVATAR_AVATAR.format(hash=digest),
                    "email": email,
                }
            )
            # If an avatar exists, pull the profile card for the display name.
            prof = http_get(_GRAVATAR_JSON.format(hash=digest), timeout=12)
            if prof is not None and prof.status_code == 200:
                try:
                    entries = (prof.json() or {}).get("entry") or []
                except ValueError:
                    entries = []
                for entry in entries:
                    if isinstance(entry, dict):
                        results.append(
                            {
                                "kind": "profile",
                                "platform": "gravatar",
                                "url": entry.get("profileUrl") or f"https://gravatar.com/{digest}",
                                "email": email,
                                "display_name": entry.get("displayName"),
                            }
                        )

        # 2. Handle derived from the email local-part → public-profile presence.
        handle = re.sub(r"[^a-z0-9._-]", "", email.split("@", 1)[0])
        if handle:
            for prof in username_profiles(handle, use_tor=True):
                results.append(
                    {
                        "kind": "account",
                        "platform": prof.get("platform"),
                        "url": prof.get("url"),
                        "email": email,
                        "handle": handle,
                    }
                )
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            url = item.get("url", "")
            if not url:
                continue
            result_type = "gravatar_hit" if item.get("platform") == "gravatar" else "account_found"
            units.append(
                self.make_evidence(
                    source_platform=(item.get("platform") or "unknown").lower(),
                    source_tier=2,
                    seed_value=item.get("email", ""),
                    result_type=result_type,
                    result_value=url,
                    notes=item.get("display_name") or item.get("kind"),
                )
            )
        return units
