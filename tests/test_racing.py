from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "predict-hk-racing" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from domain import derive_place_slots, quote_supported  # noqa: E402
from decision import payout_odds, settle_stream  # noqa: E402
from staking import edge, kelly_fraction, plan_race, stake_size  # noqa: E402
from racing_model import (  # noqa: E402
    assess_intent,
    bootstrap_logloss_difference,
    expected_calibration_error,
    fit_model,
    information_gain_vs_market,
    isotonic_apply,
    isotonic_fit,
    market_probabilities,
    pair_probabilities,
    place_probabilities,
    platt_apply,
    platt_fit,
    pseudo_r2,
    shin_probabilities,
)
from racing_lab import promotion_eligible  # noqa: E402

CLI = SCRIPTS / "racing_lab.py"


class ModelUnitTests(unittest.TestCase):
    def test_market_probabilities_remove_simple_overround(self):
        runners = [
            {"horse_no": 1, "win_odds": 2.0},
            {"horse_no": 2, "win_odds": 3.0},
            {"horse_no": 3, "win_odds": 3.0},
        ]
        probabilities, complete, overround = market_probabilities(runners)
        self.assertTrue(complete)
        self.assertAlmostEqual(overround, 1 / 6)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0)
        self.assertAlmostEqual(probabilities[1], 3 / 7)

    def test_incomplete_market_returns_uniform_with_flag(self):
        runners = [{"horse_no": 1, "win_odds": 2.0}, {"horse_no": 2, "win_odds": None}]
        probabilities, complete, overround = market_probabilities(runners)
        self.assertFalse(complete)
        self.assertIsNone(overround)
        self.assertEqual(probabilities, {1: 0.5, 2: 0.5})

    def test_place_probabilities_sum_to_slots(self):
        probabilities = [0.4, 0.25, 0.15, 0.1, 0.1]
        for slots in [1, 2, 3]:
            with self.subTest(slots=slots):
                place = place_probabilities(probabilities, slots)
                self.assertAlmostEqual(sum(place), slots)
                self.assertTrue(all(0 < value < 1 for value in place))
                self.assertEqual(place, sorted(place, reverse=True))

    def test_shin_devig_sums_to_one_and_reduces_favourite_bias(self):
        runners = [
            {"horse_no": 1, "win_odds": 1.8},
            {"horse_no": 2, "win_odds": 3.5},
            {"horse_no": 3, "win_odds": 7.0},
            {"horse_no": 4, "win_odds": 10.0},
        ]
        normalized, complete, _ = market_probabilities(runners, method="normalized")
        shin, complete_shin, _ = market_probabilities(runners, method="shin")
        self.assertTrue(complete and complete_shin)
        self.assertAlmostEqual(sum(shin.values()), 1.0, places=9)
        # Shin 修正 longshot bias: 大熱機率調高、冷門調低
        self.assertGreater(shin[1], normalized[1])
        self.assertLess(shin[4], normalized[4])

    def test_shin_handles_fair_book(self):
        fair = shin_probabilities([0.5, 0.25, 0.25])  # book == 1.0
        self.assertAlmostEqual(sum(fair), 1.0, places=9)
        self.assertAlmostEqual(fair[0], 0.5, places=9)

    def test_pair_probabilities_quinella_sums_to_one(self):
        win = [0.4, 0.25, 0.15, 0.1, 0.1]
        pairs = pair_probabilities(win, slots=2)
        self.assertEqual(len(pairs), 10)
        self.assertAlmostEqual(sum(pairs.values()), 1.0, places=9)
        self.assertTrue(all(0.0 <= value <= 1.0 for value in pairs.values()))

    def test_pair_probabilities_place_marginals_match_place_probabilities(self):
        win = [0.4, 0.25, 0.15, 0.1, 0.1]
        pairs = pair_probabilities(win, slots=3)
        place = place_probabilities(win, 3)
        for i in range(len(win)):
            marginal = sum(p for (a, b), p in pairs.items() if a == i or b == i)
            # 每個入三甲的馬會出現在 2 個 pair 之中
            self.assertAlmostEqual(marginal, 2.0 * place[i], places=9)

    def _calibration_sample(self):
        rng = random.Random(7)
        scores, labels, truth = [], [], []
        for _ in range(600):
            true_p = rng.uniform(0.02, 0.98)
            # raw 預測被壓向 0.5（典型未校準模型）
            scores.append(0.5 + 0.45 * (true_p - 0.5))
            labels.append(1 if rng.random() < true_p else 0)
            truth.append(true_p)
        return scores, labels, truth

    @staticmethod
    def _brier(probabilities, labels):
        return sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels)

    def test_isotonic_is_monotone_and_improves_brier(self):
        scores, labels, _ = self._calibration_sample()
        model = isotonic_fit(scores, labels)
        self.assertEqual(model["y"], sorted(model["y"]))
        self.assertEqual(len(model["x"]), len(model["y"]))
        calibrated = [isotonic_apply(model, score) for score in scores]
        self.assertLess(self._brier(calibrated, labels), self._brier(scores, labels))
        # 邊界 clamp，唔會出界
        self.assertAlmostEqual(isotonic_apply(model, -1.0), model["y"][0])
        self.assertAlmostEqual(isotonic_apply(model, 2.0), model["y"][-1])

    def test_platt_scaling_improves_brier(self):
        scores, labels, _ = self._calibration_sample()
        model = platt_fit(scores, labels)
        calibrated = [platt_apply(model, score) for score in scores]
        self.assertLess(self._brier(calibrated, labels), self._brier(scores, labels))
        self.assertTrue(math.isfinite(model["a"]) and math.isfinite(model["b"]))

    def test_expected_calibration_error(self):
        self.assertAlmostEqual(expected_calibration_error([(0.9, 1)] * 90 + [(0.9, 0)] * 10), 0.0)
        self.assertAlmostEqual(expected_calibration_error([(1.0, 0)]), 1.0)
        with self.assertRaises(ValueError):
            expected_calibration_error([])

    def test_pseudo_r2_matches_mcfadden(self):
        rows = [{"logloss": 0.5, "runner_count": 10} for _ in range(5)]
        self.assertAlmostEqual(pseudo_r2(rows), 1.0 - 0.5 / math.log(10))
        self.assertIsNone(pseudo_r2([]))

    def test_information_gain_vs_market_detects_improvement(self):
        market = [{"logloss": 0.60, "runner_count": 10} for _ in range(30)]
        candidate = [{"logloss": 0.55, "runner_count": 10} for _ in range(30)]
        gain = information_gain_vs_market(candidate, market, samples=200)
        self.assertGreater(gain["delta"], 0.0)
        self.assertGreater(gain["ci95"][0], 0.0)
        self.assertEqual(gain["race_count"], 30)
        worse = information_gain_vs_market(market, candidate, samples=200)
        self.assertLess(worse["delta"], 0.0)

    def test_information_gain_handles_mismatch(self):
        gain = information_gain_vs_market([{"logloss": 0.5, "runner_count": 8}], [])
        self.assertIsNone(gain["delta"])
        self.assertEqual(gain["ci95"], [None, None])

    def test_information_gain_matches_pseudo_r2_difference_and_alignment(self):
        market = [
            {"race_id": "A", "logloss": 1.0, "runner_count": 5},
            {"race_id": "B", "logloss": 2.0, "runner_count": 20},
        ]
        candidate = [
            {"race_id": "A", "logloss": 0.8, "runner_count": 5},
            {"race_id": "B", "logloss": 1.9, "runner_count": 20},
        ]
        gain = information_gain_vs_market(candidate, market, samples=0)
        expected = gain["candidate_pseudo_r2"] - gain["market_pseudo_r2"]
        self.assertAlmostEqual(gain["delta"], expected)
        self.assertAlmostEqual(gain["delta"], 0.06514417228548774)

        misaligned = information_gain_vs_market(candidate, list(reversed(market)), samples=0)
        self.assertIsNone(misaligned["delta"])
        self.assertEqual(misaligned["race_count"], 0)
        self.assertEqual(misaligned["ci"], [None, None])

    def test_bootstrap_intervals_preserve_ci95_and_apply_alpha(self):
        market_losses = [0.7, 0.4, 0.8, 0.5, 0.6, 0.9]
        candidate_losses = [0.5, 0.5, 0.6, 0.55, 0.3, 1.1]
        market = [
            {"race_id": str(index), "logloss": loss, "runner_count": 10}
            for index, loss in enumerate(market_losses)
        ]
        candidate = [
            {"race_id": str(index), "logloss": loss, "runner_count": 10}
            for index, loss in enumerate(candidate_losses)
        ]
        for function in (bootstrap_logloss_difference, information_gain_vs_market):
            with self.subTest(function=function.__name__):
                default = function(candidate, market, samples=200)
                adjusted = function(candidate, market, samples=200, alpha=0.025)
                self.assertEqual(default["alpha"], 0.05)
                self.assertEqual(default["ci"], default["ci95"])
                self.assertEqual(adjusted["ci95"], default["ci95"])
                self.assertEqual(adjusted["alpha"], 0.025)
                self.assertLessEqual(adjusted["ci"][0], default["ci"][0])
                self.assertGreaterEqual(adjusted["ci"][1], default["ci"][1])
                with self.assertRaises(ValueError):
                    function(candidate, market, samples=10, alpha=0)
        empty = bootstrap_logloss_difference([], [])
        self.assertEqual(empty["ci95"], [None, None])
        self.assertEqual(empty["ci"], [None, None])

    def test_intent_is_shrunk_and_missing_is_unknown(self):
        missing = assess_intent({"intent_evidence": []})
        self.assertEqual(missing["score"], 50)
        self.assertEqual(missing["label"], "證據不足")
        positive = assess_intent({"intent_evidence": [{
            "signal": "synthetic", "direction": "positive", "strength": 1.0,
            "confidence": 1.0, "source_id": "S1", "note": "synthetic",
        }]})
        self.assertGreater(positive["score"], 50)
        self.assertLess(positive["score"], 100)
        self.assertLess(positive["confidence"], 1.0)


