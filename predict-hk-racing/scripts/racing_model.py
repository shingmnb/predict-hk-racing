#!/usr/bin/env python3
"""Deterministic modelling primitives for the predict-hk-racing skill.

Standard library only. All probabilities are model estimates, not guarantees.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import mean, median, pstdev

EPS = 1e-12
DIRECTIONS = {
    "positive": 1.0,
    "negative": -1.0,
    "neutral": 0.0,
    "1": 1.0,
    "-1": -1.0,
    "0": 0.0,
}


def runner_key(runner: dict) -> int:
    return int(runner["horse_no"])


def active_runners(card: dict) -> list[dict]:
    return [r for r in card["runners"] if not r.get("scratched", False)]


def assess_intent(runner: dict) -> dict:
    """Shrink evidence towards 50; this is support, not P(trying)."""
    weighted_direction = 0.0
    total_evidence = 0.0
    clean_evidence = []
    for item in runner.get("intent_evidence", []):
        raw_direction = item.get("direction", "neutral")
        if isinstance(raw_direction, (int, float)):
            direction = max(-1.0, min(1.0, float(raw_direction)))
        else:
            direction = DIRECTIONS.get(str(raw_direction).lower(), 0.0)
        strength = max(0.0, min(1.0, float(item.get("strength", 0.5))))
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        effective = strength * confidence
        weighted_direction += direction * effective
        total_evidence += effective
        clean_evidence.append({
            "signal": item.get("signal", "unspecified"),
            "direction": direction,
            "strength": strength,
            "confidence": confidence,
            "source_id": item.get("source_id"),
            "note": item.get("note", ""),
        })

    centered = weighted_direction / (total_evidence + 2.0)
    score = 50.0 + 50.0 * centered
    confidence = 1.0 - math.exp(-total_evidence / 2.0)
    if total_evidence < 0.15:
        label = "證據不足"
    elif score >= 65:
        label = "較支持去馬"
    elif score >= 57:
        label = "略偏去馬"
    elif score <= 35:
        label = "較像保留／備戰"
    elif score <= 43:
        label = "略偏保留／備戰"
    else:
        label = "中性／訊號分歧"
    confidence_label = "高" if confidence >= 0.75 else ("中" if confidence >= 0.45 else "低")
    return {
        "score": round(score, 3),
        "centered": centered,
        "label": label,
        "confidence": round(confidence, 3),
        "confidence_label": confidence_label,
        "effective_evidence": round(total_evidence, 3),
        "evidence": clean_evidence,
        "warning": "意圖支持度不是去馬概率；賽果不能反推真實意圖。",
    }


def shin_probabilities(inverse_odds: list[float]) -> list[float]:
    """Shin (1993) de-vigging for a complete win book.

    Simple normalisation (``p_i = (1/o_i) / Σ(1/o_j)``) spreads the overround
    proportionally and therefore inherits the favourite–longshot bias: it
    over-states longshots and under-states favourites. Shin models the book as
    ``π_i = (1 - z) p_i + z p_i² / Σp`` where ``z`` is the insider share, and
    solves for the ``z`` that makes the probabilities sum to one.

    ``inverse_odds`` are raw ``1 / decimal_odds`` values (overround > 1).
    """
    book = sum(inverse_odds)
    if book <= 0:
        raise ValueError("inverse odds must be positive")
    if book <= 1.0:
        return [x / book for x in inverse_odds]

    def probs(z: float) -> list[float]:
        denom = 2.0 * (1.0 - z)
        return [
            (math.sqrt(z * z + 4.0 * (1.0 - z) * (x * x) / book) - z) / denom
            for x in inverse_odds
        ]

    low, high = 0.0, 0.999999  # Σp decreases monotonically from √book to Σπ²/book
    for _ in range(100):
        mid = (low + high) / 2.0
        if sum(probs(mid)) > 1.0:
            low = mid
        else:
            high = mid
    result = probs((low + high) / 2.0)
    total = sum(result)
    return [x / total for x in result]


def market_probabilities(
    runners: list[dict], method: str = "normalized"
) -> tuple[dict[int, float], bool, float | None]:
    """De-vigged win probabilities.

    ``method``: ``normalized`` (default, backward compatible) or ``shin``
    (favourite–longshot aware). Both return probabilities summing to 1 when the
    book is complete; an incomplete book still falls back to uniform with a
    ``False`` flag so callers can downgrade the run.
    """
    if method not in {"normalized", "shin"}:
        raise ValueError(f"unknown de-vig method: {method}")
    if not runners:
        return {}, False, None
    complete = all(
        isinstance(r.get("win_odds"), (int, float))
        and not isinstance(r.get("win_odds"), bool)
        and math.isfinite(float(r["win_odds"]))
        and float(r["win_odds"]) > 1.0
        for r in runners
    )
    if complete:
        keys = [runner_key(r) for r in runners]
        inverse = [1.0 / float(r["win_odds"]) for r in runners]
        book_sum = sum(inverse)
        if method == "shin":
            adjusted = shin_probabilities(inverse)
        else:
            adjusted = [x / book_sum for x in inverse]
        return dict(zip(keys, adjusted)), True, book_sum - 1.0
    uniform = 1.0 / len(runners)
    return {runner_key(r): uniform for r in runners}, False, None


def softmax(scores: list[float]) -> list[float]:
    if not scores:
        return []
    peak = max(scores)
    exps = [math.exp(max(-700.0, min(700.0, s - peak))) for s in scores]
    total = sum(exps)
    return [x / total for x in exps]


def place_probabilities(win_probs: list[float], slots: int) -> list[float]:
    """Exact top-2/top-3 inclusion under a Plackett-Luce approximation."""
    n = len(win_probs)
    slots = min(slots, n)
    if slots <= 0:
        return [0.0] * n
    if slots == n:
        return [1.0] * n
    p = [max(EPS, min(1.0 - EPS, x)) for x in win_probs]
    norm = sum(p)
    p = [x / norm for x in p]
    if slots == 1:
        return p
    result = p[:]
    for i in range(n):
        second = 0.0
        for j in range(n):
            if j == i:
                continue
            second += p[j] * p[i] / max(EPS, 1.0 - p[j])
        result[i] += second
    if slots == 2:
        return [min(1.0, x) for x in result]
    if slots != 3:
        raise ValueError("place_probabilities supports one to three place slots")
    for i in range(n):
        third = 0.0
        for j in range(n):
            if j == i:
                continue
            denom_j = max(EPS, 1.0 - p[j])
            for k in range(n):
                if k == i or k == j:
                    continue
                denom_k = max(EPS, 1.0 - p[j] - p[k])
                third += (
                    p[j]
                    * p[k]
                    / denom_j
                    * p[i]
                    / denom_k
                )
        result[i] += third
    return [min(1.0, x) for x in result]


def pair_probabilities(win_probs: list[float], slots: int = 3) -> dict[tuple[int, int], float]:
    """Joint inclusion probabilities for quinella / Place-Quinella tickets.

    Returns ``{(i, j): P(i and j both finish inside the top ``slots``)}`` for
    ``i < j``, using the same Plackett–Luce approximation as
    :func:`place_probabilities`.

    ``slots=2`` is the 連贏 (Quinella) case — the two horses must be the exact
    top two in either order. ``slots=3`` is 位置Q (Place Quinella).
    """
    n = len(win_probs)
    if slots not in (2, 3):
        raise ValueError("pair_probabilities supports slots=2 (quinella) or slots=3 (place quinella)")
    if n < 2:
        return {}
    p = [max(EPS, min(1.0 - EPS, x)) for x in win_probs]
    total = sum(p)
    p = [x / total for x in p]

    pairs: dict[tuple[int, int], float] = {}
    if slots == 2:
        for i in range(n):
            for j in range(i + 1, n):
                p_ij = p[i] * p[j] / max(EPS, 1.0 - p[i])
                p_ji = p[j] * p[i] / max(EPS, 1.0 - p[j])
                pairs[(i, j)] = min(1.0, p_ij + p_ji)
        return pairs

    for i in range(n):
        for j in range(i + 1, n):
            joint = 0.0
            for k in range(n):
                if k == i or k == j:
                    continue
                for first, second, third in (
                    (i, j, k), (i, k, j), (j, i, k),
                    (j, k, i), (k, i, j), (k, j, i),
                ):
                    joint += (
                        p[first]
                        * p[second] / max(EPS, 1.0 - p[first])
                        * p[third] / max(EPS, 1.0 - p[first] - p[second])
                    )
            pairs[(i, j)] = min(1.0, joint)
    return pairs


def _raw_value(runner: dict, name: str, base_p: float, market_complete: bool):
    if name == "market_logp":
        return math.log(max(EPS, base_p)) if market_complete else None
    if name == "intent_centered":
        return assess_intent(runner)["centered"]
    value = runner.get("features", {}).get(name, runner.get(name))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def raw_feature_rows(card: dict, feature_names: list[str]):
    runners = active_runners(card)
    base, complete, overround = market_probabilities(runners)
    rows = []
    for runner in runners:
        key = runner_key(runner)
        rows.append({
            "horse_no": key,
            "runner": runner,
            "base_p": base[key],
            "features": [
                _raw_value(runner, name, base[key], complete)
                for name in feature_names
            ],
        })
    return rows, complete, overround


def _normalisation(prepared: list[dict], feature_names: list[str]) -> dict:
    stats = {}
    total_rows = sum(len(item["rows"]) for item in prepared)
    for index, name in enumerate(feature_names):
        values = [
            row["features"][index]
            for item in prepared
            for row in item["rows"]
            if row["features"][index] is not None
        ]
        if not values:
            raise ValueError(f"Feature has no observed training values: {name}")
        centre = mean(values)
        scale = pstdev(values)
        if scale < 1e-8:
            scale = 1.0
        stats[name] = {
            "mean": centre,
            "scale": scale,
            "impute": median(values),
            "coverage": len(values) / max(1, total_rows),
        }
    return stats


def _vector(row: dict, feature_names: list[str], normalisation: dict) -> list[float]:
    vector = []
    for index, name in enumerate(feature_names):
        stat = normalisation[name]
        value = row["features"][index]
        if value is None:
            value = stat["impute"]
        vector.append((float(value) - stat["mean"]) / stat["scale"])
    return vector


def _dot(coefficients: list[float], vector: list[float]) -> float:
    return sum(a * b for a, b in zip(coefficients, vector))


def fit_model(
    records: list[dict],
    feature_names: list[str],
    *,
    l2: float = 0.01,
    learning_rate: float = 0.05,
    epochs: int = 300,
) -> dict:
    """Fit a race-softmax (conditional logit) model with Adam and L2."""
    if not feature_names:
        raise ValueError("At least one feature is required")
    if len(set(feature_names)) != len(feature_names):
        raise ValueError("Feature names must be unique")
    prepared = []
    skipped = []
    for record in records:
        card, result = record["card"], record["result"]
        rows, market_complete, _ = raw_feature_rows(card, feature_names)
        winners = [
            int(r["horse_no"]) for r in result["results"]
            if r.get("finish_position") == 1 and r.get("status", "OK") != "SCRATCHED"
        ]
        keys = [row["horse_no"] for row in rows]
        if len(winners) != 1 or winners[0] not in keys or len(rows) < 2:
            skipped.append(card["race"]["race_id"])
            continue
        prepared.append({
            "race_id": card["race"]["race_id"],
            "rows": rows,
            "winner": winners[0],
            "market_complete": market_complete,
        })
    if not prepared:
        raise ValueError("No usable completed races for training")

    normalisation = _normalisation(prepared, feature_names)
    for item in prepared:
        item["vectors"] = [_vector(row, feature_names, normalisation) for row in item["rows"]]
        item["winner_index"] = [r["horse_no"] for r in item["rows"]].index(item["winner"])

    size = len(feature_names)
    beta = [0.0] * size
    if "market_logp" in feature_names:
        beta[feature_names.index("market_logp")] = 0.25
    adam_m = [0.0] * size
    adam_v = [0.0] * size
    race_count = len(prepared)

    for epoch in range(1, epochs + 1):
        gradient = [0.0] * size
        for item in prepared:
            scores = [_dot(beta, vector) for vector in item["vectors"]]
            probabilities = softmax(scores)
            winner_index = item["winner_index"]
            for row_index, vector in enumerate(item["vectors"]):
                error = probabilities[row_index] - (1.0 if row_index == winner_index else 0.0)
                for feature_index in range(size):
                    gradient[feature_index] += error * vector[feature_index] / race_count
        for feature_index in range(size):
            gradient[feature_index] += l2 * beta[feature_index]
        norm = math.sqrt(sum(g * g for g in gradient))
        if norm > 5.0:
            gradient = [g * 5.0 / norm for g in gradient]
        for feature_index in range(size):
            g = gradient[feature_index]
            adam_m[feature_index] = 0.9 * adam_m[feature_index] + 0.1 * g
            adam_v[feature_index] = 0.999 * adam_v[feature_index] + 0.001 * g * g
            m_hat = adam_m[feature_index] / (1.0 - 0.9 ** epoch)
            v_hat = adam_v[feature_index] / (1.0 - 0.999 ** epoch)
            beta[feature_index] -= learning_rate * m_hat / (math.sqrt(v_hat) + 1e-8)

    losses = []
    for item in prepared:
        probabilities = softmax([_dot(beta, vector) for vector in item["vectors"]])
        losses.append(-math.log(max(EPS, probabilities[item["winner_index"]])))

    return {
        "algorithm": "conditional-logit-v1",
        "features": feature_names,
        "coefficients": {name: beta[i] for i, name in enumerate(feature_names)},
        "normalisation": normalisation,
        "hyperparameters": {
            "l2": l2,
            "learning_rate": learning_rate,
            "epochs": epochs,
        },
        "training": {
            "race_count": race_count,
            "runner_count": sum(len(item["rows"]) for item in prepared),
            "skipped_races": skipped,
            "training_logloss": mean(losses),
        },
    }


def predict_card(card: dict, model: dict | None = None, model_version: str = "market-baseline") -> dict:
    runners = active_runners(card)
    if len(runners) < 2:
        raise ValueError("At least two active runners are required")
    base, market_complete, overround = market_probabilities(runners)
    warnings = []
    if not market_complete:
        warnings.append("獨贏賠率不完整：市場基準改用均勻概率，唔可以當有市場優勢。")

    if model is None:
        scores = [math.log(max(EPS, base[runner_key(r)])) for r in runners]
        algorithm = "market-no-vig-v1"
        model_version = "market-baseline"
    else:
        algorithm = model["algorithm"]
        if algorithm != "conditional-logit-v1":
            raise ValueError(f"Unsupported model algorithm: {algorithm}")
        model_version = model.get("model_version", model_version)
        feature_names = model["features"]
        rows, _, _ = raw_feature_rows(card, feature_names)
        by_horse = {row["horse_no"]: row for row in rows}
        coefficients = [model["coefficients"][name] for name in feature_names]
        scores = []
        for runner in runners:
            row = by_horse[runner_key(runner)]
            vector = _vector(row, feature_names, model["normalisation"])
            scores.append(_dot(coefficients, vector))

    probabilities = softmax(scores)
    slots = min(int(card["race"].get("place_slots", 3)), len(runners))
    place = place_probabilities(probabilities, slots)
    output_runners = []
    for index, runner in enumerate(runners):
        key = runner_key(runner)
        intent = assess_intent(runner)
        output_runners.append({
            "horse_no": key,
            "horse_name": runner.get("horse_name", ""),
            "jockey": runner.get("jockey", ""),
            "trainer": runner.get("trainer", ""),
            "win_probability": probabilities[index],
            "place_probability": place[index],
            "fair_win_odds": 1.0 / max(EPS, probabilities[index]),
            "market_no_vig_probability": base[key],
            "win_odds_snapshot": runner.get("win_odds"),
            "intent": intent,
        })
    output_runners.sort(key=lambda item: item["win_probability"], reverse=True)
    return {
        "race_id": card["race"]["race_id"],
        "model_version": model_version,
        "algorithm": algorithm,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_cutoff": card["data_cutoff"],
        "place_slots": slots,
        "market_complete": market_complete,
        "market_overround": overround,
        "probability_note": "勝出概率全場合計為 1；位置概率用 Plackett-Luce 近似，唔係派彩保證。",
        "warnings": warnings,
        "runners": output_runners,
        "scratched": [
            {"horse_no": runner_key(r), "horse_name": r.get("horse_name", "")}
            for r in card["runners"] if r.get("scratched", False)
        ],
    }


def metric_row(card: dict, result: dict, prediction: dict) -> dict | None:
    predicted = prediction["runners"]
    by_horse = {int(item["horse_no"]): item for item in predicted}
    winners = [
        int(item["horse_no"]) for item in result["results"]
        if item.get("finish_position") == 1 and int(item["horse_no"]) in by_horse
    ]
    if len(winners) != 1:
        return None
    winner = winners[0]
    winner_item = by_horse[winner]
    ranked = sorted(predicted, key=lambda item: item["win_probability"], reverse=True)
    slots = int(prediction.get("place_slots", card["race"].get("place_slots", 3)))
    actual_positions = {
        int(item["horse_no"]): item.get("finish_position") for item in result["results"]
    }
    calibration = []
    place_errors = []
    for horse_no, item in by_horse.items():
        is_winner = 1.0 if horse_no == winner else 0.0
        calibration.append((item["win_probability"], is_winner))
        position = actual_positions.get(horse_no)
        if isinstance(position, int):
            is_place = 1.0 if position <= slots else 0.0
            place_errors.append((item["place_probability"] - is_place) ** 2)
    brier = sum(
        (item["win_probability"] - (1.0 if horse_no == winner else 0.0)) ** 2
        for horse_no, item in by_horse.items()
    )
    return {
        "race_id": card["race"]["race_id"],
        "off_time": card["race"]["off_time"],
        "logloss": -math.log(max(EPS, winner_item["win_probability"])),
        "brier": brier,
        "top1_hit": 1.0 if int(ranked[0]["horse_no"]) == winner else 0.0,
        "top3_hit": 1.0 if winner in [int(x["horse_no"]) for x in ranked[:3]] else 0.0,
        "winner_probability": winner_item["win_probability"],
        "place_brier": mean(place_errors) if place_errors else None,
        "calibration": calibration,
    }


def summarize_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"race_count": 0}
    calibration = [point for row in rows for point in row["calibration"]]
    place_values = [row["place_brier"] for row in rows if row["place_brier"] is not None]
    summary = {
        "race_count": len(rows),
        "logloss": mean(row["logloss"] for row in rows),
        "brier": mean(row["brier"] for row in rows),
        "top1_accuracy": mean(row["top1_hit"] for row in rows),
        "top3_accuracy": mean(row["top3_hit"] for row in rows),
        "winner_mean_probability": mean(row["winner_probability"] for row in rows),
        "place_brier": mean(place_values) if place_values else None,
    }
    bins = []
    edges = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.000001]
    for lower, upper in zip(edges, edges[1:]):
        points = [point for point in calibration if lower <= point[0] < upper]
        if points:
            bins.append({
                "lower": lower,
                "upper": min(1.0, upper),
                "count": len(points),
                "mean_predicted": mean(point[0] for point in points),
                "observed_win_rate": mean(point[1] for point in points),
            })
    summary["calibration_bins"] = bins
    return summary


def bootstrap_logloss_difference(
    candidate_rows: list[dict], baseline_rows: list[dict], *, samples: int = 1000, seed: int = 20261024
) -> dict:
    """Percentile bootstrap over races for candidate minus baseline log loss."""
    import random

    if not candidate_rows or len(candidate_rows) != len(baseline_rows):
        return {"difference": None, "ci95": [None, None]}
    differences = [c["logloss"] - b["logloss"] for c, b in zip(candidate_rows, baseline_rows)]
    observed = mean(differences)
    if len(differences) == 1 or samples <= 0:
        return {"difference": observed, "ci95": [None, None]}
    rng = random.Random(seed)
    estimates = []
    n = len(differences)
    for _ in range(samples):
        estimates.append(mean(differences[rng.randrange(n)] for _ in range(n)))
    estimates.sort()
    low = estimates[int(0.025 * (len(estimates) - 1))]
    high = estimates[int(0.975 * (len(estimates) - 1))]
    return {
        "difference": observed,
        "ci95": [low, high],
        "interpretation": "負數代表候選模型 log loss 較低；區間跨 0 即改善未明朗。",
    }
