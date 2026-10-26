#!/usr/bin/env python3
"""Calibration drift monitoring: does a fitted calibrator still hold?

A calibrator is fitted once on a holdout slice. Months later the relationship
between raw scores and outcomes may have moved. This module turns stored
predictions (probabilities + outcomes) into per-period ECE and flags periods
whose calibration error has drifted away from the reference.
"""
from __future__ import annotations

from racing_model import expected_calibration_error


def period_of(meeting_date: str, *, mode: str = "month") -> str:
    """Bucket key: ``YYYY-MM`` by month, or ``YYYY-MM-DD`` for a single meeting."""
    if mode == "meeting":
        return meeting_date
    return meeting_date[:7]


def summarize_periods(
    points_by_period: dict[str, list[tuple[float, int]]],
    *,
    baseline_ece: float | None,
    min_samples: int,
    threshold: float,
) -> dict:
    """ECE per period, delta against the baseline, and a drift flag.

    A period drifts when it has enough samples and its ECE exceeds
    ``baseline_ece + threshold``. Periods below ``min_samples`` are reported but
    never flagged, because ECE is too noisy there.
    """
    periods = []
    drifted = []
    for key in sorted(points_by_period):
        points = points_by_period[key]
        if not points:
            continue
        ece = expected_calibration_error(points)
        delta = None if baseline_ece is None else round(ece - baseline_ece, 6)
        reliable = len(points) >= min_samples
        is_drift = bool(
            reliable and delta is not None and delta > threshold
        )
        periods.append(
            {
                "period": key,
                "samples": len(points),
                "ece": round(ece, 6),
                "delta_vs_baseline": delta,
                "reliable": reliable,
                "drift": is_drift,
            }
        )
        if is_drift:
            drifted.append(key)
    return {
        "baseline_ece": None if baseline_ece is None else round(baseline_ece, 6),
        "min_samples": min_samples,
        "threshold": threshold,
        "periods": periods,
        "drifted_periods": drifted,
        "drift_detected": bool(drifted),
        "note": (
            "ECE 只反映校準，不反映區分能力；樣本不足的期間只作參考。"
        ),
    }
