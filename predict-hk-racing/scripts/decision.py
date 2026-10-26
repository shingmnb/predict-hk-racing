#!/usr/bin/env python3
"""Decision-layer helpers: bet sizing, dividend settlement and selective-use slicing.

Standard library only. These helpers never estimate probabilities; they only turn
frozen forecasts plus official payouts into money and coverage statements.
"""
from __future__ import annotations

import math

from racing_model import summarize_rows
from staking import plan_race, stake_size


def payout_odds(win_dividend: object, fallback_odds: float) -> float:
    """HK dividends are quoted per HK$10; fall back to the taken odds when absent."""
    if (
        isinstance(win_dividend, (int, float))
        and not isinstance(win_dividend, bool)
        and float(win_dividend) > 0.0
    ):
        return float(win_dividend) / 10.0
    return float(fallback_odds)


def winner_and_dividends(result: dict) -> tuple[set[int], dict[int, object]]:
    winners: set[int] = set()
    dividends: dict[int, object] = {}
    for row in result.get("results", []):
        try:
            number = int(row["horse_no"])
        except (KeyError, TypeError, ValueError):
            continue
        if row.get("finish_position") == 1:
            winners.add(number)
        dividends[number] = row.get("win_dividend")
    return winners, dividends


def profit_concentration(race_results: list[tuple[float, float]]) -> float | None:
    """Share of gross profit contributed by the single best race (concentration risk)."""
    positive = sorted((net for _, net in race_results if net > 0), reverse=True)
    if not positive or sum(positive) <= 0:
        return None
    return round(positive[0] / sum(positive), 6)


def roi_bootstrap(race_results: list[tuple[float, float]], *, samples: int = 1000, seed: int = 20261026) -> list[float | None]:
    """Percentile bootstrap of ROI over races, resampling (stake, net) pairs."""
    import random

    usable = [(stake, net) for stake, net in race_results if stake > 0]
    if len(usable) < 2 or samples <= 0:
        return [None, None]
    rng = random.Random(seed)
    estimates = []
    n = len(usable)
    for _ in range(samples):
        stake_sum = 0.0
        net_sum = 0.0
        for _ in range(n):
            stake, net = usable[rng.randrange(n)]
            stake_sum += stake
            net_sum += net
        if stake_sum > 0:
            estimates.append(net_sum / stake_sum)
    if len(estimates) < 2:
        return [None, None]
    estimates.sort()
    return [round(estimates[int(0.025 * (len(estimates) - 1))], 6),
            round(estimates[int(0.975 * (len(estimates) - 1))], 6)]