class PromotionGateUnitTests(unittest.TestCase):
    @staticmethod
    def eligible_gate():
        return {
            "min_test_races_required": 2,
            "enough_test_races": True,
            "logloss_beats_market_baseline": True,
            "brier_not_worse_than_market": True,
            "logloss_improvement_ci95_excludes_zero": True,
            "logloss_improvement_adjusted_ci_excludes_zero": True,
            "champion_comparison_required": True,
            "champion_improvement_ci95_excludes_zero": True,
            "champion_improvement_adjusted_ci_excludes_zero": True,
            "beats_current_champion": True,
            "information_gain_required": True,
            "information_gain_positive": True,
            "information_gain_ci95_excludes_zero": True,
            "information_gain_adjusted_ci_excludes_zero": True,
            "block_ablation_required": True,
            "block_ablation_recorded": True,
            "block_ablation_ci95_excludes_zero": True,
            "block_ablation_adjusted_ci_excludes_zero": True,
            "candidates_backtested": 1,
            "candidates_in_family": 1,
            "familywise_alpha": 0.05,
            "effective_alpha": 0.05,
            "multiple_comparison_method": "bonferroni",
            "multiple_comparison_adjusted": True,
            "multiple_comparison_stale": False,
        }

    def test_complete_significance_evidence_is_eligible(self):
        gate = self.eligible_gate()
        self.assertTrue(promotion_eligible(gate))
        gate["candidates_in_family"] = 2
        gate["effective_alpha"] = 0.025
        self.assertTrue(promotion_eligible(gate))

    def test_ablation_ci_crossing_zero_is_not_eligible(self):
        gate = self.eligible_gate()
        gate["block_ablation_ci95_excludes_zero"] = False
        self.assertFalse(promotion_eligible(gate))

    def test_positive_information_gain_with_ci_crossing_zero_is_not_eligible(self):
        gate = self.eligible_gate()
        self.assertTrue(gate["information_gain_positive"])
        gate["information_gain_ci95_excludes_zero"] = False
        self.assertFalse(promotion_eligible(gate))

    def test_multiple_comparison_adjustment_is_enforced(self):
        gate = self.eligible_gate()
        gate["multiple_comparison_adjusted"] = False
        self.assertFalse(promotion_eligible(gate))

        gate = self.eligible_gate()
        gate["logloss_improvement_adjusted_ci_excludes_zero"] = False
        self.assertFalse(promotion_eligible(gate))

        gate = self.eligible_gate()
        gate["champion_improvement_adjusted_ci_excludes_zero"] = False
        self.assertFalse(promotion_eligible(gate))

    def test_legacy_stale_or_wrong_alpha_gate_cannot_promote(self):
        gate = self.eligible_gate()
        del gate["multiple_comparison_adjusted"]
        self.assertFalse(promotion_eligible(gate))

        gate = self.eligible_gate()
        gate["multiple_comparison_stale"] = True
        self.assertFalse(promotion_eligible(gate))

        gate = self.eligible_gate()
        gate["candidates_in_family"] = 2
        gate["effective_alpha"] = 0.05
        self.assertFalse(promotion_eligible(gate))


