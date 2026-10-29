#!/usr/bin/env python3
"""Validate and backtest selective HKJC 10+ longshot intelligence decisions.

Only Python's standard library plus the sibling domain helpers is required.
A race card supplies the whole-field pre-race WIN snapshot. A decision file
contains the market-blind fundamental run and a review for every eligible horse.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from domain import derive_place_slots, quote_supported

MIN_WIN_ODDS = 10.0
RULE_SCHEMA = "longshot-decision-v1"
SNAPSHOT_POLICY = "closest-to-T-2h-within-T-3h-T-2h"


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_time(value: Any, field: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed
    except (TypeError, ValueError):
        errors.append(f"{field}: must be timezone-aware ISO-8601")
        return None


def pct(numerator: float, denominator: float) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def active_runners(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in card.get("runners", []) if not r.get("scratched", False)]


def eligible_horses(card: dict[str, Any]) -> dict[int, float]:
    eligible = {}
    for runner in active_runners(card):
        odds = runner.get("win_odds")
        if isinstance(odds, (int, float)) and not isinstance(odds, bool) and odds >= MIN_WIN_ODDS:
            eligible[int(runner["horse_no"])] = float(odds)
    return eligible


def place_slots(card: dict[str, Any]) -> int:
    race = card.get("race", {})
    declared = int(race.get("declared_starters") or len(active_runners(card)))
    return int(race.get("place_slots") or derive_place_slots(declared))


def validate_decision(card: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    race = card.get("race", {})
    race_id = race.get("race_id")
    if decision.get("schema_version") != RULE_SCHEMA:
        errors.append(f"schema_version must be {RULE_SCHEMA}")
    if decision.get("race_id") != race_id:
        errors.append("decision.race_id must match card.race.race_id")
    if decision.get("snapshot_policy") != SNAPSHOT_POLICY:
        errors.append(f"snapshot_policy must be {SNAPSHOT_POLICY}")
    if not str(decision.get("rule_version", "")).strip():
        errors.append("rule_version is required")
    if not str(decision.get("fundamental_run_id", "")).strip():
        errors.append("fundamental_run_id is required")

    off = parse_time(race.get("off_time"), "card.race.off_time", errors)
    captured = parse_time(card.get("captured_at"), "card.captured_at", errors)
    created = parse_time(decision.get("created_at"), "decision.created_at", errors)
    fundamental = parse_time(decision.get("fundamental_created_at"), "decision.fundamental_created_at", errors)
    data_cutoff = parse_time(card.get("data_cutoff"), "card.data_cutoff", errors)

    in_window = False
    if off and captured:
        in_window = off - timedelta(hours=3) <= captured <= off - timedelta(hours=2)
        if not in_window:
            warnings.append("market snapshot is outside T-3h to T-2h; only abstain is allowed")
    if off and created and created >= off:
        errors.append("decision.created_at must be before off_time")
    if captured and created and created < captured:
        errors.append("decision.created_at cannot precede the market snapshot")
    if captured and fundamental and fundamental > captured:
        errors.append("fundamental analysis must be frozen before the market snapshot")
    if off and data_cutoff and data_cutoff > off:
        errors.append("card.data_cutoff must not exceed off_time")

    runners = active_runners(card)
    if not runners:
        errors.append("card has no active runners")
    market_complete = bool(runners) and all(
        isinstance(r.get("win_odds"), (int, float))
        and not isinstance(r.get("win_odds"), bool)
        and r.get("win_odds") > 0
        for r in runners
    )
    if not market_complete:
        warnings.append("whole-field WIN snapshot is incomplete; only abstain is allowed")
    market_qualified = in_window and market_complete
    market_blind = decision.get("market_blind") is True
    if not market_blind:
        errors.append("market_blind must be true for a formal longshot decision")

    eligible = eligible_horses(card) if market_qualified else {}
    reviews = decision.get("reviews")
    if not isinstance(reviews, list):
        errors.append("reviews must be a list")
        reviews = []
    reviewed_numbers: list[int] = []
    selected: list[dict[str, Any]] = []
    sources = {
        s.get("source_id"): s for s in decision.get("sources", [])
        if isinstance(s, dict) and s.get("source_id")
    }

    for index, review in enumerate(reviews):
        prefix = f"reviews[{index}]"
        if not isinstance(review, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        try:
            horse_no = int(review.get("horse_no"))
            reviewed_numbers.append(horse_no)
        except (TypeError, ValueError):
            errors.append(f"{prefix}.horse_no: invalid")
            continue
        status = review.get("status")
        if status not in {"selected", "rejected"}:
            errors.append(f"{prefix}.status: must be selected or rejected")
        if status == "selected":
            selected.append(review)
            evidence = review.get("evidence")
            if not isinstance(evidence, list):
                evidence = []
                errors.append(f"{prefix}.evidence: must be a list")
            families = {item.get("family") for item in evidence if isinstance(item, dict)}
            if len(families) < 2:
                errors.append(f"{prefix}: selected horse needs two independent evidence families")
            if not any(family and family != "market" for family in families):
                errors.append(f"{prefix}: at least one evidence family must be non-market")
            for item in evidence:
                if not isinstance(item, dict):
                    errors.append(f"{prefix}.evidence: every item must be an object")
                    continue
                source = sources.get(item.get("source_id"))
                if not source:
                    errors.append(f"{prefix}.evidence: unknown source_id")
                elif not quote_supported(str(item.get("quote", "")), str(source.get("excerpt", ""))):
                    errors.append(f"{prefix}.evidence: quote is absent from source excerpt")
            for field in ("strongest_counterevidence", "withdraw_if"):
                if not str(review.get(field, "")).strip():
                    errors.append(f"{prefix}.{field}: required for selected horse")
            if not isinstance(review.get("unknowns"), list) or not review.get("unknowns"):
                errors.append(f"{prefix}.unknowns: provide at least one explicit unknown")
        elif status == "rejected" and not str(review.get("reason", "")).strip():
            errors.append(f"{prefix}.reason: required for rejected horse")

    if len(selected) > 1:
        errors.append("at most one horse may be selected per race")
    if len(reviewed_numbers) != len(set(reviewed_numbers)):
        errors.append("duplicate horse reviews are not allowed")
    if set(reviewed_numbers) != set(eligible):
        errors.append("reviews must cover every eligible 10+ horse and no others")
    selected_numbers = [int(r["horse_no"]) for r in selected]
    if selected_numbers and selected_numbers[0] not in eligible:
        errors.append("selected horse is not eligible in the qualified market snapshot")
    if not selected and not str(decision.get("abstain_reason", "")).strip():
        errors.append("abstain_reason is required when no horse is selected")
    if not market_qualified and selected:
        errors.append("selection is forbidden without a qualified market snapshot")

    forward_eligible = (
        not errors
        and market_qualified
        and card.get("collection_mode") == "live"
        and card.get("retrieval_after_cutoff") is not True
    )
    return {
        "kind": "longshot-decision-validation-v1",
        "race_id": race_id,
        "rule_version": decision.get("rule_version"),
        "validation_status": "passed" if not errors else "failed",
        "market_snapshot_qualified": market_qualified,
        "market_snapshot_in_window": in_window,
        "whole_field_market_complete": market_complete,
        "eligible_horses": [{"horse_no": n, "win_odds": eligible[n]} for n in sorted(eligible)],
        "selected_horse_no": selected_numbers[0] if selected_numbers else None,
        "abstained": not selected_numbers,
        "forward_kpi_eligible": forward_eligible,
        "warnings": warnings,
        "errors": errors,
    }


def result_rows(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows = {}
    for row in result.get("results", []):
        if isinstance(row, dict) and isinstance(row.get("horse_no"), int):
            rows[row["horse_no"]] = row
    return rows


def drawdown(returns: list[float]) -> float:
    profit = peak = maximum = 0.0
    for value in returns:
        profit += value - 1.0
        peak = max(peak, profit)
        maximum = max(maximum, peak - profit)
    return round(maximum, 4)


def bootstrap_interval(rows: list[dict[str, Any]], samples: int, seed: int = 20261029) -> list[float] | None:
    if not rows or samples <= 0:
        return None
    grouped: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        grouped[row["meeting_date"]].append(int(row["top3_hit"]))
    days = sorted(grouped)
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        hits = total = 0
        for _ in days:
            sampled = grouped[rng.choice(days)]
            hits += sum(sampled)
            total += len(sampled)
        estimates.append(100 * hits / total)
    estimates.sort()
    lower = estimates[int(0.025 * (len(estimates) - 1))]
    upper = estimates[int(0.975 * (len(estimates) - 1))]
    return [round(lower, 2), round(upper, 2)]


def backtest(input_dir: str | Path, bootstrap: int = 1000) -> dict[str, Any]:
    root = Path(input_dir)
    decision_paths = sorted((root / "decisions").glob("*.json"))
    if not decision_paths:
        raise ValueError("no decision files found")
    records = []
    excluded = []
    rule_versions = set()

    for decision_path in decision_paths:
        decision = load_json(decision_path)
        race_id = decision.get("race_id")
        card = load_json(root / "cards" / f"{race_id}.json")
        result = load_json(root / "results" / f"{race_id}.json")
        validation = validate_decision(card, decision)
        if validation["errors"]:
            raise ValueError(f"{race_id}: " + "; ".join(validation["errors"]))
        if result.get("race_id") != race_id:
            raise ValueError(f"{race_id}: result race_id mismatch")
        if not any(s.get("official") is True for s in result.get("sources", [])):
            raise ValueError(f"{race_id}: official result source is required")
        if not validation["forward_kpi_eligible"]:
            excluded.append({"race_id": race_id, "reason": "not a verified live pre-race decision"})
            continue
        rule_versions.add(decision["rule_version"])
        records.append((card, decision, result, validation))
    if len(rule_versions) > 1:
        raise ValueError("one backtest accepts exactly one rule_version")

    selected_rows = []
    eligible_total = eligible_hits = actual_cold_top3 = 0
    random_expected = 0.0
    win_returns = []
    place_returns = []
    win_complete = place_complete = True
    race_summaries = []

    for card, decision, result, validation in records:
        race_id = card["race"]["race_id"]
        eligible = eligible_horses(card)
        slots = place_slots(card)
        finishes = result_rows(result)
        cold_top3 = {
            horse_no for horse_no in eligible
            if finishes.get(horse_no, {}).get("finish_position", 10**9) <= slots
        }
        eligible_total += len(eligible)
        eligible_hits += len(cold_top3)
        actual_cold_top3 += len(cold_top3)
        selected_no = validation["selected_horse_no"]
        race_summaries.append({
            "race_id": race_id,
            "selected_horse_no": selected_no,
            "abstained": selected_no is None,
            "eligible_horse_numbers": sorted(eligible),
            "actual_cold_top3_horse_numbers": sorted(cold_top3),
        })
        if selected_no is None:
            continue
        selected_result = finishes.get(selected_no)
        if not selected_result:
            raise ValueError(f"{race_id}: selected horse missing from official result")
        finish = selected_result.get("finish_position")
        top3_hit = isinstance(finish, int) and finish <= slots
        winner = finish == 1
        random_expected += len(cold_top3) / len(eligible) if eligible else 0.0
        win_return = 0.0
        place_return = 0.0
        if winner:
            dividend = selected_result.get("win_dividend")
            if dividend is None:
                win_complete = False
            else:
                win_return = float(dividend) / 10.0
        if top3_hit:
            dividend = selected_result.get("place_dividend")
            if dividend is None:
                place_complete = False
            else:
                place_return = float(dividend) / 10.0
        win_returns.append(win_return)
        place_returns.append(place_return)
        selected_rows.append({
            "race_id": race_id,
            "meeting_date": card["race"]["meeting_date"],
            "horse_no": selected_no,
            "win_odds_snapshot": eligible[selected_no],
            "finish_position": finish,
            "top3_hit": top3_hit,
            "winner": winner,
            "win_return_units": win_return,
            "place_return_units": place_return,
        })

    selected_count = len(selected_rows)
    hits = sum(row["top3_hit"] for row in selected_rows)
    winners = sum(row["winner"] for row in selected_rows)
    losing_streak = longest_losing_streak = 0
    for row in selected_rows:
        losing_streak = 0 if row["top3_hit"] else losing_streak + 1
        longest_losing_streak = max(longest_losing_streak, losing_streak)
    metrics = {
        "races_including_abstentions": len(records),
        "selected_count": selected_count,
        "abstained_races": len(records) - selected_count,
        "abstention_rate_pct": pct(len(records) - selected_count, len(records)),
        "selected_top3_hits": hits,
        "selected_top3_hit_rate_pct": pct(hits, selected_count),
        "selected_top3_hit_rate_cluster_bootstrap_ci95_pct": bootstrap_interval(selected_rows, bootstrap),
        "selected_winner_rate_pct": pct(winners, selected_count),
        "actual_cold_top3_capture_rate_pct": pct(hits, actual_cold_top3),
        "all_eligible_runners_top3_baseline_pct": pct(eligible_hits, eligible_total),
        "same_count_random_selection_expectation_pct": pct(random_expected, selected_count),
        "longest_top3_losing_streak": longest_losing_streak,
        "historical_price_band_baseline": {
            "status": "requires_external_history",
            "note": "Do not relabel this meeting's own price-band outcome as independent history.",
        },
        "win_roi": None,
        "place_roi": None,
    }
    if selected_count and win_complete:
        metrics["win_roi"] = {
            "turnover_units": selected_count,
            "return_units": round(sum(win_returns), 4),
            "roi_pct": pct(sum(win_returns) - selected_count, selected_count),
            "max_drawdown_units": drawdown(win_returns),
        }
    if selected_count and place_complete:
        metrics["place_roi"] = {
            "turnover_units": selected_count,
            "return_units": round(sum(place_returns), 4),
            "roi_pct": pct(sum(place_returns) - selected_count, selected_count),
            "max_drawdown_units": drawdown(place_returns),
        }
    return {
        "kind": "longshot-selective-backtest-v1",
        "rule_version": next(iter(rule_versions)) if rule_versions else None,
        "maturity": {
            "status": (
                "time_out_evaluation_ready"
                if len(records) >= 30 and selected_count >= 100
                else "preliminary_only"
            ),
            "minimum_races_met": len(records) >= 30,
            "minimum_selections_met": selected_count >= 100,
            "note": "Before both thresholds, describe results only. Meeting them permits evaluation, not claims of stable edge or profit.",
        },
        "metrics": metrics,
        "races": race_summaries,
        "selections": selected_rows,
        "excluded_historical_or_unverified": excluded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--card", required=True)
    validate_parser.add_argument("--decision", required=True)
    validate_parser.add_argument("--output", required=True)
    backtest_parser = sub.add_parser("backtest")
    backtest_parser.add_argument("--input-dir", required=True)
    backtest_parser.add_argument("--bootstrap", type=int, default=1000)
    backtest_parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.command == "validate":
        report = validate_decision(load_json(args.card), load_json(args.decision))
        save_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["errors"]:
            raise SystemExit(1)
    else:
        report = backtest(args.input_dir, args.bootstrap)
        save_json(args.output, report)
        print(json.dumps({"metrics": report["metrics"], "maturity": report["maturity"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
