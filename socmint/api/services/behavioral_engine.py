"""MODULE 7.10 — Behavioral Engine (Social Depth Module).

Pure-core service: no DB/network in the hot path.  Input is plain dicts
(lists of post timestamps, platform labels); output is plain dicts (frequency
distributions, timezone inferences, rhythm breaks, velocity spikes, cross-
platform rhythm).  Fully unit-testable with DTO fixtures.

Confidence rules for timezone inference:
  - Minimum SDM_MIN_POSTS_FOR_TZ_INFERENCE posts (default 30)
  - Trough must be ≥3× quieter than peak
  - Maximum confidence_raw = 0.55 regardless of sample size
  - All inferred values tagged [behavioral-inferred]
"""
from __future__ import annotations

import math
import os
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Tuning constants (env-overridable)
# ---------------------------------------------------------------------------
_MIN_POSTS_TZ = int(os.environ.get("SDM_MIN_POSTS_FOR_TZ_INFERENCE", "30"))
_MIN_POSTS_RHYTHM = int(os.environ.get("SDM_MIN_POSTS_FOR_RHYTHM_SIMILARITY", "50"))
_SILENCE_DAYS = int(os.environ.get("SDM_SILENCE_THRESHOLD_DAYS", "7"))
_TZ_MAX_CONFIDENCE = 0.55
_BEHAVIORAL_MAX_CONFIDENCE = 0.40
_TROUGH_RATIO = 3.0          # trough must be ≥3× quieter than peak