class StakingUnitTests(unittest.TestCase):
    def test_edge_and_full_kelly_known_values(self):
        self.assertAlmostEqual(edge(0.2, 6.0), 0.2)
        self.assertAlmostEqual(kelly_fraction(0.2, 6.0), 0.04)
        self.assertEqual(kelly_fraction(0.1, 6.0), 0.0)  # 負 edge → 0
        self.assertIsNone(edge(0.0, 6.0))
        self.assertIsNone(edge(0.5, 1.0))

    def test_stake_skips_below_min_edge(self):
        outcome = stake_size(0.15, 6.0, 10_000)
        self.assertEqual(outcome["status"], "skip")
        self.assertEqual(outcome["reason"], "edge_below_threshold")
        self.assertEqual(outcome["stake"], 0.0)

    def test_fractional_kelly_applied(self):
        outcome = stake_size(0.2, 6.0, 10_000, kelly_multiplier=0.25)
        # full Kelly 4% × 0.25 = 1% of bankroll = 100
        self.assertEqual(outcome["status"], "bet")
        self.assertAlmostEqual(outcome["full_kelly"], 0.04)
        self.assertAlmostEqual(outcome["stake"], 100.0)

    def test_max_bankroll_fraction_caps_stake(self):
        outcome = stake_size(0.5, 3.0, 10_000, kelly_multiplier=1.0, max_bankroll_fraction=0.005)
        self.assertEqual(outcome["status"], "bet")
        self.assertLessEqual(outcome["stake"], 50.0)

    def test_pool_cap_limits_stake(self):
        outcome = stake_size(
            0.4, 4.0, 1_000_000, kelly_multiplier=1.0, pool_size=50_000, max_pool_fraction=0.001
        )
        self.assertAlmostEqual(outcome["caps"]["pool"], 50.0)
        self.assertAlmostEqual(outcome["stake"], 50.0)

    def test_stake_floored_to_unit(self):
        outcome = stake_size(0.2, 6.0, 9_700, kelly_multiplier=0.25, unit=10.0)
        self.assertEqual(outcome["stake"] % 10, 0)
        self.assertLessEqual(outcome["stake"], 97)

    def test_minimum_stake_and_invalid_bankroll(self):
        tiny = stake_size(0.2, 6.0, 100, kelly_multiplier=0.25, unit=10.0)
        self.assertEqual(tiny["status"], "skip")
        self.assertEqual(tiny["reason"], "stake_below_minimum")
        bad = stake_size(0.2, 6.0, 0)
        self.assertEqual(bad["reason"], "invalid_bankroll")

    def test_plan_race_scales_to_total_exposure_cap(self):
        bets = [
            {"probability": 0.3, "decimal_odds": 4.0, "horse_no": number}
            for number in (1, 2, 3)
        ]
        plan = plan_race(bets, 10_000, max_race_fraction=0.02, kelly_multiplier=1.0)
        self.assertTrue(plan["scaled_to_race_cap"])
        self.assertLessEqual(plan["total_stake"], plan["race_cap"])
        self.assertEqual(plan["bet_count"], 3)
        self.assertTrue(all(item["stake"] % 10 == 0 for item in plan["bets"]))

    def test_plan_race_preserves_bookkeeping(self):
        plan = plan_race([{"probability": 0.3, "decimal_odds": 4.0, "horse_no": 7}], 10_000)
        self.assertEqual(plan["bets"][0]["horse_no"], 7)
        self.assertEqual(plan["bets"][0]["status"], "bet")


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        for folder in ["cards", "results", "predictions", "models", "evaluations"]:
            (self.base / folder).mkdir()
        self.db = self.base / "racing.db"
        self.rng = random.Random(20261024)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, ok=True):
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
        completed = subprocess.run(
            [sys.executable, str(CLI), *map(str, args)],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=240,
            env=env,
        )
        if ok and completed.returncode != 0:
            self.fail(f"Command failed: {args}\nSTDOUT={completed.stdout}\nSTDERR={completed.stderr}")
        if not ok and completed.returncode == 0:
            self.fail(f"Command unexpectedly passed: {args}\n{completed.stdout}")
        return completed

    @staticmethod
    def dump(path: Path, payload: dict):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def make_card(self, date: str, race_no=1, live=False):
        off = f"{date}T13:00:00+08:00"
        captured = f"{date}T09:00:00+08:00"
        runners = []
        strengths = []
        for number in range(1, 6):
            rating = 55 + number * 5 + self.rng.randrange(-2, 3)
            intent = (number - 3) / 3
            strengths.append(math.exp(0.055 * rating + 0.25 * intent))
            evidence = []
            if number != 1:
                evidence.append({
                    "signal": "synthetic_deployment_observation",
                    "direction": "positive" if intent >= 0 else "negative",
                    "strength": 0.6,
                    "confidence": 0.7,
                    "source_id": "S1",
                    "note": "純虛構測試證據，只供驗證程式。",
                })
            runners.append({
                "horse_no": number,
                "horse_name": f"虛構馬{date}-{number}",
                "jockey": f"騎師{number}",
                "trainer": f"練馬師{(number % 2) + 1}",
                "draw": number,
                "weight_lb": 116 + number * 2,
                "rating": rating,
                "scratched": False,
                "features": {
                    "cd_place_rate": min(0.9, 0.08 + 0.1 * number),
                    "recent_finish_mean": 8.0 - number,
                    "days_since_run": 14 + number * 7,
                },
                "intent_evidence": evidence,
            })
        total = sum(strengths)
        probabilities = [value / total for value in strengths]
        for runner, probability in zip(runners, probabilities):
            runner["win_odds"] = round(1 / (probability * 1.16), 3)
            runner["place_odds"] = None
        return {
            "schema_version": "1.0",
            "collection_mode": "live" if live else "historical",
            "captured_at": captured,
            "data_cutoff": captured,
            "race": {
                "race_id": f"{date}-ST-R{race_no}",
                "meeting_date": date,
                "venue": "ST",
                "race_no": race_no,
                "off_time": off,
                "distance_m": 1200,
                "surface": "turf",
                "going": "good",
                "class_name": "Synthetic",
                "place_slots": 3,
            },
            "sources": [{
                "source_id": "S1",
                "title": "虛構測試資料",
                "url": "https://example.invalid/synthetic",
                "retrieved_at": captured,
                "official": False,
            }],
            "runners": runners,
        }, probabilities

    def test_fit_model_early_stopping_holds_out_last_races(self):
        records = []
        for offset in range(12):
            date = f"2025-01-{offset + 1:02d}"
            card, probabilities = self.make_card(date, race_no=(offset % 5) + 1)
            winner = max(range(len(probabilities)), key=lambda i: probabilities[i]) + 1
            results = [
                {
                    "horse_no": number,
                    "finish_position": 1 if number == winner else number + 1,
                    "status": "OK",
                }
                for number in range(1, len(probabilities) + 1)
            ]
            records.append({"card": card, "result": {"results": results}})

        features = ["cd_place_rate", "recent_finish_mean", "days_since_run", "market_logp"]
        model = fit_model(
            records,
            features,
            epochs=60,
            validation_fraction=0.25,
            early_stopping_patience=5,
        )
        training = model["training"]
        self.assertEqual(training["validation_races"], 3)
        self.assertIsNotNone(training["validation_logloss"])
        self.assertLessEqual(training["best_epoch"], 60)
        self.assertGreaterEqual(training["best_epoch"], 20)
        self.assertTrue(all(math.isfinite(value) for value in model["coefficients"].values()))

    def test_fit_model_defaults_unchanged(self):
        records = []
        for offset in range(6):
            card, probabilities = self.make_card(f"2025-02-{offset + 1:02d}", race_no=1)
            winner = max(range(len(probabilities)), key=lambda i: probabilities[i]) + 1
            results = [
                {
                    "horse_no": number,
                    "finish_position": 1 if number == winner else number + 1,
                    "status": "OK",
                }
                for number in range(1, len(probabilities) + 1)
            ]
            records.append({"card": card, "result": {"results": results}})
        model = fit_model(records, ["cd_place_rate", "recent_finish_mean"], epochs=5)
        self.assertEqual(model["training"]["validation_races"], 0)
        self.assertIsNone(model["training"]["validation_logloss"])
        self.assertFalse(model["training"]["stopped_early"])
        self.assertEqual(model["training"]["best_epoch"], 5)

    def test_end_to_end_and_safety_guards(self):
        self.run_cli("init", "--db", self.db)
        for index in range(8):
            date = f"2026-10-{index + 1:02d}"
            card, probabilities = self.make_card(date)
            card_path = self.base / "cards" / f"{index}.json"
            self.dump(card_path, card)
            self.run_cli("import-card", "--db", self.db, "--file", card_path)
            winner = self.rng.choices(
                [runner["horse_no"] for runner in card["runners"]], weights=probabilities, k=1
            )[0]
            remaining = [runner["horse_no"] for runner in card["runners"] if runner["horse_no"] != winner]
            self.rng.shuffle(remaining)
            order = [winner] + remaining
            result = {
                "schema_version": "1.0",
                "race_id": card["race"]["race_id"],
                "recorded_at": f"{date}T14:00:00+08:00",
                "sources": [{"source_id": "R1", "title": "虛構測試賽果"}],
                "results": [
                    {"horse_no": number, "finish_position": position, "status": "OK"}
                    for position, number in enumerate(order, start=1)
                ],
            }
            result_path = self.base / "results" / f"{index}.json"
            self.dump(result_path, result)
            self.run_cli("import-result", "--db", self.db, "--file", result_path)

        live, _ = self.make_card("2099-01-01", live=True)
        live_path = self.base / "cards" / "live.json"
        self.dump(live_path, live)
        self.run_cli("import-card", "--db", self.db, "--file", live_path)
        prediction_path = self.base / "predictions" / "live.json"
        self.run_cli(
            "predict", "--db", self.db, "--race-id", live["race"]["race_id"],
            "--output", prediction_path,
        )
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        self.assertEqual(prediction["model_version"], "market-baseline")
        self.assertAlmostEqual(sum(item["win_probability"] for item in prediction["runners"]), 1.0)
        self.assertAlmostEqual(sum(item["place_probability"] for item in prediction["runners"]), 3.0)
        self.assertTrue(any(item["intent"]["score"] == 50 for item in prediction["runners"]))

        order = [runner["horse_no"] for runner in live["runners"]]
        live_result = {
            "schema_version": "1.0",
            "race_id": live["race"]["race_id"],
            "recorded_at": "2099-01-01T14:00:00+08:00",
            "sources": [{"source_id": "R1", "title": "虛構測試賽果"}],
            "results": [
                {"horse_no": number, "finish_position": position, "status": "OK"}
                for position, number in enumerate(order, start=1)
            ],
        }
        live_result_path = self.base / "results" / "live.json"
        self.dump(live_result_path, live_result)
        self.run_cli("import-result", "--db", self.db, "--file", live_result_path)
        self.run_cli(
            "predict", "--db", self.db, "--race-id", live["race"]["race_id"],
            "--historical", ok=False,
        )
        evaluation_path = self.base / "evaluations" / "live.json"
        self.run_cli("evaluate", "--db", self.db, "--output", evaluation_path)
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        self.assertEqual(evaluation["models"]["market-baseline"]["race_count"], 1)

        model_path = self.base / "models" / "candidate.json"
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "candidate-v1",
            "--features", "market_logp,intent_centered,rating,draw,cd_place_rate,recent_finish_mean",
            "--train-before", "2026-10-09", "--min-races", 8, "--epochs", 120,
            "--output", model_path,
        )
        model = json.loads(model_path.read_text(encoding="utf-8"))
        self.assertEqual(model["training"]["race_count"], 8)
        self.assertTrue(all(math.isfinite(value) for value in model["coefficients"].values()))
        backtest_path = self.base / "evaluations" / "backtest.json"
        self.run_cli(
            "backtest", "--db", self.db, "--model-version", "candidate-v1",
            "--min-train-races", 3, "--min-test-races", 50, "--refit-every", 2,
            "--test-before", "2026-10-09", "--bootstrap", 200, "--output", backtest_path,
        )
        backtest = json.loads(backtest_path.read_text(encoding="utf-8"))
        self.assertEqual(backtest["candidate"]["race_count"], 5)
        self.assertEqual(backtest["market_baseline"]["race_count"], 5)
        self.assertFalse(backtest["promotion_gate"]["eligible"])
        promotion = self.run_cli("promote", "--db", self.db, "--model-version", "candidate-v1")
        self.assertEqual(json.loads(promotion.stdout)["event"], "rejected")

        # Exercise champion comparison without claiming this synthetic model is genuinely promoted.
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "INSERT INTO model_events VALUES (?, ?, ?, ?, ?)",
                ("synthetic_champion", "candidate-v1", "promoted", "2099-01-01T00:00:00+00:00", "{}"),
            )
            connection.commit()
        finally:
            connection.close()
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "candidate-v2",
            "--features", "market_logp,rating", "--train-before", "2026-10-09",
            "--min-races", 8, "--epochs", 80,
        )
        champion_path = self.base / "evaluations" / "champion.json"
        self.run_cli(
            "backtest", "--db", self.db, "--model-version", "candidate-v2",
            "--min-train-races", 3, "--min-test-races", 50, "--refit-every", 2,
            "--test-before", "2026-10-09", "--bootstrap", 100, "--output", champion_path,
        )
        champion = json.loads(champion_path.read_text(encoding="utf-8"))
        self.assertEqual(champion["champion_version"], "candidate-v1")
        self.assertEqual(champion["champion"]["race_count"], 5)
        self.assertIsNotNone(champion["candidate_minus_champion_logloss"])

        connection = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.Error):
                connection.execute("DELETE FROM prediction_runs")
        finally:
            connection.close()

        bad, _ = self.make_card("2099-01-02", live=True)
        bad["data_cutoff"] = "2099-01-02T14:00:00+08:00"
        bad_path = self.base / "cards" / "bad.json"
        self.dump(bad_path, bad)
        self.run_cli("import-card", "--db", self.db, "--file", bad_path, ok=False)

        bad_time, _ = self.make_card("2099-01-03", live=True)
        bad_time["data_cutoff"] = "2099-01-03T10:00:00+08:00"
        bad_time_path = self.base / "cards" / "bad-time.json"
        self.dump(bad_time_path, bad_time)
        self.run_cli("import-card", "--db", self.db, "--file", bad_time_path, ok=False)

    def v11_card(self, card: dict) -> dict:
        """Turn a 1.0 fixture card into a 1.1 card with quotes and an excerpt."""
        card = json.loads(json.dumps(card))
        card["schema_version"] = "1.1"
        card["race"]["declared_starters"] = len(card["runners"])
        card["race"].pop("place_slots", None)
        card["sources"][0]["url"] = "https://example.invalid/synthetic"
        card["sources"][0]["excerpt"] = "練馬師表示，今仗以爭勝為目標，並會交由主帥策騎。"
        for runner in card["runners"]:
            for item in runner["intent_evidence"]:
                item["quote"] = "今仗以爭勝為目標"
        return card

    def test_unknown_feature_is_rejected_unless_opted_in(self):
        card, _ = self.make_card("2026-10-01")
        card["runners"][0]["features"]["won_last_start"] = 1
        path = self.base / "cards" / "unknown.json"
        self.dump(path, card)
        rejected = self.run_cli("import-card", "--db", self.db, "--file", path, ok=False)
        self.assertIn("Unregistered feature", rejected.stderr)
        accepted = self.run_cli(
            "import-card", "--db", self.db, "--file", path, "--allow-unknown-features"
        )
        self.assertFalse(json.loads(accepted.stdout)["duplicate"])

    def test_schema_1_1_requires_quote_that_matches_excerpt(self):
        base_card, _ = self.make_card("2026-10-02")
        good = self.v11_card(base_card)

        missing = json.loads(json.dumps(good))
        missing["runners"][1]["intent_evidence"][0].pop("quote")
        missing_path = self.base / "cards" / "missing-quote.json"
        self.dump(missing_path, missing)
        rejected = self.run_cli("import-card", "--db", self.db, "--file", missing_path, ok=False)
        self.assertIn("verbatim quote", rejected.stderr)

        mismatch = json.loads(json.dumps(good))
        mismatch["runners"][1]["intent_evidence"][0]["quote"] = "完全無關嘅一句說話"
        mismatch_path = self.base / "cards" / "mismatch-quote.json"
        self.dump(mismatch_path, mismatch)
        rejected = self.run_cli("import-card", "--db", self.db, "--file", mismatch_path, ok=False)
        self.assertIn("not found in the excerpt", rejected.stderr)

        good_path = self.base / "cards" / "good-quote.json"
        self.dump(good_path, good)
        accepted = self.run_cli("import-card", "--db", self.db, "--file", good_path)
        self.assertFalse(json.loads(accepted.stdout)["duplicate"])

    def test_evaluate_scores_pre_race_predictions_only(self):
        card, _ = self.make_card("2026-01-05")
        card_path = self.base / "cards" / "recon.json"
        self.dump(card_path, card)
        self.run_cli("import-card", "--db", self.db, "--file", card_path)
        # A reconstruction created after the race must not enter the KPI.
        self.run_cli(
            "predict", "--db", self.db, "--race-id", card["race"]["race_id"], "--historical"
        )
        order = [runner["horse_no"] for runner in card["runners"]]
        result = {
            "schema_version": "1.0",
            "race_id": card["race"]["race_id"],
            "recorded_at": "2026-01-05T14:00:00+08:00",
            "sources": [{"source_id": "R1", "title": "虛構測試賽果"}],
            "results": [
                {"horse_no": number, "finish_position": position, "status": "OK"}
                for position, number in enumerate(order, start=1)
            ],
        }
        result_path = self.base / "results" / "recon.json"
        self.dump(result_path, result)
        self.run_cli("import-result", "--db", self.db, "--file", result_path)

        report = json.loads(self.run_cli("evaluate", "--db", self.db).stdout)
        self.assertEqual(report["excluded_by_forecast_kind"], {"historical-reconstruction": 1})
        self.assertEqual(report["models"], {})

        legacy = json.loads(
            self.run_cli("evaluate", "--db", self.db, "--all-kinds").stdout
        )
        self.assertEqual(legacy["models"]["market-baseline"]["race_count"], 1)

    def test_place_slots_follow_declared_field_size(self):
        card, _ = self.make_card("2026-10-03")
        card["race"]["declared_starters"] = 5
        card["race"].pop("place_slots", None)
        card_path = self.base / "cards" / "small-field.json"
        self.dump(card_path, card)
        imported = json.loads(
            self.run_cli("import-card", "--db", self.db, "--file", card_path).stdout
        )
        self.assertFalse(any("place_slots" in w for w in imported["warnings"]))
        prediction = json.loads(
            self.run_cli(
                "predict", "--db", self.db, "--race-id", card["race"]["race_id"]
            ).stdout
        )
        self.assertEqual(prediction["place_slots"], 2)
        self.assertAlmostEqual(
            sum(item["place_probability"] for item in prediction["runners"]), 2.0
        )

        mismatched, _ = self.make_card("2026-10-04")
        mismatched["race"]["declared_starters"] = 5
        mismatch_path = self.base / "cards" / "slot-mismatch.json"
        self.dump(mismatch_path, mismatched)
        warnings = json.loads(
            self.run_cli("import-card", "--db", self.db, "--file", mismatch_path).stdout
        )["warnings"]
        self.assertTrue(any("place_slots=3" in warning for warning in warnings))

    def make_result(self, card, date, winner, dividend=None):
        remaining = [r["horse_no"] for r in card["runners"] if r["horse_no"] != winner]
        self.rng.shuffle(remaining)
        order = [winner] + remaining
        return {
            "schema_version": "1.0",
            "race_id": card["race"]["race_id"],
            "recorded_at": f"{date}T14:00:00+08:00",
            "sources": [{"source_id": "R1", "title": "虛構測試賽果"}],
            "results": [
                {
                    "horse_no": number,
                    "finish_position": position,
                    "status": "OK",
                    "win_dividend": dividend if number == winner else None,
                }
                for position, number in enumerate(order, start=1)
            ],
        }

    def build_history(self, start_day, races, dividends=True):
        """Import cards + results and return the date range used."""
        dates = []
        for index in range(races):
            date = f"2026-08-{start_day + index:02d}"
            dates.append(date)
            card, probabilities = self.make_card(date)
            card_path = self.base / "cards" / f"v{index}.json"
            self.dump(card_path, card)
            self.run_cli("import-card", "--db", self.db, "--file", card_path)
            winner = self.rng.choices(
                [r["horse_no"] for r in card["runners"]], weights=probabilities, k=1
            )[0]
            odds = next(r["win_odds"] for r in card["runners"] if r["horse_no"] == winner)
            result = self.make_result(
                card, date, winner, dividend=round(odds * 10, 2) if dividends else None
            )
            result_path = self.base / "results" / f"v{index}.json"
            self.dump(result_path, result)
            self.run_cli("import-result", "--db", self.db, "--file", result_path)
        return dates

    def test_value_backtest_settles_against_dividends(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 8)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "value-v1",
            "--features", "market_logp,rating", "--train-before", "2026-08-08",
            "--min-races", 6, "--epochs", 60,
        )
        settlement = json.loads(
            self.run_cli(
                "value-backtest", "--db", self.db, "--model-version", "value-v1",
                "--stake", "flat", "--cap", 0.02, "--min-edge", -5,
                "--min-train-races", 4, "--refit-every", 2,
                "--test-after", "2026-08-05",
            ).stdout
        )["settlement"]
        self.assertGreaterEqual(settlement["bets"], 1)
        self.assertGreater(settlement["turnover"], 0)
        self.assertAlmostEqual(
            settlement["bankroll_end"],
            settlement["bankroll_start"] + settlement["profit"],
            places=4,
        )
        self.assertIsNotNone(settlement["roi"])

        kelly_report = json.loads(
            self.run_cli(
                "value-backtest", "--db", self.db, "--model-version", "value-v1",
                "--stake", "kelly", "--cap", 0.02, "--min-edge", -5,
                "--kelly-multiplier", 0.2, "--max-race-fraction", 0.03,
                "--pool-size", 1_000_000, "--stake-unit", 1,
                "--min-train-races", 4, "--refit-every", 2,
                "--test-after", "2026-08-05",
            ).stdout
        )
        kelly_settlement = kelly_report["settlement"]
        self.assertEqual(kelly_settlement["kelly_multiplier"], 0.2)
        self.assertEqual(kelly_settlement["max_race_fraction"], 0.03)
        self.assertEqual(kelly_settlement["max_pool_fraction"], 0.001)
        self.assertEqual(kelly_report["method"]["pool_size_assumption"], 1_000_000)
        self.assertEqual(kelly_report["method"]["stake_unit"], 1)

    def test_evaluate_reports_selective_use_bands(self):
        self.run_cli("init", "--db", self.db)
        for index in range(4):
            date = f"2026-09-{index + 1:02d}"
            card, probabilities = self.make_card(date)
            card_path = self.base / "cards" / f"s{index}.json"
            self.dump(card_path, card)
            self.run_cli("import-card", "--db", self.db, "--file", card_path)
            self.run_cli("predict", "--db", self.db, "--race-id", card["race"]["race_id"])
            winner = self.rng.choices(
                [r["horse_no"] for r in card["runners"]], weights=probabilities, k=1
            )[0]
            result_path = self.base / "results" / f"s{index}.json"
            self.dump(result_path, self.make_result(card, date, winner, dividend=20.0))
            self.run_cli("import-result", "--db", self.db, "--file", result_path)
        # These historical fixtures are reconstructions; opt in without weakening the live KPI default.
        report = json.loads(
            self.run_cli("evaluate", "--db", self.db, "--all-kinds", "--coverage", "50").stdout
        )
        bands = report["models"]["market-baseline"]["selective_use"]
        self.assertEqual(bands[0]["coverage_pct"], 50)
        self.assertEqual(bands[0]["race_count"], 2)

    def test_block_ablation_is_recorded_and_gates_promotion(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 10)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "abl-v1",
            "--features", "market_logp,intent_centered,rating",
            "--block", "intent=intent_centered",
            "--train-before", "2026-08-08", "--min-races", 6, "--epochs", 40,
        )
        with_ablation = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "abl-v1",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-06", "--bootstrap", 50, "--ablate", "intent",
            ).stdout
        )
        self.assertEqual(with_ablation["ablation"]["block"], "intent")
        gate = with_ablation["promotion_gate"]
        self.assertTrue(gate["block_ablation_recorded"])
        self.assertEqual(gate["candidates_in_family"], 1)
        self.assertEqual(gate["candidates_backtested"], 1)
        self.assertEqual(gate["familywise_alpha"], 0.05)
        self.assertEqual(gate["effective_alpha"], 0.05)
        self.assertEqual(gate["multiple_comparison_method"], "bonferroni")
        self.assertFalse(gate["multiple_comparison_stale"])
        ablation_comparison = with_ablation["ablation"]["candidate_minus_without_block"]
        self.assertEqual(ablation_comparison["alpha"], 0.05)
        self.assertEqual(ablation_comparison["ci"], ablation_comparison["ci95"])
        self.assertEqual(
            gate["block_ablation_ci95_excludes_zero"],
            ablation_comparison["ci95"][1] is not None and ablation_comparison["ci95"][1] < 0,
        )
        self.assertEqual(
            gate["block_ablation_adjusted_ci_excludes_zero"],
            ablation_comparison["ci"][1] is not None and ablation_comparison["ci"][1] < 0,
        )

        self.run_cli(
            "fit", "--db", self.db, "--model-version", "abl-v2",
            "--features", "market_logp,intent_centered,rating",
            "--block", "intent=intent_centered",
            "--train-before", "2026-08-08", "--min-races", 6, "--epochs", 20,
        )
        without_ablation = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "abl-v2",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-06", "--bootstrap", 50,
            ).stdout
        )
        second_gate = without_ablation["promotion_gate"]
        self.assertEqual(second_gate["candidates_in_family"], 2)
        self.assertEqual(second_gate["effective_alpha"], 0.025)
        self.assertEqual(without_ablation["candidate_minus_baseline_logloss"]["alpha"], 0.025)
        self.assertEqual(without_ablation["information_gain_vs_market"]["alpha"], 0.025)
        promotion = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "abl-v2").stdout
        )
        self.assertEqual(promotion["event"], "rejected")
        self.assertTrue(promotion["promotion_gate"]["block_ablation_missing"])
        self.assertEqual(promotion["promotion_gate"]["candidates_backtested"], 2)
        self.assertFalse(promotion["promotion_gate"]["multiple_comparison_stale"])

        stale = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "abl-v1").stdout
        )
        self.assertEqual(stale["event"], "rejected")
        self.assertTrue(stale["promotion_gate"]["multiple_comparison_stale"])
        self.assertEqual(stale["promotion_gate"]["candidates_in_family"], 1)
        self.assertEqual(stale["promotion_gate"]["current_candidates_in_family"], 2)
        self.assertEqual(stale["promotion_gate"]["effective_alpha"], 0.05)
        self.assertEqual(stale["promotion_gate"]["current_effective_alpha"], 0.025)
        self.assertFalse(stale["promotion_gate"]["multiple_comparison_adjusted"])

        bypass = self.run_cli(
            "promote", "--db", self.db, "--model-version", "abl-v2",
            "--allow-missing-ablation", ok=False,
        )
        self.assertIn("unrecognized arguments", bypass.stderr)


