"""ToutatisAdapter — Tier 4 Instagram metadata (Section 11.27)."""
from __future__ import annotations

import os
import re
import shutil

from worker_python.adapters.base import ToolAdapter, ToolUnavailableError
from api.models.evidence import EvidenceUnit

# toutatis prints human-readable "Field : value" lines (it does not emit JSON).
_FIELD_RE = re.compile(r"^\s*[-\u2022]?\s*([A-Za-z][A-Za-z /]+?)\s*:\s*(.+?)\s*$")
# Markers that prove a real profile was returned (vs. an auth/lookup failure).
_SUCCESS_MARKERS = ("user id", "userid", "full name", "informations about")
_FAILURE_MARKERS = (
    "sessionid", "session id", "invalid", "expired", "checkpoint",
    "login_required", "please wait", "not found", "doesn't exist", "does not exist",
)


class ToutatisAdapter(ToolAdapter):
    """Wraps toutatis (toutatis -u {username} -s {session_id}); parses fields.

    Requires a *valid* Instagram session cookie (``INSTAGRAM_SESSION_ID``). When
    the cookie is missing, expired or rejected, the tool yields no profile data;
    rather than silently returning nothing, the adapter degrades to an explicit
    'unavailable' marker so the analyst knows a valid session is needed.
    """

    def name(self) -> str:
        return "toutatis"

    def version(self) -> str:
        return "toutatis"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        # Direct egress (2), NOT Tor. Instagram blocks Tor exit nodes outright,
        # and this request is already authenticated by the session cookie, so
        # routing it through Tor only guarantees a connection failure.
        return 2

    def health_check(self) -> bool:
        return shutil.which("toutatis") is not None and bool(os.environ.get("INSTAGRAM_SESSION_ID"))

    def run(self, seed: str) -> list[dict]:
        session_id = os.environ.get("INSTAGRAM_SESSION_ID", "")
        stdout, stderr, code = self.run_subprocess(
            ["toutatis", "-u", seed, "-s", session_id], timeout=180, use_tor=False
        )
        text = stdout or ""
        lowered = text.lower()

        fields: dict[str, str] = {}
        for line in text.splitlines():
            match = _FIELD_RE.match(line)
            if match:
                fields[match.group(1).strip().lower()] = match.group(2).strip()

        got_profile = any(marker in lowered for marker in _SUCCESS_MARKERS) and fields
        if not got_profile:
            # No usable profile → honest degradation (invalid/expired session,
            # rate-limit, checkpoint, or the account simply not existing).
            reason = "instagram session invalid/expired or account unavailable"
            for marker in _FAILURE_MARKERS:
                if marker in lowered:
                    reason = f"toutatis: {marker}"
                    break
            raise ToolUnavailableError(reason)

        return [{"username": seed, "fields": fields}]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            fields = data.get("fields", {}) if isinstance(data, dict) else {}
            username = data.get("username", "") if isinstance(data, dict) else ""
            handle = fields.get("username") or username
            units.append(
                self.make_evidence(
                    source_platform="instagram",
                    source_tier=1,
                    seed_value="",
                    result_type="account_found",
                    result_value=f"https://instagram.com/{handle}",
                    platform_enrichment=fields or None,
                    notes="; ".join(
                        f"{k}={v}" for k, v in fields.items()
                        if k in ("full name", "public email", "public phone",
                                 "obfuscated email", "obfuscated phone", "user id")
                    )[:500] or None,
                )
            )
        return units
