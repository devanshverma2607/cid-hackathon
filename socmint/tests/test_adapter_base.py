"""Tests for the ToolAdapter base class (MODULE 2) — graceful degradation."""
from __future__ import annotations

from uuid import uuid4

from api.models.evidence import EvidenceUnit
from worker_python.adapters.base import ToolAdapter


class _OkAdapter(ToolAdapter):
    def name(self) -> str:
        return "fake_ok"

    def version(self) -> str:
        return "1.0"

    def health_check(self) -> bool:
        return True

    def run(self, seed: str) -> list[dict]:
        return [{"url": f"https://example.com/{seed}"}]

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        return [
            self.make_evidence(
                source_platform="example",
                seed_value="alice",
                result_type="account_found",
                result_value=r["url"],
            )
            for r in raw
        ]

    def get_tool_tier(self) -> int:
        return 1


class _BrokenHealthAdapter(_OkAdapter):
    def name(self) -> str:
        return "fake_broken"

    def health_check(self) -> bool:
        return False


class _RaisingAdapter(_OkAdapter):
    def name(self) -> str:
        return "fake_raise"

    def run(self, seed: str) -> list[dict]:
        raise RuntimeError("boom")


def _ctx():
    return uuid4(), uuid4(), "analyst-1"


def test_successful_execute_returns_units():
    case_id, run_id, analyst = _ctx()
    units = _OkAdapter().execute("alice", case_id, run_id, analyst)
    assert len(units) == 1
    assert units[0].result_type == "account_found"
    assert units[0].case_id == case_id
    assert units[0].analyst_id == analyst


def test_failed_health_check_degrades_to_unavailable():
    case_id, run_id, analyst = _ctx()
    units = _BrokenHealthAdapter().execute("alice", case_id, run_id, analyst)
    assert len(units) == 1
    assert units[0].result_type == "unavailable"


def test_raising_run_degrades_to_unavailable():
    case_id, run_id, analyst = _ctx()
    units = _RaisingAdapter().execute("alice", case_id, run_id, analyst)
    assert len(units) == 1
    assert units[0].result_type == "unavailable"
    assert "boom" in (units[0].notes or "")


def test_default_proxy_tier_is_direct():
    assert _OkAdapter().get_proxy_tier() == 2
