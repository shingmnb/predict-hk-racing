"""Staking layer for the racing lab.

Literature
----------
Benter (1994), *Computer Based Horse Race Handicapping and Wagering Systems*:
bet only when the estimated edge is positive, size with **fractional Kelly**
(1/2 to 1/3 of full Kelly) because edges are routinely over-estimated by up to
2x and full Kelly drawdowns commonly exceed 50%, and cap the stake by pool
size because a large pari-mutuel bet lowers its own dividend.

All functions are pure (no I/O) and use decimal odds, not fractional odds.

Formulas
--------
``edge  = p * odds - 1``
``f*    = (p * odds - 1) / (odds - 1)``   (full Kelly fraction of bankroll)
``stake = bankroll * k * f*``              (``k`` = fractional-Kelly multiplier)
"""

from __future__ import annotations

import math

EPS = 1e-12


def _valid(probability: float, decimal_odds: float) -> bool:
    return (
        isinstance(probability, (int, float))
        and isinstance(decimal_odds, (int, float))
        and math.isfinite(float(probability))
        and math.isfinite(float(decimal_odds))
        and 0.0 < float(probability) < 1.0
        and float(decimal_odds) > 1.0
    )


def edge(probability: float, decimal_odds: float) -> float | None:
    """Expected profit per unit staked: ``p * odds - 1``.

    Returns ``None`` for invalid inputs (probability outside (0, 1) or odds
    at/below 1.0) instead of raising, so batch callers can skip cleanly.
    """
    if not _valid(probability, decimal_odds):
        return None
    return float(probability) * float(decimal_odds) - 1.0


def kelly_fraction(probability: float, decimal_odds: float) -> float:
    """Full-Kelly fraction of bankroll, clamped to ``[0, 1]``.

    Zero when there is no edge. This is the *full* Kelly stake; callers should
    almost always apply a fractional multiplier (Benter: 1/2 to 1/3).
    """
    if not _valid(probability, decimal_odds):
        return 0.0
    b = float(decimal_odds) - 1.0
    fraction = (float(probability) * float(decimal_odds) - 1.0) / b
    return max(0.0, min(1.0, fraction))


def stake_size(
    probability: float,
    decimal_odds: float,
    bankroll: float,
    *,
    kelly_multiplier: float = 0.25,
    min_edge: float = 0.02,
    max_bankroll_fraction: float = 0.02,
    pool_size: float | None = None,
    max_pool_fraction: float = 0.001,
    unit: float = 10.0,
    min_stake: float | None = None,
) -> dict:
    """Size one bet.

    Parameters
    ----------
    kelly_multiplier
        Fraction of full Kelly to apply. Default 0.25 (between Benter's 1/3
        and 1/2, conservative because the probability is itself estimated).
    min_edge
        Skip the bet when ``edge < min_edge`` (default 2%).
    max_bankroll_fraction
        Hard per-bet cap as a fraction of bankroll (default 2%).
    pool_size / max_pool_fraction
        Optional pari-mutuel impact cap: ``stake <= pool_size * max_pool_fraction``.
    unit / min_stake
        Stake is floored to ``unit`` (default HK$10); bets below ``min_stake``
        (default one unit) are skipped.

    Returns
    -------
    dict with ``status`` (``"bet"`` or a skip reason), ``stake``, ``edge``,
    ``full_kelly``, ``applied_fraction`` and the caps that bound the stake.
    """
    result = {
        "status": "skip",
        "reason": None,
        "stake": 0.0,
        "edge": None,
        "full_kelly": 0.0,
        "applied_fraction": 0.0,
        "caps": {},
    }
    if not _valid(probability, decimal_odds):
        result["reason"] = "invalid_input"
        return result
    if not isinstance(bankroll, (int, float)) or not math.isfinite(float(bankroll)) or bankroll <= 0:
        result["reason"] = "invalid_bankroll"
        return result
    if kelly_multiplier < 0 or max_bankroll_fraction < 0:
        result["reason"] = "invalid_parameters"
        return result

    estimated_edge = edge(probability, decimal_odds)
    result["edge"] = estimated_edge
    if estimated_edge < min_edge:
        result["reason"] = "edge_below_threshold"
        return result

    full_kelly = kelly_fraction(probability, decimal_odds)
    result["full_kelly"] = full_kelly
    applied = min(full_kelly * kelly_multiplier, max_bankroll_fraction)
    result["applied_fraction"] = applied
    raw = float(bankroll) * applied
    result["caps"]["kelly"] = raw

    if pool_size is not None and max_pool_fraction > 0:
        if not isinstance(pool_size, (int, float)) or not math.isfinite(float(pool_size)) or pool_size <= 0:
            result["reason"] = "invalid_pool_size"
            return result
        pool_cap = float(pool_size) * max_pool_fraction
        result["caps"]["pool"] = pool_cap
        raw = min(raw, pool_cap)

    raw = min(raw, float(bankroll))
    step = unit if isinstance(unit, (int, float)) and unit > 0 else 0.0
    stake = math.floor(raw / step) * step if step else raw
    threshold = step if min_stake is None else float(min_stake)
    if stake < threshold or stake <= 0:
        result["reason"] = "stake_below_minimum"
        return result

    result["status"] = "bet"
    result["stake"] = stake
    return result


def plan_race(
    bets: list[dict],
    bankroll: float,
    *,
    max_race_fraction: float = 0.05,
    **kwargs,
) -> dict:
    """Size several bets in one race with a total-exposure cap.

    Each item in ``bets`` must provide ``probability`` and ``decimal_odds``
    (plus any optional bookkeeping fields, which are copied through). Because
    bets in the same race are correlated, the sum is capped at
    ``max_race_fraction * bankroll`` and scaled down proportionally before
    rounding to the stake unit.
    """
    sized = []
    for bet in bets:
        outcome = stake_size(
            bet.get("probability"),
            bet.get("decimal_odds"),
            bankroll,
            **kwargs,
        )
        merged = dict(bet)
        merged.update(outcome)
        sized.append(merged)

    total = sum(item["stake"] for item in sized)
    cap = float(bankroll) * max(0.0, min(1.0, max_race_fraction))
    scaled = False
    if total > cap and total > 0:
        scale = cap / total
        step = kwargs.get("unit", 10.0)
        step = step if isinstance(step, (int, float)) and step > 0 else 0.0
        minimum = kwargs.get("min_stake")
        minimum = step if minimum is None else float(minimum)
        for item in sized:
            if item["status"] != "bet":
                continue
            raw = item["stake"] * scale
            value = math.floor(raw / step) * step if step else raw
            if value < minimum:
                item["status"] = "skip"
                item["reason"] = "scaled_below_minimum"
                item["stake"] = 0.0
            else:
                item["stake"] = value
        total = sum(item["stake"] for item in sized)
        scaled = True

    return {
        "bankroll": float(bankroll),
        "bet_count": sum(1 for item in sized if item["status"] == "bet"),
        "total_stake": total,
        "total_exposure": round(total / float(bankroll), 4) if bankroll else None,
        "race_cap": cap,
        "scaled_to_race_cap": scaled,
        "bets": sized,
    }
