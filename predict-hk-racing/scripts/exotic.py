#!/usr/bin/env python3
"""Freeze and settle evidence-led HKJC Q / Place-Quinella combinations.

The ticket is bound to a validated longshot decision and market card by content
hash. Payouts come only from recorded official dividends; never from odds or a
probability approximation. Agent isolation is procedural, not independent proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from domain import quote_supported
from longshot import (
    active_runners,
    load_json,
    parse_time,
    pct,
    place_slots,
    result_rows,
    save_json,
    validate_decision,
)

TICKET_SCHEMA = "exotic-ticket-v1"
POOLS = ("Q", "PQ")
ROLES = {"form_and_race_shape", "stable_and_intelligence"}
CHECKS = ("identity", "timestamps", "quotes", "counterevidence", "candidate_coverage")
HEX_256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def money(value: float) -> float:
    return round(value + 1e-9, 2)


def number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def coerce_pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        return None
    pair = tuple(value)
    return pair if pair[0] < pair[1] else None


def parse_pair_text(value: str) -> list[int]:
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", value)
    if not match:
        raise ValueError(f"Invalid Q/PQ combination: {value!r}")
    pair = sorted((int(match.group(1)), int(match.group(2))))
    if pair[0] == pair[1]:
        raise ValueError(f"Q/PQ combination needs two different horses: {value!r}")
    return pair


def validate_isolation(
    ticket: dict[str, Any], sources: dict[str, dict[str, Any]], created: Any, errors: list[str]
) -> dict[str, Any]:
    researchers = ticket.get("researchers")
    if not isinstance(researchers, list) or len(researchers) != 2:
        errors.append("researchers must contain exactly two isolated researchers")
        researchers = []
    by_role: dict[str, dict[str, Any]] = {}
    completion_times = []
    for index, researcher in enumerate(researchers):
        if not isinstance(researcher, dict):
            errors.append(f"researchers[{index}] must be an object")
            continue
        role = researcher.get("role")
        if role not in ROLES or role in by_role:
            errors.append("researchers require one form_and_race_shape and one stable_and_intelligence")
            continue
        by_role[role] = researcher
        if not str(researcher.get("agent_id", "")).strip():
            errors.append(f"researchers[{index}].agent_id is required")
        if researcher.get("blind_to_other_researcher_output") is not True:
            errors.append(f"researchers[{index}] must be blind to the other researcher's output")
        source_ids = researcher.get("source_ids")
        if not isinstance(source_ids, list) or not source_ids or any(sid not in sources for sid in source_ids):
            errors.append(f"researchers[{index}].source_ids must reference registered raw sources")
        completed = parse_time(researcher.get("completed_at"), f"researchers[{index}].completed_at", errors)
        if completed:
            completion_times.append(completed)
            if created and completed > created:
                errors.append(f"researchers[{index}].completed_at must precede ticket creation")

    audit = ticket.get("audit")
    if not isinstance(audit, dict):
        errors.append("audit must be an object")
        audit = {}
    if audit.get("status") != "passed" or audit.get("critical_errors"):
        errors.append("independent audit must pass with no unresolved critical_errors")
    if audit.get("recommendations_exposed_before_completion") is not False:
        errors.append("auditor must complete raw-source review before seeing recommendations")
    if audit.get("independent_source_search") is not True:
        errors.append("auditor must independently search for missing or contrary sources")
    checks = audit.get("checks")
    if not isinstance(checks, dict) or any(checks.get(field) is not True for field in CHECKS):
        errors.append("audit.checks must pass identity, timestamps, quotes, counterevidence and candidate_coverage")
    audit_sources = audit.get("source_ids")
    if not isinstance(audit_sources, list) or not audit_sources or any(sid not in sources for sid in audit_sources):
        errors.append("audit.source_ids must reference registered raw sources")
        audit_sources = []
    started = parse_time(audit.get("started_at"), "audit.started_at", errors)
    completed = parse_time(audit.get("completed_at"), "audit.completed_at", errors)
    if started and completion_times and started < max(completion_times):
        errors.append("audit must start after both researchers complete their isolated work")
    if started and completed and completed < started:
        errors.append("audit.completed_at cannot precede started_at")
    if created and completed and completed > created:
        errors.append("audit must complete before ticket creation")

    researcher_ids = {
        sid for researcher in by_role.values() for sid in researcher.get("source_ids", [])
    }
    role_sources = [set(researcher.get("source_ids", [])) for researcher in by_role.values()]
    shared_between = set.intersection(*role_sources) if len(role_sources) == 2 else set()
    return {
        "scope": "procedural_isolation_only_not_evidence_independence",
        "researcher_shared_source_ids": sorted(shared_between),
        "auditor_shared_source_ids": sorted(set(audit_sources) & researcher_ids),
        "note": "Shared sources remain one evidence chain; agent agreement is not independent proof.",
    }


def validate_ticket(
    card: dict[str, Any], decision: dict[str, Any], ticket: dict[str, Any]
) -> dict[str, Any]:
    errors: list[str] = []
    base = validate_decision(card, decision)
    errors.extend(f"longshot: {message}" for message in base["errors"])
    race_id = card.get("race", {}).get("race_id")
    if ticket.get("schema_version") != TICKET_SCHEMA:
        errors.append(f"schema_version must be {TICKET_SCHEMA}")
    if ticket.get("race_id") != race_id:
        errors.append("ticket.race_id must match card.race.race_id")
    if not str(ticket.get("rule_version", "")).strip():
        errors.append("rule_version is required")
    if ticket.get("longshot_decision_sha256") != canonical_sha256(decision):
        errors.append("longshot_decision_sha256 does not bind this frozen decision")
    if ticket.get("market_card_sha256") != canonical_sha256(card):
        errors.append("market_card_sha256 does not bind this market snapshot")
    core = base["selected_horse_no"]
    if ticket.get("core_horse_no") != core:
        errors.append("ticket.core_horse_no must equal the validated longshot selection")

    off = parse_time(card.get("race", {}).get("off_time"), "card.race.off_time", errors)
    created = parse_time(ticket.get("created_at"), "ticket.created_at", errors)
    decision_created = parse_time(decision.get("created_at"), "decision.created_at", errors)
    if created and off and created >= off:
        errors.append("ticket.created_at must be before off_time")
    if created and decision_created and created < decision_created:
        errors.append("ticket cannot precede its frozen longshot decision")

    raw_sources = ticket.get("sources")
    if not isinstance(raw_sources, list):
        errors.append("sources must be a list")
        raw_sources = []
    sources: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(raw_sources):
        if not isinstance(source, dict) or not source.get("source_id"):
            errors.append(f"sources[{index}] needs a source_id")
            continue
        source_id = source["source_id"]
        if source_id in sources:
            errors.append(f"duplicate source_id: {source_id}")
        sources[source_id] = source
        if not str(source.get("excerpt", "")).strip() or not str(source.get("url", "")).strip():
            errors.append(f"sources[{index}] needs url and excerpt")
        if not HEX_256.fullmatch(str(source.get("sha256", ""))):
            errors.append(f"sources[{index}].sha256 must identify the original source bytes")
        retrieved = parse_time(source.get("retrieved_at"), f"sources[{index}].retrieved_at", errors)
        if retrieved and created and retrieved > created:
            errors.append(f"sources[{index}] cannot be retrieved after ticket creation")
    isolation = validate_isolation(ticket, sources, created, errors)
    raw_researchers = ticket.get("researchers")
    if not isinstance(raw_researchers, list):
        raw_researchers = []
    researchers = {
        researcher.get("agent_id"): researcher
        for researcher in raw_researchers
        if isinstance(researcher, dict) and researcher.get("agent_id")
    }

    combinations = ticket.get("combinations")
    if not isinstance(combinations, list):
        errors.append("combinations must be a list")
        combinations = []
    if not combinations and not str(ticket.get("abstain_reason", "")).strip():
        errors.append("abstain_reason is required for an empty combination ticket")
    if combinations and core is None:
        errors.append("an abstained longshot decision cannot generate Q/PQ combinations")
    active = {int(runner["horse_no"]) for runner in active_runners(card)}
    used_pools = {
        item.get("pool") for item in combinations
        if isinstance(item, dict) and isinstance(item.get("pool"), str)
    }
    if "Q" in used_pools and len(active) < 3:
        errors.append("Q requires at least three active runners")
    if "PQ" in used_pools and len(active) < 7:
        errors.append("PQ requires at least seven active runners")

    seen: set[tuple[str, int, int]] = set()
    costs = {pool: 0.0 for pool in POOLS}
    counts = {pool: 0 for pool in POOLS}
    for index, item in enumerate(combinations):
        prefix = f"combinations[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        pool = item.get("pool")
        if pool not in POOLS:
            errors.append(f"{prefix}.pool must be Q or PQ")
            continue
        pair = coerce_pair(item.get("horses"))
        if not pair:
            errors.append(f"{prefix}.horses must be two distinct horse numbers in ascending order")
            continue
        if not set(pair) <= active:
            errors.append(f"{prefix} contains an inactive or unknown horse")
        key = (pool, *pair)
        if key in seen:
            errors.append(f"duplicate combination in the same pool: {pool} {pair}")
        seen.add(key)
        stake = item.get("stake_hkd")
        if not number(stake) or stake < 10 or abs(stake / 10 - round(stake / 10)) > 1e-9:
            errors.append(f"{prefix}.stake_hkd must be a positive HK$10 multiple")
        else:
            costs[pool] += float(stake)
            counts[pool] += 1
        for field in ("scenario_id", "mechanism", "strongest_counterevidence", "withdraw_if"):
            if not str(item.get(field, "")).strip():
                errors.append(f"{prefix}.{field} is required")
        if not isinstance(item.get("unknowns"), list) or not item.get("unknowns"):
            errors.append(f"{prefix}.unknowns must contain at least one explicit unknown")
        evidence = item.get("horse_evidence")
        if not isinstance(evidence, list):
            errors.append(f"{prefix}.horse_evidence must be a list")
            evidence = []
        evidenced_horses = set()
        for evidence_index, claim in enumerate(evidence):
            if not isinstance(claim, dict):
                errors.append(f"{prefix}.horse_evidence[{evidence_index}] must be an object")
                continue
            horse_no = claim.get("horse_no")
            if horse_no in pair:
                evidenced_horses.add(horse_no)
            else:
                errors.append(f"{prefix}.horse_evidence refers to a horse outside the pair")
            family = claim.get("family")
            if not str(family or "").strip() or family == "market":
                errors.append(f"{prefix}.horse_evidence needs a non-market family")
            source = sources.get(claim.get("source_id"))
            if not source or not quote_supported(str(claim.get("quote", "")), str(source.get("excerpt", ""))):
                errors.append(f"{prefix}.horse_evidence quote is absent from its source excerpt")
            researcher = researchers.get(claim.get("agent_id"))
            if not researcher or claim.get("source_id") not in researcher.get("source_ids", []):
                errors.append(f"{prefix}.horse_evidence agent/source attribution is invalid")
        if evidenced_horses != set(pair):
            errors.append(f"{prefix} needs non-market evidence for each horse")

    total_cost = money(sum(costs.values()))
    budget = ticket.get("budget_limit_hkd")
    if not number(budget) or budget <= 0:
        errors.append("budget_limit_hkd must be positive")
    elif total_cost > budget + 1e-9:
        errors.append("frozen combinations exceed budget_limit_hkd")
    forward_eligible = not errors and base["forward_kpi_eligible"]
    return {
        "kind": "exotic-ticket-freeze-v1",
        "schema_version": TICKET_SCHEMA,
        "race_id": race_id,
        "rule_version": ticket.get("rule_version"),
        "core_horse_no": core,
        "validation_status": "passed" if not errors else "failed",
        "freeze_status": "ready" if not errors else "blocked",
        "ticket_sha256": canonical_sha256(ticket),
        "combination_counts": counts,
        "cost_by_pool_hkd": {pool: money(costs[pool]) for pool in POOLS},
        "total_cost_hkd": total_cost,
        "forward_kpi_eligible": forward_eligible,
        "agent_isolation": isolation,
        "source_integrity": "declared_original_hashes_and_quotes_checked",
        "errors": errors,
    }


def official_pool(result: dict[str, Any], pool: str) -> tuple[str, dict, dict, bool]:
    pools = result.get("exotic_dividends")
    if not isinstance(pools, dict) or not isinstance(pools.get(pool), dict):
        raise ValueError(f"official {pool} dividend record is required; no odds fallback is allowed")
    data = pools[pool]
    status = data.get("status")
    if status not in {"final", "closed"}:
        raise ValueError(f"official {pool} status must be final or closed")
    dividends: dict[tuple[int, int], float] = {}
    refunds: dict[tuple[int, int], float] = {}
    for row in data.get("combinations", []):
        pair = coerce_pair(row.get("horses")) if isinstance(row, dict) else None
        dividend = row.get("dividend_hkd") if isinstance(row, dict) else None
        if not pair or not number(dividend) or dividend <= 0 or pair in dividends:
            raise ValueError(f"invalid or duplicate official {pool} dividend combination")
        dividends[pair] = float(dividend)
    for row in data.get("refunds", []):
        pair = coerce_pair(row.get("horses")) if isinstance(row, dict) else None
        amount = row.get("amount_hkd") if isinstance(row, dict) else None
        if not pair or not number(amount) or amount <= 0 or pair in refunds:
            raise ValueError(f"invalid or duplicate official {pool} refund combination")
        refunds[pair] = float(amount)
    if status == "closed" and (data.get("refund_all") is not True or dividends or refunds):
        raise ValueError(f"closed {pool} requires refund_all with no dividend or partial-refund rows")
    return status, dividends, refunds, status == "closed"


def settle_ticket(
    card: dict[str, Any], decision: dict[str, Any], ticket: dict[str, Any], result: dict[str, Any],
    freeze_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    freeze = validate_ticket(card, decision, ticket)
    if freeze["errors"]:
        raise ValueError("ticket validation failed: " + "; ".join(freeze["errors"]))
    if freeze_receipt is not None and (
        freeze_receipt.get("kind") != "exotic-ticket-freeze-v1"
        or freeze_receipt.get("freeze_status") != "ready"
        or freeze_receipt.get("ticket_sha256") != freeze["ticket_sha256"]
    ):
        raise ValueError("ticket differs from its ready freeze receipt")
    if result.get("race_id") != ticket.get("race_id"):
        raise ValueError("result race_id mismatch")
    if not any(isinstance(s, dict) and s.get("official") is True for s in result.get("sources", [])):
        raise ValueError("an official result/dividend source is required")
    errors: list[str] = []
    recorded = parse_time(result.get("recorded_at"), "result.recorded_at", errors)
    off = parse_time(card.get("race", {}).get("off_time"), "card.race.off_time", errors)
    if errors or (recorded and off and recorded < off):
        raise ValueError("result must have a valid timestamp after off_time")
    finishes = result_rows(result)
    core = freeze["core_horse_no"]
    core_finish = finishes.get(core, {}).get("finish_position")
    intelligence_hit = isinstance(core_finish, int) and core_finish <= place_slots(card)
    active_odds = {int(r["horse_no"]): r.get("win_odds") for r in active_runners(card)}
    rows = []
    pool_cash = {pool: {"cost": 0.0, "refunds": 0.0, "dividends": 0.0} for pool in POOLS}
    target_hits = official_hits = 0
    target_payout = non_core_payout = both_below_10_payout = 0.0

    for item in ticket["combinations"]:
        pool = item["pool"]
        pair = tuple(item["horses"])
        stake = float(item["stake_hkd"])
        status, dividends, refunds, refund_all = official_pool(result, pool)
        if any(number not in finishes for number in pair):
            raise ValueError(f"official result lacks horse in {pool} {pair}")
        refund = stake if refund_all else refunds.get(pair, 0.0)
        if refund > stake + 1e-9:
            raise ValueError(f"refund exceeds stake for {pool} {pair}")
        if pair in dividends and refund:
            raise ValueError(f"one combination cannot both pay a dividend and be refunded: {pool} {pair}")
        if refund_all and refund != stake:
            raise ValueError(f"closed pool must refund the full stake: {pool} {pair}")
        dividend = dividends.get(pair)
        payout = dividend * stake / 10.0 if dividend is not None else 0.0
        positions = [finishes[horse_no].get("finish_position") for horse_no in pair]
        if not refund and any(not isinstance(position, int) for position in positions):
            raise ValueError(f"official refund or adjudication is required for non-finisher in {pool} {pair}")
        slots = 2 if pool == "Q" else 3
        sporting_hit = not refund and all(isinstance(position, int) and position <= slots for position in positions)
        official_hit = dividend is not None
        target_hit = sporting_hit and core in pair
        target_hits += int(target_hit)
        official_hits += int(official_hit)
        if official_hit:
            if core in pair:
                target_payout += payout
            else:
                non_core_payout += payout
            if all(number(active_odds[horse_no]) and active_odds[horse_no] < 10 for horse_no in pair):
                both_below_10_payout += payout
        cash = pool_cash[pool]
        cash["cost"] += stake
        cash["refunds"] += refund
        cash["dividends"] += payout
        rows.append({
            "pool": pool,
            "horses": list(pair),
            "contains_non_favourite_core": core in pair,
            "stake_hkd": money(stake),
            "refund_hkd": money(refund),
            "official_dividend_per_10_hkd": dividend,
            "payout_hkd": money(payout),
            "profit_hkd": money(payout + refund - stake),
            "sporting_hit": sporting_hit,
            "target_hit": target_hit,
            "official_payout_hit": official_hit,
            "exception_review_required": official_hit and not sporting_hit,
        })

    summaries = {}
    for pool in POOLS:
        cash = pool_cash[pool]
        net_stake = cash["cost"] - cash["refunds"]
        profit = cash["dividends"] - net_stake
        summaries[pool] = {
            "combination_count": freeze["combination_counts"][pool],
            "cost_hkd": money(cash["cost"]),
            "refunds_hkd": money(cash["refunds"]),
            "net_stake_hkd": money(net_stake),
            "dividends_hkd": money(cash["dividends"]),
            "net_profit_hkd": money(profit),
            "roi_on_net_stake_pct": pct(profit, net_stake),
        }
    cost = sum(cash["cost"] for cash in pool_cash.values())
    refunds = sum(cash["refunds"] for cash in pool_cash.values())
    payouts = sum(cash["dividends"] for cash in pool_cash.values())
    net_stake = cost - refunds
    return {
        "kind": "exotic-official-settlement-v1",
        "race_id": ticket["race_id"],
        "rule_version": ticket["rule_version"],
        "ticket_sha256": freeze["ticket_sha256"],
        "core_horse_no": core,
        "accounts": {
            "intelligence_hit_core_top3": intelligence_hit,
            "target_combination_hits": target_hits,
            "official_payout_hits": official_hits,
            "target_core_payout_hkd": money(target_payout),
            "non_core_payout_hkd": money(non_core_payout),
            "both_horses_win_odds_below_10_payout_hkd": money(both_below_10_payout),
            "note": "Non-core and hot-only payouts stay in cash but are not target hits.",
        },
        "cash": {
            "cost_hkd": money(cost),
            "refunds_hkd": money(refunds),
            "net_stake_hkd": money(net_stake),
            "official_dividends_hkd": money(payouts),
            "total_return_including_refunds_hkd": money(payouts + refunds),
            "net_profit_hkd": money(payouts - net_stake),
            "roi_on_net_stake_pct": pct(payouts - net_stake, net_stake),
        },
        "pools": summaries,
        "combinations": rows,
        "settlement_basis": "official dividends per HK$10 and explicit official refunds; no odds or probability fallback",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("freeze", "settle"):
        command = sub.add_parser(name)
        command.add_argument("--card", required=True)
        command.add_argument("--longshot-decision", required=True)
        command.add_argument("--ticket", required=True)
        command.add_argument("--output", required=True)
        if name == "settle":
            command.add_argument("--result", required=True)
            command.add_argument("--freeze", required=True)
    args = parser.parse_args()
    card = load_json(args.card)
    decision = load_json(args.longshot_decision)
    ticket = load_json(args.ticket)
    if args.command == "freeze":
        report = validate_ticket(card, decision, ticket)
    else:
        report = settle_ticket(
            card, decision, ticket, load_json(args.result), load_json(args.freeze)
        )
    save_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