def _parse_ts(raw) -> Optional[datetime]:
    """Best-effort parse of an ISO 8601 timestamp string or datetime."""
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    try:
        s = str(raw).strip()
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 3b — Posting Frequency Distribution
# ---------------------------------------------------------------------------
def compute_frequency(timestamps: list) -> dict:
    """Compute posting frequency statistics from a list of ISO timestamps.

    Returns a dict with: posts_per_day (mean/median/std), hour_histogram (0-23),
    dow_histogram (0-6), inter_post_intervals (mean/median/min/max/std in
    minutes), activity_density (active_days / observed_days).
    """
    dts = sorted(t for t in (_parse_ts(ts) for ts in timestamps) if t)
    if len(dts) < 2:
        return {"sample_size": len(dts), "sufficient": False}

    # --- hour-of-day histogram (UTC) ---
    hour_counts: list[int] = [0] * 24
    dow_counts: list[int] = [0] * 7
    for dt in dts:
        hour_counts[dt.hour] += 1
        dow_counts[dt.weekday()] += 1

    # --- posts per day ---
    day_counts: Counter[str] = Counter()
    for dt in dts:
        day_counts[dt.strftime("%Y-%m-%d")] += 1
    ppd_values = list(day_counts.values())
    ppd_mean = sum(ppd_values) / len(ppd_values)
    ppd_sorted = sorted(ppd_values)
    ppd_median = ppd_sorted[len(ppd_sorted) // 2]
    ppd_var = sum((v - ppd_mean) ** 2 for v in ppd_values) / max(1, len(ppd_values))
    ppd_std = math.sqrt(ppd_var)

    # --- inter-post intervals (minutes) ---
    intervals = [(dts[i + 1] - dts[i]).total_seconds() / 60.0 for i in range(len(dts) - 1)]
    int_mean = sum(intervals) / len(intervals)
    int_sorted = sorted(intervals)
    int_median = int_sorted[len(int_sorted) // 2]
    int_min = int_sorted[0]
    int_max = int_sorted[-1]
    int_var = sum((v - int_mean) ** 2 for v in intervals) / max(1, len(intervals))
    int_std = math.sqrt(int_var)

    # --- activity density ---
    span = (dts[-1] - dts[0]).days + 1
    active_days = len(day_counts)

    return {
        "sample_size": len(dts),
        "sufficient": True,
        "posts_per_day": {
            "mean": round(ppd_mean, 2),
            "median": ppd_median,
            "std": round(ppd_std, 2),
        },
        "hour_histogram": hour_counts,
        "dow_histogram": dow_counts,
        "inter_post_intervals": {
            "mean_minutes": round(int_mean, 2),
            "median_minutes": round(int_median, 2),
            "min_minutes": round(int_min, 2),
            "max_minutes": round(int_max, 2),
            "std_minutes": round(int_std, 2),
        },
        "activity_density": round(active_days / max(1, span), 4),
        "active_days": active_days,
        "observed_days": span,
    }


# ---------------------------------------------------------------------------
# 3c — Timezone Inference
# ---------------------------------------------------------------------------
def infer_timezone(hour_histogram: list[int], sample_size: int) -> Optional[dict]:
    """Infer UTC offset from posting hour-of-day histogram.

    Method: find the 6-to-8-hour window with lowest activity (sleep trough).
    The centre of the trough shifted by expected mid-sleep hour (02:00 local)
    gives the estimated UTC offset.

    Returns None if insufficient data, ambiguous trough, or below threshold.
    """
    if sample_size < _MIN_POSTS_TZ:
        return None
    if len(hour_histogram) != 24:
        return None
    total = sum(hour_histogram)
    if total < _MIN_POSTS_TZ:
        return None

    # Try window sizes 6, 7, 8 — pick the one with the deepest trough
    best_trough = None
    for window in (6, 7, 8):
        for start in range(24):
            trough_sum = sum(hour_histogram[(start + i) % 24] for i in range(window))
            if best_trough is None or trough_sum < best_trough["sum"]:
                best_trough = {
                    "start": start,
                    "window": window,
                    "sum": trough_sum,
                }

    if best_trough is None:
        return None

    # Check trough clarity: trough window must be ≥3× quieter than peak window
    trough_rate = best_trough["sum"] / best_trough["window"]
    # Peak = the complementary window
    peak_hours = 24 - best_trough["window"]
    peak_sum = total - best_trough["sum"]
    peak_rate = peak_sum / max(1, peak_hours)

    if trough_rate <= 0:
        ratio = float("inf")
    else:
        ratio = peak_rate / trough_rate

    if ratio < _TROUGH_RATIO:
        return None  # Ambiguous — trough not clear enough

    # Centre of trough (in UTC hours)
    trough_centre = (best_trough["start"] + best_trough["window"] / 2.0) % 24

    # Expected mid-sleep is 02:00 local → local_hour = utc_hour + offset
    # trough_centre (UTC) maps to 02:00 local → offset = 2 - trough_centre
    raw_offset = 2.0 - trough_centre
    # Normalise to [-12, +14] range
    if raw_offset < -12:
        raw_offset += 24
    elif raw_offset > 14:
        raw_offset -= 24

    # Report as a range (±0.5h uncertainty)
    offset_lo = raw_offset - 0.5
    offset_hi = raw_offset + 0.5

    # Confidence scales with sample size and trough clarity
    size_factor = min(1.0, sample_size / 200.0)
    clarity_factor = min(1.0, ratio / 10.0)
    confidence = round(min(_TZ_MAX_CONFIDENCE, 0.25 + 0.30 * size_factor * clarity_factor), 4)

    trough_hours = [(best_trough["start"] + i) % 24 for i in range(best_trough["window"])]

    return {
        "utc_offset_range": [round(offset_lo, 1), round(offset_hi, 1)],
        "utc_offset_point": round(raw_offset, 1),
        "confidence_raw": confidence,
        "trough_hours": trough_hours,
        "trough_ratio": round(ratio, 2),
        "sample_size": sample_size,
        "source": "behavioral_inferred",
        "tag": "[behavioral-inferred]",
    }


# ---------------------------------------------------------------------------
# 3d — Activity Rhythm Anomalies (Rhythm Breaks)
# ---------------------------------------------------------------------------
def detect_rhythm_breaks(
    timestamps: list,
    threshold_days: Optional[int] = None,
) -> list[dict]:
    """Detect significant gaps in posting activity.

    A silence of more than ``threshold_days`` in an otherwise active account
    is a rhythm break.  Returns a list of ``{start_date, end_date,
    duration_days}``.
    """
    threshold = threshold_days if threshold_days is not None else _SILENCE_DAYS
    dts = sorted(t for t in (_parse_ts(ts) for ts in timestamps) if t)
    if len(dts) < 2:
        return []

    breaks: list[dict] = []
    for i in range(len(dts) - 1):
        gap = (dts[i + 1] - dts[i]).days
        if gap >= threshold:
            breaks.append({
                "start_date": dts[i].strftime("%Y-%m-%d"),
                "end_date": dts[i + 1].strftime("%Y-%m-%d"),
                "duration_days": gap,
            })
    return breaks


# ---------------------------------------------------------------------------
# 3e — Content Velocity Analysis
# ---------------------------------------------------------------------------
def detect_velocity_spikes(
    timestamps: list,
    window_days: int = 30,
) -> list[dict]:
    """Identify periods where posting rate is >2 std above baseline.

    Uses a rolling window of ``window_days``.  Returns a list of
    ``{start_date, end_date, posts_in_window, baseline_rate}``.
    """
    dts = sorted(t for t in (_parse_ts(ts) for ts in timestamps) if t)
    if len(dts) < 10:
        return []

    first_day = dts[0].date()
    last_day = dts[-1].date()
    span = (last_day - first_day).days + 1
    if span < window_days * 2:
        return []  # Need at least 2× window to establish baseline

    # Build daily count array
    daily: dict[int, int] = {}
    for dt in dts:
        day_idx = (dt.date() - first_day).days
        daily[day_idx] = daily.get(day_idx, 0) + 1

    # Rolling window rates
    rates: list[tuple[int, int]] = []  # (start_day_idx, count_in_window)
    for start in range(span - window_days + 1):
        count = sum(daily.get(start + d, 0) for d in range(window_days))
        rates.append((start, count))

    if not rates:
        return []

    values = [r[1] for r in rates]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / max(1, len(values))
    std = math.sqrt(variance)
    threshold = mean + 2 * std

    if std < 1:
        return []  # Flat baseline — no meaningful spikes

    spikes: list[dict] = []
    in_spike = False
    spike_start = 0

    for start_idx, count in rates:
        if count > threshold:
            if not in_spike:
                in_spike = True
                spike_start = start_idx
        else:
            if in_spike:
                spike_end = start_idx + window_days - 1
                spike_count = sum(
                    daily.get(d, 0) for d in range(spike_start, spike_end + 1)
                )
                spikes.append({
                    "start_date": (first_day + timedelta(days=spike_start)).isoformat(),
                    "end_date": (first_day + timedelta(days=spike_end)).isoformat(),
                    "posts_in_window": spike_count,
                    "baseline_rate": round(mean, 2),
                })
                in_spike = False

    # Close any open spike
    if in_spike:
        spike_end = rates[-1][0] + window_days - 1
        spike_count = sum(
            daily.get(d, 0)
            for d in range(spike_start, min(spike_end + 1, span))
        )
        spikes.append({
            "start_date": (first_day + timedelta(days=spike_start)).isoformat(),
            "end_date": (first_day + timedelta(days=min(spike_end, span - 1))).isoformat(),
            "posts_in_window": spike_count,
            "baseline_rate": round(mean, 2),
        })

    return spikes


# ---------------------------------------------------------------------------
# 3f — Cross-Platform Activity Correlation
# ---------------------------------------------------------------------------
def cross_platform_correlation(
    platform_timelines: dict[str, list],
) -> dict:
    """Compare activity timelines across confirmed platforms.

    ``platform_timelines`` maps platform name → list of ISO timestamp strings.
    Returns compartmentalization indicators and lead/lag analysis.
    """
    parsed: dict[str, list[datetime]] = {}
    for plat, ts_list in platform_timelines.items():
        dts = sorted(t for t in (_parse_ts(ts) for ts in ts_list) if t)
        if dts:
            parsed[plat] = dts

    if len(parsed) < 2:
        return {"platforms_compared": 0, "sufficient": False}

    platforms = sorted(parsed.keys())
    result: dict = {
        "platforms_compared": len(platforms),
        "sufficient": True,
        "comparisons": [],
    }

    for i in range(len(platforms)):
        for j in range(i + 1, len(platforms)):
            pa, pb = platforms[i], platforms[j]
            comp = _compare_two_platforms(pa, parsed[pa], pb, parsed[pb])
            result["comparisons"].append(comp)

    return result


def _compare_two_platforms(
    name_a: str, dts_a: list[datetime],
    name_b: str, dts_b: list[datetime],
) -> dict:
    """Compare two platform timelines for compartmentalization / lead-lag."""
    # Build daily activity sets
    days_a = {dt.date() for dt in dts_a}
    days_b = {dt.date() for dt in dts_b}
    all_days = days_a | days_b
    if not all_days:
        return {"platform_a": name_a, "platform_b": name_b, "overlap": 0}

    both = days_a & days_b
    only_a = days_a - days_b
    only_b = days_b - days_a

    # Hour-of-day histograms for cosine similarity
    hist_a = [0] * 24
    hist_b = [0] * 24
    for dt in dts_a:
        hist_a[dt.hour] += 1
    for dt in dts_b:
        hist_b[dt.hour] += 1

    cosine = _cosine_similarity(hist_a, hist_b)

    return {
        "platform_a": name_a,
        "platform_b": name_b,
        "shared_active_days": len(both),
        "only_a_days": len(only_a),
        "only_b_days": len(only_b),
        "overlap_ratio": round(len(both) / max(1, len(all_days)), 4),
        "hour_cosine_similarity": round(cosine, 4) if cosine is not None else None,
        "compartmentalization": len(only_a) > 0.5 * len(days_a) and len(only_b) > 0.5 * len(days_b),
    }


def _cosine_similarity(a: list[int], b: list[int]) -> Optional[float]:
    """Cosine similarity between two numeric vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Convenience: check if two TZ inferences agree / conflict
# ---------------------------------------------------------------------------
def tz_offsets_agree(tz_a: dict, tz_b: dict, tolerance_hours: float = 1.0) -> bool:
    """True when two TZ inferences agree within ±tolerance_hours."""
    pa = tz_a.get("utc_offset_point")
    pb = tz_b.get("utc_offset_point")
    if pa is None or pb is None:
        return False
    return abs(float(pa) - float(pb)) <= tolerance_hours


def tz_offsets_conflict(tz_a: dict, tz_b: dict, gap_hours: float = 3.0) -> bool:
    """True when two TZ inferences disagree by more than gap_hours."""
    pa = tz_a.get("utc_offset_point")
    pb = tz_b.get("utc_offset_point")
    if pa is None or pb is None:
        return False
    return abs(float(pa) - float(pb)) > gap_hours


def posting_rhythm_similar(
    hist_a: list[int],
    hist_b: list[int],
    min_posts_each: int = 50,
    threshold: float = 0.75,
) -> Optional[float]:
    """Return cosine similarity of two hour histograms, or None if insufficient.

    Only fires when both platforms have at least ``min_posts_each`` posts.
    """
    if len(hist_a) != 24 or len(hist_b) != 24:
        return None
    if sum(hist_a) < min_posts_each or sum(hist_b) < min_posts_each:
        return None
    sim = _cosine_similarity(hist_a, hist_b)
    if sim is None:
        return None
    return sim if sim >= threshold else None
