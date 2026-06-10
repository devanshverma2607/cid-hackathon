"""Email2WhatsAppAdapter — Tier 2 Go WhatsApp linkage, Tor (Section 11.19)."""
from __future__ import annotations

import os

from worker_python.adapters.base import ToolAdapter
from worker_go.adapters import go_binary
from api.models.evidence import EvidenceUnit


class Email2WhatsAppAdapter(ToolAdapter):
    """Wraps email2whatsapp (./tools/go/email2whatsapp -e {email}); Tor. Weight +7."""

    def name(self) -> str:
        return "email2whatsapp"

    def version(self) -> str:
        return "go"

    def get_tool_tier(self) -> int:
        return 2

    def get_proxy_tier(self) -> int:
        return 1  # Tor — WhatsApp is rate-sensitive

    def health_check(self) -> bool:
        path = go_binary("email2whatsapp")
        return os.path.exists(path) and os.access(path, os.X_OK)

    def run(self, seed: str) -> list[dict]:
        # Real CLI: `email2whatsapp -email target@gmail.com` scrapes Brazilian
        # account-recovery sites (Magalu, PayPal-BR, PagBank, MercadoLivre,
        # Rappi, Uber) for the masked phone digits each site leaks, then derives
        # full candidate numbers. The `-whatsapp` / `-bruteforce` modes need an
        # interactive WhatsApp QR login / captchas and cannot run headless, so
        # we only use the keyless `-email` scraping mode here.
        #
        # NOTE: `-email` drives a headed Chrome (chromedp, headless=false) and
        # therefore needs a Chromium binary + display in the worker image. When
        # those are absent the tool emits no numbers and this adapter returns an
        # empty list, which the fallback chain records as an 'unavailable'
        # marker rather than a failure.
        stdout, stderr, code = self.run_subprocess(
            [go_binary("email2whatsapp"), "-email", seed], timeout=300, use_tor=True
        )
        results: list[dict] = []
        seen: set[str] = set()
        for line in (stdout or "").splitlines():
            digits = "".join(ch for ch in line if ch.isdigit())
            # Candidate phone numbers are at least 10 digits (national) and at
            # most 15 (E.164 max). Shorter digit runs are progress/log noise.
            if 10 <= len(digits) <= 15:
                phone = f"+{digits}"
                if phone not in seen:
                    seen.add(phone)
                    results.append({"phone": phone})
        return results

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            phone = item.get("phone", "")
            if not phone:
                continue
            units.append(
                self.make_evidence(
                    source_platform="whatsapp",
                    source_tier=2,
                    seed_value="",
                    result_type="whatsapp_hit",
                    result_value=phone,
                )
            )
        return units
