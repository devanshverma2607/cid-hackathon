"""ProtonIntelAdapter — Tier 4 ProtonMail OSINT (Section 11.35).

Reimplemented key-less against Proton's public PGP key server (HKP-style
``pks/lookup``). Proton publishes a key index for every address as an
anti-enumeration measure, but the key's **creation timestamp** is a genuine
signal: real, long-lived accounts have older keys than freshly minted/dummy
ones. The adapter records the key fingerprint and creation time as a weak
enrichment. The old adapter shelled out to a ``proton_intel.py`` script that was
never present, so it always returned empty.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from worker_python.adapters._net import http_get
from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit

_PKS_INDEX = "https://mail-api.proton.me/pks/lookup?op=index&search={email}"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# HKP machine-readable index lines: pub:<fpr>:<algo>:<keylen>:<created>:<expires>:<flags>
_PUB_RE = re.compile(r"^pub:([0-9A-Fa-f]+):(\d*):(\d*):(\d*):(\d*):", re.M)
_UID_RE = re.compile(r"^uid:([^:]+):", re.M)


class ProtonIntelAdapter(ToolAdapter):
    """Keyless ProtonMail registration signal via the public PKS key index."""

    def name(self) -> str:
        return "proton_intel"

    def version(self) -> str:
        return "proton-pks"

    def get_tool_tier(self) -> int:
        return 4

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        email = (seed or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return []
        resp = http_get(_PKS_INDEX.format(email=email), timeout=15)
        if resp is None or resp.status_code != 200:
            return []
        body = resp.text or ""
        pub = _PUB_RE.search(body)
        if not pub:
            return []
        fingerprint = pub.group(1)
        created_epoch = pub.group(4)
        created_iso = None
        if created_epoch.isdigit() and int(created_epoch) > 0:
            created_iso = datetime.fromtimestamp(
                int(created_epoch), tz=timezone.utc
            ).isoformat()
        uids = _UID_RE.findall(body)
        return [
            {
                "email": email,
                "fingerprint": fingerprint,
                "key_created_epoch": int(created_epoch) if created_epoch.isdigit() else None,
                "key_created": created_iso,
                "uids": uids[:5],
            }
        ]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            units.append(
                self.make_evidence(
                    source_platform="protonmail",
                    source_tier=2,
                    seed_value=data.get("email", ""),
                    result_type="email_registered",
                    result_value=data.get("email", "protonmail"),
                    platform_enrichment=data,
                    notes=f"pgp_key_created={data.get('key_created')}",
                )
            )
        return units
