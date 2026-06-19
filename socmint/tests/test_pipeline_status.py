"""Tests for the pipeline status lifecycle state machine + expected-tool mapping.

Covers the accuracy contract for `/api/v1/pipeline/status`: the scan stays
"running" through every active phase (queued → sweeping → analysing → pivoting),
reports the correct finish time only once work has genuinely gone quiet, and
scopes the progress denominator to the tools a case actually exercises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.routers.pipeline import (
    ACTIVE_WINDOW, WATCHDOG_WINDOW, _compute_lifecycle, _expected_tools,
)

_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> datetime:
    return _NOW - timedelta(seconds=seconds)


def _life(**over):
    base = dict(
        started_at=_ago(60), last_activity_at=None, correlation_at=None,
        last_pivot_at=None, pivot_hops=0, dispatch_failed=False, total_hits=0,
        server_now=_NOW,
    )
    base.update(over)
    return _compute_lifecycle(**base)


# --------------------------------------------------------------- state machine
def test_queued_before_first_evidence():
    r = _life(started_at=_ago(5))
    assert r["state"] == "running"
    assert r["phase"] == "queued"
    assert r["finished_at"] is None


def test_sweeping_while_evidence_is_fresh():
    r = _life(last_activity_at=_ago(3), total_hits=5)
    assert r["state"] == "running"
    assert r["phase"] == "sweeping"


def test_running_while_awaiting_correlation_after_quiet_sweep():
    # Sweep produced hits then went quiet, but correlation has not fired yet and
    # the watchdog window has not elapsed → still "running", not "complete".
    r = _life(started_at=_ago(200), last_activity_at=_ago(ACTIVE_WINDOW + 30), total_hits=10)
    assert r["state"] == "running"
    assert r["phase"] == "sweeping"


def test_analysing_after_correlation_no_pivots():
    r = _life(last_activity_at=_ago(5), correlation_at=_ago(8), total_hits=12)
    assert r["state"] == "running"
    assert r["phase"] == "analysing"


def test_pivoting_after_correlation_with_pivot_hops():
    r = _life(started_at=_ago(300), last_activity_at=_ago(10),
              correlation_at=_ago(120), last_pivot_at=_ago(40),
              pivot_hops=2, total_hits=30)
    assert r["state"] == "running"
    assert r["phase"] == "pivoting"


def test_complete_uses_latest_event_as_finish():
    # Quiet for longer than the active window, correlation done → complete, with
    # the finish time taken from the latest of activity / correlation / pivot.
    r = _life(started_at=_ago(400), last_activity_at=_ago(200),
              correlation_at=_ago(250), last_pivot_at=_ago(210),
              pivot_hops=1, total_hits=40)
    assert r["state"] == "complete"
    assert r["phase"] == "complete"
    assert r["finished_at"] == _ago(200)            # latest of the three
    assert abs(r["elapsed_seconds"] - 200.0) < 0.1   # start → last activity


def test_complete_when_watchdog_passed_with_hits_but_no_correlation():
    r = _life(started_at=_ago(WATCHDOG_WINDOW + 200),
              last_activity_at=_ago(WATCHDOG_WINDOW + 100), total_hits=8)
    assert r["state"] == "complete"


def test_idle_empty_case_after_watchdog():
    r = _life(started_at=_ago(WATCHDOG_WINDOW + 200))
    assert r["state"] == "idle"
    assert r["phase"] == "idle"


def test_failed_dispatch_with_no_hits():
    r = _life(started_at=_ago(30), dispatch_failed=True, total_hits=0)
    assert r["state"] == "failed"


def test_fresh_evidence_resumes_running_after_apparent_quiet():
    # A late pivot hop lands new evidence → the scan is genuinely active again.
    r = _life(started_at=_ago(500), last_activity_at=_ago(2),
              correlation_at=_ago(300), last_pivot_at=_ago(5),
              pivot_hops=3, total_hits=50)
    assert r["state"] == "running"
    assert r["phase"] == "pivoting"


def test_no_started_at_is_idle():
    r = _life(started_at=None)
    assert r["state"] == "idle"


# ------------------------------------------------------------- expected tools
def test_expected_tools_username_excludes_email_and_phone():
    tools = _expected_tools({"username"})
    assert "sherlock" in tools and "blackbird" in tools
    assert "dorks_eye" in tools                      # passive recon always runs
    assert "holehe" not in tools and "ghunt" not in tools   # email tools excluded
    assert "ignorant" not in tools                   # phone tools excluded


def test_expected_tools_email_includes_username_chain():
    # The email header also runs the username sweeps (see _build_header).
    tools = _expected_tools({"email"})
    assert {"holehe", "ghunt", "zehef"} <= tools     # email chain
    assert {"sherlock", "blackbird"} <= tools        # username chain too


def test_expected_tools_phone_only_phone_and_passive():
    tools = _expected_tools({"phone"})
    assert {"phone_enrich", "ignorant", "phoneinfoga"} <= tools
    assert "sherlock" not in tools and "holehe" not in tools


def test_expected_tools_profile_url_maps_to_username():
    assert "sherlock" in _expected_tools({"profile_url"})


def test_expected_tools_empty_seedset_is_empty():
    assert _expected_tools(set()) == set()
