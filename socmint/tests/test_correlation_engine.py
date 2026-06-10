"""Tests for the Correlation Engine scoring model (MODULE 6, Section 9)."""
from __future__ import annotations

from datetime import datetime, timezone

from api.services.correlation import (
    CorrelationEngine, THRESHOLD_HIGH, THRESHOLD_MEDIUM, MIN_SIGNALS,
)


def _now():
    return datetime.now(timezone.utc)


def test_two_signal_rule_discards_single_signal():
    # A single signal, regardless of weight, must never exceed DISCARD.
    assert CorrelationEngine._tier(25, 1) == "DISCARD"


def test_tier_boundaries():
    assert CorrelationEngine._tier(THRESHOLD_HIGH, MIN_SIGNALS) == "HIGH"
    assert CorrelationEngine._tier(THRESHOLD_MEDIUM, MIN_SIGNALS) == "MEDIUM"
    assert CorrelationEngine._tier(25, MIN_SIGNALS) == "LOW"
    assert CorrelationEngine._tier(24, MIN_SIGNALS) == "DISCARD"


def test_identical_username_plus_email_is_high(make_unit):
    engine = CorrelationEngine()
    a = [
        make_unit(source_platform="github", result_type="account_found",
                  result_value="https://github.com/alice", seed_value="alice"),
        make_unit(source_platform="github", result_type="email_registered",
                  result_value="alice@example.com", seed_type="email", seed_value="alice@example.com"),
    ]
    b = [
        make_unit(source_platform="twitter", result_type="account_found",
                  result_value="https://twitter.com/alice", seed_value="alice"),
        make_unit(source_platform="twitter", result_type="email_registered",
                  result_value="alice@example.com", seed_type="email", seed_value="alice@example.com"),
    ]
    result = engine.compute_confidence(a, b)
    # username (25) + email (20) = 45 with 2 signals → LOW band (25-49).
    assert result["signal_count"] >= 2
    assert result["confidence_score"] == 45
    assert result["confidence_tier"] == "LOW"
    assert "identical_username" in result["signal_breakdown"]
    assert "email_match" in result["signal_breakdown"]


def test_unrelated_accounts_discarded(make_unit):
    engine = CorrelationEngine()
    a = [make_unit(result_value="https://github.com/alice", seed_value="alice")]
    b = [make_unit(source_platform="twitter",
                   result_value="https://twitter.com/bob", seed_value="bob")]
    result = engine.compute_confidence(a, b)
    assert result["confidence_tier"] == "DISCARD"


def test_emails_match_handles_plus_alias():
    assert CorrelationEngine._emails_match({"alice+news@example.com"}, {"alice@example.com"})


def test_emails_match_distinct_returns_false():
    assert not CorrelationEngine._emails_match({"alice@example.com"}, {"bob@example.com"})
