"""Tests for the Behavioral Engine (pure-core, no DB/network).

Covers timezone inference, rhythm breaks, velocity spikes, and cross-platform
correlation with fixture timestamp lists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.services.behavioral_engine import (
    compute_frequency,
    cross_platform_correlation,
    detect_rhythm_breaks,
    detect_velocity_spikes,
    infer_timezone,
    posting_rhythm_similar,
    tz_offsets_agree,
    tz_offsets_conflict,
)


# ---------------------------------------------------------------------------
# Helpers — generate fixture timestamp lists
# ---------------------------------------------------------------------------
def _make_timestamps(
    count: int,
    start: datetime | None = None,
    active_hours: tuple[int, ...] = (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22),
    interval_minutes: int = 60,
) -> list[str]:
    """Build a list of ISO timestamps concentrated in ``active_hours`` (UTC)."""
    start = start or datetime(2025, 1, 1, tzinfo=timezone.utc)
    out: list[str] = []
    current = start
    while len(out) < count:
        if current.hour in active_hours:
            out.append(current.isoformat())
        current += timedelta(minutes=interval_minutes)
    return out


def _make_night_shift_timestamps(count: int) -> list[str]:
    """Timestamps concentrated in night hours (22-06 UTC) — daytime trough."""
    return _make_timestamps(
        count,
        active_hours=(22, 23, 0, 1, 2, 3, 4, 5, 6),
    )


# ============================= Frequency ====================================

class TestComputeFrequency:

    def test_basic_frequency(self):
        ts = _make_timestamps(50)
        result = compute_frequency(ts)
        assert result["sufficient"] is True
        assert result["sample_size"] == 50
        assert len(result["hour_histogram"]) == 24
        assert len(result["dow_histogram"]) == 7
        assert result["activity_density"] > 0

    def test_insufficient_data(self):
        result = compute_frequency(["2025-01-01T12:00:00Z"])
        assert result["sufficient"] is False

    def test_empty_list(self):
        result = compute_frequency([])
        assert result["sufficient"] is False


# ============================ TZ Inference ==================================

class TestInferTimezone:

    def test_clear_trough_infers_timezone(self):
        """Active 08-22 UTC → sleep trough ~23-05 UTC → ~UTC+0 ± a few hours."""
        ts = _make_timestamps(100, active_hours=tuple(range(8, 23)))
        freq = compute_frequency(ts)
        result = infer_timezone(freq["hour_histogram"], freq["sample_size"])
        assert result is not None
        assert result["confidence_raw"] <= 0.55
        assert result["tag"] == "[behavioral-inferred]"
        # The offset should roughly indicate UTC+0 to UTC+5 range
        assert -6 <= result["utc_offset_point"] <= 8

    def test_ambiguous_trough_abstains(self):
        """Uniform distribution across all hours → no clear trough."""
        # Build a histogram with every hour having roughly equal counts
        flat_hist = [10] * 24
        result = infer_timezone(flat_hist, 240)
        # With perfectly flat distribution, trough ratio < 3 → should abstain
        assert result is None

    def test_insufficient_sample_abstains(self):
        """Fewer than 30 posts → no inference."""
        hist = [1, 0, 0, 0, 0, 0, 0, 1, 2, 3, 2, 2, 3, 2, 1, 2, 3, 2, 1, 1, 0, 0, 0, 0]
        result = infer_timezone(hist, 20)
        assert result is None

    def test_night_shift_pattern(self):
        """Active at night (22-06 UTC) → trough in daytime."""
        ts = _make_night_shift_timestamps(100)
        freq = compute_frequency(ts)
        result = infer_timezone(freq["hour_histogram"], freq["sample_size"])
        # Should either infer a very different offset or abstain (edge case)
        if result is not None:
            assert result["confidence_raw"] <= 0.55
            # Night shift means trough is in daytime — offset should reflect that
            assert isinstance(result["utc_offset_point"], (int, float))

    def test_confidence_never_exceeds_max(self):
        """Even with huge sample, confidence ≤ 0.55."""
        ts = _make_timestamps(500, active_hours=tuple(range(8, 22)))
        freq = compute_frequency(ts)
        result = infer_timezone(freq["hour_histogram"], freq["sample_size"])
        if result is not None:
            assert result["confidence_raw"] <= 0.55


# ========================= Rhythm Breaks ====================================

class TestDetectRhythmBreaks:

    def test_regular_posting_no_breaks(self):
        """Posts every day → no breaks."""
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        ts = [(start + timedelta(days=i)).isoformat() for i in range(60)]
        breaks = detect_rhythm_breaks(ts)
        assert breaks == []

    def test_one_clear_gap(self):
        """Posts daily, then 15-day silence, then daily again."""
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        ts = [(start + timedelta(days=i)).isoformat() for i in range(30)]
        ts += [(start + timedelta(days=45 + i)).isoformat() for i in range(30)]
        breaks = detect_rhythm_breaks(ts)
        assert len(breaks) == 1
        assert breaks[0]["duration_days"] >= 14

    def test_multiple_gaps(self):
        """Two gaps > 7 days each."""
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        ts = [(start + timedelta(days=i)).isoformat() for i in range(10)]
        ts += [(start + timedelta(days=25 + i)).isoformat() for i in range(10)]
        ts += [(start + timedelta(days=50 + i)).isoformat() for i in range(10)]
        breaks = detect_rhythm_breaks(ts)
        assert len(breaks) == 2

    def test_sparse_posting_no_trigger(self):
        """Posts every 5 days (below 7-day threshold) → no breaks."""
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        ts = [(start + timedelta(days=i * 5)).isoformat() for i in range(20)]
        breaks = detect_rhythm_breaks(ts, threshold_days=7)
        assert breaks == []


# ======================== Velocity Spikes ===================================

class TestDetectVelocitySpikes:

    def test_flat_baseline_no_spikes(self):
        """Steady posting rate → no spikes."""
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        ts = [(start + timedelta(hours=i * 12)).isoformat() for i in range(200)]
        spikes = detect_velocity_spikes(ts)
        assert spikes == []

    def test_one_spike(self):
        """Steady, then a burst of 10×, then steady."""
        start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
        ts: list[str] = []
        # 90 days of 1 post/day
        for d in range(90):
            ts.append((start + timedelta(days=d, hours=12)).isoformat())
        # 15-day burst: 10 posts/day
        for d in range(90, 105):
            for h in range(10):
                ts.append((start + timedelta(days=d, hours=h)).isoformat())
        # 90 more days of 1 post/day
        for d in range(105, 195):
            ts.append((start + timedelta(days=d, hours=12)).isoformat())
        spikes = detect_velocity_spikes(ts, window_days=30)
        assert len(spikes) >= 1
        assert spikes[0]["posts_in_window"] > spikes[0]["baseline_rate"] * 2

    def test_too_few_posts(self):
        """Fewer than 10 posts → no analysis."""
        ts = ["2025-01-01T12:00:00Z"] * 5
        spikes = detect_velocity_spikes(ts)
        assert spikes == []


# ==================== Cross-Platform Correlation ============================

class TestCrossPlatformCorrelation:

    def test_two_similar_platforms(self):
        """Same posting pattern on two platforms → high overlap."""
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        ts_a = [(start + timedelta(days=i, hours=12)).isoformat() for i in range(60)]
        ts_b = [(start + timedelta(days=i, hours=13)).isoformat() for i in range(60)]
        result = cross_platform_correlation({"twitter": ts_a, "github": ts_b})
        assert result["platforms_compared"] == 2
        assert result["sufficient"] is True
        comp = result["comparisons"][0]
        assert comp["overlap_ratio"] > 0.8

    def test_single_platform_insufficient(self):
        result = cross_platform_correlation({"twitter": ["2025-01-01T12:00:00Z"]})
        assert result["sufficient"] is False


# ====================== TZ Agreement / Conflict =============================

class TestTzAgreement:

    def test_agree_within_tolerance(self):
        tz_a = {"utc_offset_point": 5.0}
        tz_b = {"utc_offset_point": 5.5}
        assert tz_offsets_agree(tz_a, tz_b, tolerance_hours=1.0) is True

    def test_disagree_outside_tolerance(self):
        tz_a = {"utc_offset_point": 5.0}
        tz_b = {"utc_offset_point": -3.0}
        assert tz_offsets_agree(tz_a, tz_b) is False

    def test_conflict_detected(self):
        tz_a = {"utc_offset_point": 5.0}
        tz_b = {"utc_offset_point": -5.0}
        assert tz_offsets_conflict(tz_a, tz_b, gap_hours=3.0) is True

    def test_no_conflict_close(self):
        tz_a = {"utc_offset_point": 5.0}
        tz_b = {"utc_offset_point": 6.0}
        assert tz_offsets_conflict(tz_a, tz_b, gap_hours=3.0) is False


# ================== Posting Rhythm Similarity ===============================

class TestPostingRhythmSimilar:

    def test_similar_histograms(self):
        hist_a = [0, 0, 0, 0, 0, 0, 0, 5, 8, 10, 8, 7, 6, 5, 6, 7, 8, 9, 7, 5, 3, 1, 0, 0]
        hist_b = [0, 0, 0, 0, 0, 0, 1, 6, 9, 11, 9, 8, 7, 6, 7, 8, 9, 10, 8, 6, 4, 2, 0, 0]
        sim = posting_rhythm_similar(hist_a, hist_b, min_posts_each=50)
        assert sim is not None
        assert sim > 0.9

    def test_insufficient_posts(self):
        hist_a = [0] * 24
        hist_a[12] = 10
        hist_b = [0] * 24
        hist_b[12] = 10
        sim = posting_rhythm_similar(hist_a, hist_b, min_posts_each=50)
        assert sim is None

    def test_dissimilar_histograms(self):
        hist_a = [0] * 12 + [10] * 12  # active afternoon/evening
        hist_b = [10] * 12 + [0] * 12  # active morning
        # Ensure enough total posts
        hist_a = [x * 5 for x in hist_a]
        hist_b = [x * 5 for x in hist_b]
        sim = posting_rhythm_similar(hist_a, hist_b, min_posts_each=50, threshold=0.75)
        # Orthogonal patterns → sim < 0.75 → returns None
        assert sim is None
