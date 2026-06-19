"""ProxyNovaAdapter — Tier 2 combo-list exposure lookup (keyless ProxyNova API).

ProxyNova's "comb" endpoint (https://api.proxynova.com/comb) searches the
"Compilation of Many Breaches" (COMB) combo lists for an identifier and returns
matching ``account:secret`` lines. For SOCMINT this surfaces two things the
feature explicitly asks for:

* confirmation that the seed identifier appears in aggregated leak/combo lists
  (``breach_hit``), and
* the *other* identifiers (e.g. associated email addresses) that co-occur with a
  queried username — i.e. **email-username associations**.

SECURITY / ETHICS: combo lines contain plaintext passwords. This adapter NEVER
stores or emits the secret half — passwords are dropped immediately and only the
non-secret account identifiers are kept, with their local-part masked. It exists
to map identity relationships, not to harvest credentials.
"""
from __future__ import annotations

import re

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{1,38})$")

_COMB_URL = "https://api.proxynova.com/comb?query={q}&limit={limit}"
_QUERY_LIMIT = 25
_MAX_ASSOCIATIONS = 8


def _mask_identifier(ident: str) -> str:
    """Mask the local-part of an email / a bare handle, keeping the domain.

    ``john.doe@gmail.com`` -> ``j******@gmail.com``; ``john.doe`` -> ``j******``.
    """
    ident = (ident or "").strip()
    if not ident:
        return ""
    if "@" in ident:
        local, _, domain = ident.partition("@")
        if not local:
            return "@" + domain
        masked = local[0] + "*" * max(1, len(local) - 1)
        return f"{masked}@{domain}"
    return ident[0] + "*" * max(1, len(ident) - 1)


class ProxyNovaAdapter(ToolAdapter):
    """Keyless combo-list presence + associated-identifier extraction (masked)."""

    def name(self) -> str:
        return "proxynova"

    def version(self) -> str:
        return "comb-keyless"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 2  # direct egress

    def health_check(self) -> bool:
        return True

    # ---- collection ---------------------------------------------------------
    def run(self, seed: str) -> list[dict]:
        s = (seed or "").strip()
        is_email = bool(_EMAIL_RE.match(s.lower()))
        if is_email:
            s = s.lower()
        elif not _USERNAME_RE.match(s):
            return []  # phone / junk seeds are not meaningful combo queries

        url = _COMB_URL.format(q=s, limit=_QUERY_LIMIT)
        resp = http_get(url, use_tor=False, timeout=25)
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []

        lines = data.get("lines")
        if not isinstance(lines, list) or not lines:
            return []

        total = data.get("count")
        associations: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if not isinstance(line, str) or ":" not in line:
                continue
            # Drop the secret half immediately — only the account identifier is kept.
            account = line.split(":", 1)[0].strip()
            if not account:
                continue
            key = account.lower()
            # An associated identifier is one that differs from the seed itself.
            if key == s.lower():
                continue
            if key in seen:
                continue
            seen.add(key)
            associations.append(_mask_identifier(account))
            if len(associations) >= _MAX_ASSOCIATIONS:
                break

        match_count = len([l for l in lines if isinstance(l, str) and ":" in l])
        return [
            {
                "seed": seed,
                "count": total if isinstance(total, int) else match_count,
                "matches": match_count,
                "associations": associations,
            }
        ]

    # ---- mapping ------------------------------------------------------------
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            units.append(
                self.make_evidence(
                    source_platform="breach",
                    source_tier=3,
                    seed_value=item.get("seed", ""),
                    result_type="breach_hit",
                    result_value="proxynova-combolist",
                    notes=self._format_notes(item),
                )
            )
        return units

    @staticmethod
    def _format_notes(item: dict) -> str:
        parts = ["source=proxynova-combolist", "passwords=masked/omitted"]
        if item.get("count"):
            parts.append(f"combolist_entries={item['count']}")
        assoc = item.get("associations") or []
        if assoc:
            parts.append("associated_identifiers=" + ", ".join(assoc))
        return " ".join(parts)[:2000]