class DomainRuleTests(unittest.TestCase):
    def test_place_slots_follow_hkjc_terms(self):
        self.assertEqual(derive_place_slots(14), 3)
        self.assertEqual(derive_place_slots(8), 3)
        self.assertEqual(derive_place_slots(7), 3)
        self.assertEqual(derive_place_slots(6), 2)
        self.assertEqual(derive_place_slots(4), 2)
        self.assertEqual(derive_place_slots(3), 1)

    def test_quote_matching_folds_width_and_punctuation(self):
        excerpt = "練馬師表示，今仗以爭勝為目標！"
        self.assertTrue(quote_supported("今仗以爭勝為目標", excerpt))
        self.assertTrue(quote_supported("練馬師表示 今仗以爭勝為目標", excerpt))
        self.assertFalse(quote_supported("今仗只係熱身", excerpt))
        self.assertFalse(quote_supported("爭勝", excerpt))


class DecisionUnitTests(unittest.TestCase):
    @staticmethod
    def settlement_stream(runners):
        prediction = {"race_id": "R1", "runners": runners}
        record = {
            "result": {
                "results": [
                    {"horse_no": 1, "finish_position": 1, "win_dividend": 60.0}
                ]
            }
        }
        return [(record, prediction)]

    def test_kelly_fraction_uses_shared_two_argument_api(self):
        self.assertAlmostEqual(kelly_fraction(0.5, 3.0), 0.25)
        self.assertAlmostEqual(kelly_fraction(0.45, 2.5), 0.125 / 1.5, places=9)
        self.assertEqual(kelly_fraction(0.2, 3.0), 0.0)
        self.assertEqual(kelly_fraction(0.9, 1.0), 0.0)

    def test_kelly_settlement_matches_shared_stake_size(self):
        runners = [{"horse_no": 1, "win_probability": 0.2, "win_odds_snapshot": 6.0}]
        settlement = settle_stream(
            self.settlement_stream(runners), stake_mode="kelly", cap=0.02,
            bankroll=10_000, min_edge=0.02, kelly_multiplier=0.25,
        )
        expected = stake_size(
            0.2, 6.0, 10_000, kelly_multiplier=0.25,
            min_edge=0.02, max_bankroll_fraction=0.02,
        )
        self.assertEqual(expected["status"], "bet")
        self.assertEqual(settlement["detail"][0]["stake"], expected["stake"])

    def test_kelly_settlement_enforces_race_cap_for_two_bets(self):
        runners = [
            {"horse_no": number, "win_probability": 0.3, "win_odds_snapshot": 4.0}
            for number in (1, 2)
        ]
        settlement = settle_stream(
            self.settlement_stream(runners), stake_mode="kelly", cap=0.05,
            bankroll=10_000, min_edge=0.02, kelly_multiplier=1.0,
            max_race_fraction=0.02,
        )
        self.assertEqual(settlement["bets"], 2)
        total_stake = sum(item["stake"] for item in settlement["detail"])
        self.assertLessEqual(total_stake, 0.02 * 10_000)

    def test_kelly_settlement_enforces_pool_cap(self):
        runners = [{"horse_no": 1, "win_probability": 0.4, "win_odds_snapshot": 4.0}]
        settlement = settle_stream(
            self.settlement_stream(runners), stake_mode="kelly", cap=0.05,
            bankroll=1_000_000, min_edge=0.02, kelly_multiplier=1.0,
            pool_size=50_000,
        )
        self.assertEqual(settlement["bets"], 1)
        self.assertLessEqual(settlement["detail"][0]["stake"], 50_000 * 0.001)

    def test_fractional_caps_never_round_up(self):
        runners = [{"horse_no": 1, "win_probability": 0.4, "win_odds_snapshot": 4.0}]
        expected = stake_size(
            0.4, 4.0, 1_000_000, kelly_multiplier=1.0, min_edge=0.02,
            max_bankroll_fraction=0.05, pool_size=50_005, unit=0,
        )
        settlement = settle_stream(
            self.settlement_stream(runners), stake_mode="kelly", cap=0.05,
            bankroll=1_000_000, min_edge=0.02, kelly_multiplier=1.0,
            pool_size=50_005, unit=0,
        )
        self.assertEqual(settlement["detail"][0]["stake"], expected["stake"])
        self.assertLessEqual(settlement["detail"][0]["stake"], 50_005 * 0.001)

        plan = plan_race(
            [{"probability": 0.5, "decimal_odds": 3.0}], 10_000,
            max_race_fraction=0.0100005, kelly_multiplier=1.0,
            max_bankroll_fraction=1.0, unit=0,
        )
        self.assertLessEqual(plan["total_stake"], plan["race_cap"])
        self.assertLessEqual(plan["bets"][0]["stake"], plan["race_cap"])

    def test_race_cap_never_exceeds_bankroll(self):
        bets = [
            {"probability": 0.9, "decimal_odds": 2.0, "horse_no": number}
            for number in (1, 2, 3)
        ]
        plan = plan_race(
            bets, 1_000, max_race_fraction=2.0,
            kelly_multiplier=1.0, max_bankroll_fraction=1.0,
        )
        self.assertLessEqual(plan["total_stake"], 1_000)

    def test_payout_odds_uses_hk_dividend(self):
        self.assertAlmostEqual(payout_odds(42.5, 3.1), 4.25)
        self.assertAlmostEqual(payout_odds(None, 3.1), 3.1)

    def test_coverage_slices_handles_empty_input(self):
        from decision import coverage_slices
        self.assertEqual(coverage_slices([], [50]), [])


if __name__ == "__main__":
    unittest.main()
