#!/usr/bin/env python3
"""Decision-layer helpers: bet sizing, dividend settlement and selective-use slicing.

Standard library only. These helpers never estimate probabilities; they only turn
frozen forecasts plus official payouts into money and coverage statements.
"""
from __future__ import annotations

import math

from racing_model import summarize_rows


def kelly_fraction(probability: float, odds: float, cap: float) -> float:
    """Fractional Kelly stake as a share of bankroll, capped and floored at zero."""
    if not isinstance(odds, (int, float)) or odds <= 1.0 or cap <= 0:
        return 0.0
    edge = float(probability) * float(odds) - 1.0
    if edge <= 0.0:
        return 0.0
    return max(0.0, min(float(cap), edge / (float(odds) - 1.0)))


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


def settle_stream(
    stream,
    *,
    stake_mode: str,
    cap: float,
    bankroll: float,
    min_edge: float,
) -> dict:
    """Settle a walk-forward stream of (record, prediction) pairs.

    ``stake_mode`` is ``flat`` (fixed fraction of bankroll) or ``kelly``
    (fractional Kelly using the model probability against the taken odds).
    """
    bank = float(bankroll)
    start = bank
    peak = bank
    max_drawdown = 0.0
    turnover = 0.0
    profit = 0.0
    wins = 0
    bets: list[dict] = []
    for record, prediction in stream:
        winners, dividends = winner_and_dividends(record["result"])
        best = None
        for item in prediction["runners"]:
            odds = item.get("win_odds_snapshot")
            if not isinstance(odds, (int, float)) or odds <= 1.0:
                continue
            edge = float(item["win_probability"]) * float(odds) - 1.0
            if best is None or edge > best["edge"]:
                best = {"runner": item, "edge": edge, "odds": float(odds)}
        if best is None or best["edge"] <= min_edge or bank <= 0:
            continue
        if stake_mode == "kelly":
            fraction = kelly_fraction(best["runner"]["win_probability"], best["odds"], cap)
        else:
            fraction = min(cap, 1.0)
        stake = bank * fraction
        if stake <= 0:
            continue
        number = int(best["runner"]["horse_no"])
        won = number in winners
        odds_paid = payout_odds(dividends.get(number), best["odds"])
        returned = stake * odds_paid if won else 0.0
        bank += returned - stake
        turnover += stake
        profit += returned - stake
        wins += 1 if won else 0
        peak = max(peak, bank)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - bank) / peak)
        bets.append(
            {
                "race_id": prediction["race_id"],
                "horse_no": number,
                "probability": round(float(best["runner"]["win_probability"]), 6),
                "odds": round(best["odds"], 4),
                "edge": round(best["edge"], 6),
                "stake": round(stake, 4),
                "won": won,
                "returned": round(returned, 4),
            }
        )
    return {
        "stake_mode": stake_mode,
        "cap": cap,
        "min_edge": min_edge,
        "bankroll_start": round(start, 4),
        "bankroll_end": round(bank, 4),
        "bets": len(bets),
        "turnover": round(turnover, 4),
        "profit": round(profit, 4),
        "roi": round(profit / turnover, 6) if turnover else None,
        "strike_rate": round(wins / len(bets), 6) if bets else None,
        "max_drawdown": round(max_drawdown, 6),
        "policy": (
            "Kelly under HK takeout loses on average; positive ROI here needs evidence, "
            "not intent. Dividends are settled per HK$10 when present."
        ),
        "detail": bets[:200],
    }


def coverage_slices(points: list[tuple[float, dict]], percentages: list[int]) -> list[dict]:
    """Split races by forecast confidence and summarise each coverage band.

    ``points`` is a list of ``(confidence, metric_row)``; higher confidence first.
    """
    ordered = sorted(points, key=lambda item: item[0], reverse=True)
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