def settle_stream(
    stream,
    *,
    stake_mode: str,
    cap: float,
    bankroll: float,
    min_edge: float,
    kelly_multiplier: float = 0.25,
    max_race_fraction: float = 0.05,
    pool_size: float | dict[str, float] | None = None,
    max_pool_fraction: float = 0.001,
    unit: float = 10.0,
) -> dict:
    """Settle a walk-forward stream of (record, prediction) pairs.

    ``flat`` keeps the legacy best-runner-only policy. ``kelly`` delegates all
    sizing to :mod:`staking`, including fractional Kelly, pool and race caps.
    ``pool_size`` may be one explicit backtest assumption or a race-id mapping.
    """
    bank = float(bankroll)
    start = bank
    peak = bank
    max_drawdown = 0.0
    max_single_stake_fraction = 0.0
    race_results: list[tuple[float, float]] = []
    turnover = 0.0
    profit = 0.0
    wins = 0
    bets: list[dict] = []
    for record, prediction in stream:
        if bank <= 0:
            continue
        winners, dividends = winner_and_dividends(record["result"])
        selected = []
        if stake_mode == "kelly":
            candidates = []
            for item in prediction["runners"]:
                odds = item.get("win_odds_snapshot")
                if not isinstance(odds, (int, float)) or odds <= 1.0:
                    continue
                candidates.append({
                    "probability": item.get("win_probability"),
                    "decimal_odds": float(odds),
                    "runner": item,
                })
            if not candidates:
                continue
            race_id = prediction["race_id"]
            race_pool_size = pool_size.get(race_id) if isinstance(pool_size, dict) else pool_size
            options = {
                "kelly_multiplier": kelly_multiplier,
                "min_edge": min_edge,
                "pool_size": race_pool_size,
                "max_pool_fraction": max_pool_fraction,
                "unit": unit,
            }
            if len(candidates) == 1:
                candidate = candidates[0]
                outcome = stake_size(
                    candidate["probability"],
                    candidate["decimal_odds"],
                    bank,
                    max_bankroll_fraction=min(cap, max_race_fraction),
                    **options,
                )
                selected = [{**candidate, **outcome}]
            else:
                selected = plan_race(
                    candidates,
                    bank,
                    max_race_fraction=max_race_fraction,
                    max_bankroll_fraction=cap,
                    **options,
                )["bets"]
        else:
            best = None
            for item in prediction["runners"]:
                odds = item.get("win_odds_snapshot")
                if not isinstance(odds, (int, float)) or odds <= 1.0:
                    continue
                edge = float(item["win_probability"]) * float(odds) - 1.0
                if best is None or edge > best["edge"]:
                    best = {"runner": item, "edge": edge, "odds": float(odds)}
            if best is None or best["edge"] <= min_edge:
                continue
            stake = bank * min(cap, 1.0)
            if stake <= 0:
                continue
            selected = [{
                "status": "bet",
                "runner": best["runner"],
                "decimal_odds": best["odds"],
                "edge": best["edge"],
                "stake": stake,
            }]

        race_stake = 0.0
        race_returned = 0.0
        race_wins = 0
        race_bets = []
        for item in selected:
            if item.get("status") != "bet":
                continue
            stake = float(item["stake"])
            if stake <= 0:
                continue
            runner = item["runner"]
            number = int(runner["horse_no"])
            won = number in winners
            odds_paid = payout_odds(dividends.get(number), item["decimal_odds"])
            returned = stake * odds_paid if won else 0.0
            race_stake += stake
            race_returned += returned
            race_wins += 1 if won else 0
            race_bets.append({
                "race_id": prediction["race_id"],
                "horse_no": number,
                "probability": round(float(runner["win_probability"]), 6),
                "odds": round(float(item["decimal_odds"]), 4),
                "edge": round(float(item["edge"]), 6),
                "stake": stake,
                "won": won,
                "returned": round(returned, 4),
            })
        if not race_bets:
            continue
        bank_before = bank
        if bank_before > 0:
            max_single_stake_fraction = max(
                max_single_stake_fraction, race_stake / bank_before
            )
        race_results.append((race_stake, race_returned - race_stake))
        bank += race_returned - race_stake
        turnover += race_stake
        profit += race_returned - race_stake
        wins += race_wins
        peak = max(peak, bank)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - bank) / peak)
        bets.extend(race_bets)
    return {
        "stake_mode": stake_mode,
        "cap": cap,
        "min_edge": min_edge,
        "kelly_multiplier": kelly_multiplier,
        "max_race_fraction": max_race_fraction,
        "max_pool_fraction": max_pool_fraction,
        "bankroll_start": round(start, 4),
        "bankroll_end": round(bank, 4),
        "bets": len(bets),
        "turnover": round(turnover, 4),
        "profit": round(profit, 4),
        "roi": round(profit / turnover, 6) if turnover else None,
        "strike_rate": round(wins / len(bets), 6) if bets else None,
        "max_drawdown": round(max_drawdown, 6),
        "max_single_stake_fraction": round(max_single_stake_fraction, 6),
        "roi_ci95": roi_bootstrap(race_results),
        "profit_concentration": profit_concentration(race_results),
        "bootstrap_samples": 1000,
        "policy": (
            "Kelly stakes use the shared fractional-Kelly staking layer with per-bet, "
            "pool and race caps. Frozen probabilities and taken odds are used; ROI is "
            "an empirical result, not a guarantee. Dividends are settled per HK$10."
        ),
        "detail": bets[:200],
    }


def coverage_slices(points: list[tuple[float, dict]], percentages: list[int]) -> list[dict]:
    """Split races by forecast confidence and summarise each coverage band.

    ``points`` is a list of ``(confidence, metric_row)``; higher confidence first.
    """
    ordered = sorted(points, key=lambda item: item[0], reverse=True)
    if not ordered:
        return []
    total = len(ordered)
    out: list[dict] = []
    for pct in percentages:
        if not isinstance(pct, int) or pct <= 0 or pct > 100:
            continue
        take = max(1, math.ceil(total * pct / 100))
        if take > total:
            take = total
        subset = [row for _, row in ordered[:take]]
        summary = summarize_rows(subset)
        summary.pop("calibration_bins", None)
        summary["coverage_pct"] = pct
        summary["mean_confidence"] = round(
            sum(conf for conf, _ in ordered[:take]) / take, 6
        )
        out.append(summary)
    return out
