"""MODULE 2 — FallbackChainManager.

Runs ordered tool chains with graceful degradation, audit logging of skipped
tools, and platform/domain trigger matrices. See MODULE 2 (Section 5) of
SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID, uuid4

from api.models.evidence import EvidenceUnit

# Tier 1/2 username + email adapters
from worker_python.adapters.username.blackbird import BlackbirdAdapter
from worker_python.adapters.username.whatsmyname import WhatsMyNameAdapter
from worker_python.adapters.username.sherlock import SherlockAdapter
from worker_python.adapters.username.maigret import MaigretAdapter
from worker_python.adapters.username.nexfil import NexfilAdapter
from worker_python.adapters.username.social_analyzer import SocialAnalyzerAdapter
from worker_python.adapters.username.tracer import TracerAdapter
from worker_python.adapters.email.zehef import ZehefAdapter
from worker_python.adapters.email.socialscan import SocialScanAdapter
from worker_python.adapters.email.hashtray import HashtrayAdapter
from worker_python.adapters.email.holehe import HoleheAdapter
from worker_python.adapters.email.h8mail import H8mailAdapter
from worker_python.adapters.email.mailcat import MailcatAdapter
from worker_python.adapters.email.eyes import EyesAdapter
from worker_python.adapters.email.ghunt import GhuntAdapter

# Phone adapters
from worker_python.adapters.phone.phone_enrich import PhoneEnrichAdapter
from worker_python.adapters.phone.ignorant import IgnorantAdapter
from worker_python.adapters.phone.phoneinfoga import PhoneInfogaAdapter

# Passive recon adapters
from worker_python.adapters.passive.dorks_eye import DorksEyeAdapter
from worker_python.adapters.passive.dorksint import DorksintAdapter
from worker_python.adapters.passive.wayback_urls import WayBackURLsAdapter
from worker_python.adapters.passive.hunt_pastebin import HuntPastebinAdapter

# Platform (Tier 4) adapters
from worker_python.adapters.platform.toutatis import ToutatisAdapter
from worker_python.adapters.platform.medor import MedorAdapter
from worker_python.adapters.platform.snapintel import SnapIntelAdapter
from worker_python.adapters.platform.telegram_intel import TelegramIntelAdapter
from worker_python.adapters.platform.tiktok_userdata import TikTokUserDataAdapter
from worker_python.adapters.platform.mastosint import MastOSINTAdapter
from worker_python.adapters.platform.osintssky import OSINTSkyAdapter
from worker_python.adapters.platform.osintchan import OSINTChanAdapter
from worker_python.adapters.platform.proton_intel import ProtonIntelAdapter
from worker_python.adapters.platform.linkedin2username import LinkedIn2UsernameAdapter
from worker_python.adapters.platform.theharvester import TheHarvesterAdapter
from worker_python.adapters.platform.finalrecon import FinalReconAdapter
from worker_python.adapters.platform.webdiver import WebdiverAdapter
from worker_python.adapters.platform.sublist3r import Sublist3rAdapter
from worker_python.adapters.platform.dnstwist import DnstwistAdapter

# Go-binary adapters
from worker_go.adapters.enola import EnolaAdapter
from worker_go.adapters.detectdee import DetectDeeAdapter
from worker_go.adapters.mailsleuth import MailsleuthAdapter
from worker_go.adapters.email2whatsapp import Email2WhatsAppAdapter
# GitHub enrichment runs natively in this worker via the REST API + PAT.
# (tillson/git-hound needs a full account login and only exists in worker_go.)
from worker_python.adapters.platform.github_api import GitHubApiAdapter

logger = logging.getLogger(__name__)


class ChainExhaustedError(Exception):
    """Raised when every tool in a chain fails."""


class FallbackChainManager:
    """Executes ordered tool chains and platform/domain trigger matrices."""

    # Chain definitions (MODULE 2).
    chains = {
        "username_tier1": [BlackbirdAdapter, WhatsMyNameAdapter],
        "username_tier2": [
            SherlockAdapter, MaigretAdapter, NexfilAdapter,
            SocialAnalyzerAdapter, TracerAdapter, EnolaAdapter, DetectDeeAdapter,
        ],
        "email_tier1": [ZehefAdapter, SocialScanAdapter, HashtrayAdapter],
        "email_tier2": [
            HoleheAdapter, H8mailAdapter, MailcatAdapter, EyesAdapter,
            MailsleuthAdapter, GhuntAdapter, Email2WhatsAppAdapter,
        ],
        "phone_tier1": [PhoneEnrichAdapter, IgnorantAdapter, PhoneInfogaAdapter],
        "passive_recon": [
            DorksEyeAdapter, DorksintAdapter, WayBackURLsAdapter, HuntPastebinAdapter,
        ],
    }

    # Platform trigger matrix (auto-fires on confirmed hit).
    platform_map = {
        "instagram": [ToutatisAdapter, MedorAdapter],
        "snapchat": [SnapIntelAdapter],
        "telegram": [TelegramIntelAdapter],
        "tiktok": [TikTokUserDataAdapter],
        "mastodon": [MastOSINTAdapter],
        "bluesky": [OSINTSkyAdapter],
        "4chan": [OSINTChanAdapter],
        "protonmail": [ProtonIntelAdapter],
        "linkedin": [LinkedIn2UsernameAdapter],
        "github": [GitHubApiAdapter],
        "domain": [
            TheHarvesterAdapter, FinalReconAdapter, WebdiverAdapter,
            Sublist3rAdapter, DnstwistAdapter,
        ],
    }

    def __init__(self, case_id: UUID, run_id: UUID, analyst_id: str = "system") -> None:
        self.case_id = case_id
        self.run_id = run_id
        self.analyst_id = analyst_id

    # ---- audit helper -------------------------------------------------------
    def _audit(self, event_type: str, metadata: dict) -> None:
        """Best-effort append-only audit log write."""
        try:
            from api.db.postgres import session_scope
            from api.services.provenance import ProvenanceService

            with session_scope() as session:
                ProvenanceService().log_audit_event(
                    case_id=self.case_id,
                    run_id=self.run_id,
                    event_type=event_type,
                    actor_id=self.analyst_id,
                    metadata=metadata,
                    session=session,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("audit log unavailable (%s): %s", event_type, exc)

    @staticmethod
    def _is_positive(unit: EvidenceUnit) -> bool:
        return unit.result_type not in ("unavailable", "blocked")

    @staticmethod
    def _deduplicate(units: list[EvidenceUnit]) -> list[EvidenceUnit]:
        seen: dict[tuple, EvidenceUnit] = {}
        for unit in units:
            key = (str(unit.case_id), unit.source_platform, unit.result_value, unit.seed_value)
            existing = seen.get(key)
            if existing is None:
                seen[key] = unit
            else:
                if (unit.confidence_raw or 0) > (existing.confidence_raw or 0):
                    seen[key] = unit
        return list(seen.values())

    # ---- chain execution ----------------------------------------------------
    def execute_chain(self, chain_name: str, seed_type: str, seed_value: str) -> list[EvidenceUnit]:
        """Run every adapter in a chain; persist a status marker per tool.

        Every adapter that runs contributes at least one EvidenceUnit to the
        returned list: its positive hits when it finds any, otherwise a single
        ``unavailable`` marker recording that the tool ran (and why it produced
        nothing). These markers are excluded from correlation/persona/insights
        (which only consume positive result types) but make the pipeline status
        view truthful — a tool that ran and found nothing is no longer
        indistinguishable from a tool that never ran.
        """
        adapters = self.chains.get(chain_name)
        if not adapters:
            raise ValueError(f"unknown chain: {chain_name}")

        merged: list[EvidenceUnit] = []
        markers: list[EvidenceUnit] = []
        any_success = False

        for adapter_cls in adapters:
            adapter = adapter_cls()
            try:
                units = adapter.execute(
                    seed_value, self.case_id, self.run_id, self.analyst_id, seed_type
                )
            except Exception as exc:  # noqa: BLE001 — adapters should not raise, but be safe
                units = []
                logger.warning("adapter %s raised: %s", adapter_cls.__name__, exc)

            positives = [u for u in units if self._is_positive(u)]
            if positives:
                any_success = True
                merged.extend(positives)
            else:
                # Preserve a single status marker so the tool is recorded as
                # having run. Reuse the adapter's own ``unavailable`` unit when
                # it produced one (carries the failure note); otherwise the tool
                # ran cleanly but found nothing.
                marker = next(
                    (u for u in units if u.result_type in ("unavailable", "blocked")),
                    None,
                )
                if marker is None:
                    marker = adapter._make_unavailable_unit(seed_value, "no results")
                    marker.case_id = self.case_id
                    marker.run_id = self.run_id
                    marker.analyst_id = self.analyst_id
                markers.append(marker)
                self._audit(
                    "TOOL_SKIPPED",
                    {"tool": adapter.name(), "chain": chain_name, "reason": "no positive results"},
                )

        if not any_success:
            self._audit("CHAIN_EXHAUSTED", {"chain": chain_name, "seed_value": seed_value})

        # Positives first (deduped), then one status marker per empty/failed tool.
        return self._deduplicate(merged) + markers

    # ---- trigger matrices ---------------------------------------------------
    def _run_trigger_adapter(
        self, adapter_cls, seed: str, case_id: UUID, run_id: UUID, chain: str,
    ) -> tuple[list[EvidenceUnit], Optional[EvidenceUnit]]:
        """Run one Tier-4 trigger adapter; return (positives, status_marker).

        Mirrors :meth:`execute_chain`: a tool that runs but yields no positive
        hit still contributes a single ``unavailable``/``blocked`` marker so the
        pipeline status view can tell a tool that *ran and found nothing*
        (skipped) apart from one that *never ran* (pending). Without this, every
        Tier-4 tool that came up empty stayed "pending" forever and looked unused.
        """
        adapter = adapter_cls()
        try:
            units = adapter.execute(seed, case_id, run_id, self.analyst_id, "username")
        except Exception as exc:  # noqa: BLE001 — a trigger tool must never abort enrichment
            units = []
            logger.warning("trigger adapter %s raised: %s", adapter_cls.__name__, exc)

        positives = [u for u in units if self._is_positive(u)]
        if positives:
            return positives, None

        marker = next(
            (u for u in units if u.result_type in ("unavailable", "blocked")), None
        )
        if marker is None:
            marker = adapter._make_unavailable_unit(seed, "no results")
        marker.case_id = case_id
        marker.run_id = run_id
        marker.analyst_id = self.analyst_id
        self._audit(
            "TOOL_SKIPPED",
            {"tool": adapter.name(), "chain": chain, "reason": "no positive results"},
        )
        return [], marker

    def trigger_platform_tools(
        self, platform: str, account_url: str, case_id: Optional[UUID] = None,
        run_id: Optional[UUID] = None,
    ) -> list[EvidenceUnit]:
        """Fire the Tier 4 adapters mapped to a confirmed platform.

        Returns positive hits *plus* one status marker per adapter that ran
        empty, so :func:`preserve_and_persist` records every Tier-4 tool that
        executed (the markers are written to the DB but excluded from
        correlation/insights, which only consume positive result types).
        """
        case_id = case_id or self.case_id
        run_id = run_id or self.run_id
        adapters = self.platform_map.get(platform.lower(), [])
        seed = self._seed_from_url(account_url) or account_url
        positives: list[EvidenceUnit] = []
        markers: list[EvidenceUnit] = []
        for adapter_cls in adapters:
            pos, marker = self._run_trigger_adapter(
                adapter_cls, seed, case_id, run_id, f"platform:{platform.lower()}"
            )
            positives.extend(pos)
            if marker is not None:
                markers.append(marker)
        return self._deduplicate(positives) + markers

    def trigger_domain_tools(
        self, domain: str, case_id: Optional[UUID] = None, run_id: Optional[UUID] = None
    ) -> list[EvidenceUnit]:
        """Fire the domain Tier 4 matrix (theHarvester/finalrecon/webdiver/...).

        Like :meth:`trigger_platform_tools`, returns positives plus a status
        marker per empty tool so every domain tool's execution is recorded.
        """
        case_id = case_id or self.case_id
        run_id = run_id or self.run_id
        positives: list[EvidenceUnit] = []
        markers: list[EvidenceUnit] = []
        for adapter_cls in (
            TheHarvesterAdapter, FinalReconAdapter, WebdiverAdapter,
            Sublist3rAdapter, DnstwistAdapter,
        ):
            pos, marker = self._run_trigger_adapter(
                adapter_cls, domain, case_id, run_id, "domain"
            )
            positives.extend(pos)
            if marker is not None:
                markers.append(marker)
        return self._deduplicate(positives) + markers

    @staticmethod
    def _seed_from_url(url: str) -> str:
        """Extract a likely username from a profile URL (last path segment)."""
        cleaned = url.rstrip("/")
        if "/" in cleaned:
            return cleaned.rsplit("/", 1)[-1].lstrip("@")
        return cleaned
