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
from contextlib import closing
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "predict-hk-racing" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from domain import derive_place_slots, quote_supported  # noqa: E402
from decision import payout_odds, settle_stream  # noqa: E402
from staking import edge, kelly_fraction, plan_race, stake_size  # noqa: E402
from racing_model import (  # noqa: E402
    analyze_card,
    apply_calibration,
    assess_intent,
    bootstrap_logloss_difference,
    expected_calibration_error,
    fit_model,
    information_gain_vs_uniform,
    isotonic_apply,
    isotonic_fit,
    metric_row,
    pair_probabilities,
    place_probabilities,
    platt_apply,
    platt_fit,
    predict_card,
    pseudo_r2,
    uniform_baseline_card,
)
from racing_lab import payload_hash, promotion_eligible  # noqa: E402

CLI = SCRIPTS / "racing_lab.py"


class ModelUnitTests(unittest.TestCase):
    def test_analysis_preserves_evidence_without_inventing_probabilities(self):
        card = {
            "data_cutoff": "2026-10-28T09:00:00+08:00",
            "race": {"race_id": "synthetic-intelligence", "place_slots": 3},
            "sources": [{
                "source_id": "S1", "title": "虛構部署原文",
                "url": "https://example.invalid/intelligence",
                "retrieved_at": "2026-10-28T09:00:00+08:00",
            }],
            "runners": [{
                "horse_no": 1, "horse_name": "甲", "win_odds": 4.2, "place_odds": 1.8,
                "features": {"rating": 80},
                "intent_evidence": [
                    {"signal": "deployment", "direction": "positive", "source_id": "S1",
                     "quote": "今仗以爭勝為目標"},
                    {"signal": "counter_evidence", "direction": "negative", "source_id": "S1",
                     "quote": "上仗後曾有小毛病"},
                ],
            }, {
                "horse_no": 2, "horse_name": "乙", "win_odds": 6.0,
                "intent_evidence": [{"direction": "neutral", "source_id": "S1", "quote": "暫無進一步部署"}],
            }],
        }
        analysis = analyze_card(card)
        self.assertEqual(analysis["kind"], "race-intelligence-analysis-v1")
        self.assertEqual(analysis["algorithm"], "evidence-intelligence-v1")
        first = analysis["runners"][0]
        self.assertEqual(first["supporting_evidence"][0]["quote"], "今仗以爭勝為目標")
        self.assertEqual(first["supporting_evidence"][0]["source_title"], "虛構部署原文")
        self.assertEqual(first["opposing_evidence"][0]["quote"], "上仗後曾有小毛病")
        self.assertEqual(first["win_odds_snapshot"], 4.2)
        self.assertEqual(first["place_odds_snapshot"], 1.8)
        self.assertTrue(analysis["runners"][1]["unknowns"])
        for runner in analysis["runners"]:
            self.assertNotIn("win_probability", runner)
            self.assertNotIn("place_probability", runner)
        result = {"results": [{"horse_no": 1, "finish_position": 1}, {"horse_no": 2, "finish_position": 2}]}
        self.assertIsNone(metric_row(card, result, analysis))

    def test_probability_output_requires_a_fitted_model(self):
        card = {
            "data_cutoff": "2026-10-28T09:00:00+08:00",
            "race": {"race_id": "synthetic-guard", "place_slots": 3},
            "runners": [{"horse_no": 1}, {"horse_no": 2}],
        }
        with self.assertRaisesRegex(ValueError, "use analyze_card"):
            predict_card(card, None)

    def test_uniform_output_is_an_internal_no_information_comparator(self):
        card = {
            "data_cutoff": "2026-10-28T09:00:00+08:00",
            "race": {"race_id": "synthetic-uniform", "place_slots": 1},
            "runners": [{"horse_no": 1}, {"horse_no": 2}, {"horse_no": 3}, {"horse_no": 4}],
        }
        baseline = uniform_baseline_card(card)
        self.assertEqual(baseline["model_version"], "uniform-baseline")
        self.assertEqual(baseline["algorithm"], "uniform-no-information-v1")
        self.assertAlmostEqual(sum(item["win_probability"] for item in baseline["runners"]), 1.0)
        self.assertTrue(all(item["win_probability"] == 0.25 for item in baseline["runners"]))
        self.assertIn("唔係賽日預測", baseline["warnings"][0])

    def test_place_probabilities_sum_to_slots(self):
        probabilities = [0.4, 0.25, 0.15, 0.1, 0.1]
        for slots in [1, 2, 3]:
            with self.subTest(slots=slots):
                place = place_probabilities(probabilities, slots)
                self.assertAlmostEqual(sum(place), slots)
                self.assertTrue(all(0 < value < 1 for value in place))
                self.assertEqual(place, sorted(place, reverse=True))

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

    def test_calibration_is_applied_per_race_not_per_runner(self):
        model = {"calibration": {"method": "platt", "model": {"a": 2.0, "b": 0.0}}}
        scores = [0.8, 0.2]
        labels = [1, 0]
        grouped = apply_calibration(model, scores)
        singleton = [apply_calibration(model, [score])[0] for score in scores]
        self.assertAlmostEqual(sum(grouped), 1.0)
        self.assertGreater(grouped[0], 0.8)
        self.assertEqual(singleton, [1.0, 1.0])
        grouped_ece = expected_calibration_error(list(zip(grouped, labels)))
        singleton_ece = expected_calibration_error(list(zip(singleton, labels)))
        self.assertAlmostEqual(singleton_ece, 0.5)
        self.assertLess(grouped_ece, singleton_ece)

    def test_pseudo_r2_matches_mcfadden(self):
        rows = [{"logloss": 0.5, "runner_count": 10} for _ in range(5)]
        self.assertAlmostEqual(pseudo_r2(rows), 1.0 - 0.5 / math.log(10))
        self.assertIsNone(pseudo_r2([]))

    def test_information_gain_vs_uniform_detects_improvement(self):
        market = [{"logloss": 0.60, "runner_count": 10} for _ in range(30)]
        candidate = [{"logloss": 0.55, "runner_count": 10} for _ in range(30)]
        gain = information_gain_vs_uniform(candidate, market, samples=200)
        self.assertGreater(gain["delta"], 0.0)
        self.assertGreater(gain["ci95"][0], 0.0)
        self.assertEqual(gain["race_count"], 30)
        worse = information_gain_vs_uniform(market, candidate, samples=200)
        self.assertLess(worse["delta"], 0.0)

    def test_information_gain_handles_mismatch(self):
        gain = information_gain_vs_uniform([{"logloss": 0.5, "runner_count": 8}], [])
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
        gain = information_gain_vs_uniform(candidate, market, samples=0)
        expected = gain["candidate_pseudo_r2"] - gain["uniform_pseudo_r2"]
        self.assertAlmostEqual(gain["delta"], expected)
        self.assertAlmostEqual(gain["delta"], 0.06514417228548774)

        misaligned = information_gain_vs_uniform(candidate, list(reversed(market)), samples=0)
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
        for function in (bootstrap_logloss_difference, information_gain_vs_uniform):
            with self.subTest(function=function.__name__):
                default = function(candidate, market, samples=200)
                adjusted = function(candidate, market, samples=200, alpha=0.025)
                self.assertEqual(default["alpha"], 0.05)
                self.assertEqual(default["ci"], default["ci95"])
                self.assertTrue(default["ci_resolution_sufficient"])
                self.assertIsNone(default["ci_warning"])
                self.assertEqual(adjusted["ci95"], default["ci95"])
                self.assertEqual(adjusted["alpha"], 0.025)
                self.assertTrue(adjusted["ci_resolution_sufficient"])
                self.assertLessEqual(adjusted["ci"][0], default["ci"][0])
                self.assertGreaterEqual(adjusted["ci"][1], default["ci"][1])

                unadjusted_1000 = function(candidate, market, samples=1000)
                unresolved = function(candidate, market, samples=1000, alpha=0.001)
                scale = 999 if function is bootstrap_logloss_difference else 1000
                self.assertEqual(int((0.001 / 2) * scale), 0)
                self.assertEqual(unresolved["ci95"], unadjusted_1000["ci95"])
                self.assertEqual(unresolved["ci"], [None, None])
                self.assertFalse(unresolved["ci_resolution_sufficient"])
                self.assertIn("--bootstrap", unresolved["ci_warning"])
                with self.assertRaises(ValueError):
                    function(candidate, market, samples=10, alpha=0)
        empty = bootstrap_logloss_difference([], [])
        self.assertEqual(empty["ci95"], [None, None])
        self.assertEqual(empty["ci"], [None, None])
        self.assertFalse(empty["ci_resolution_sufficient"])

    def test_bootstrap_adjusted_upper_bounds_round_outward(self):
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
        pairs = list(zip(candidate, market))
        differences = [c["logloss"] - m["logloss"] for c, m in pairs]
        for function in (bootstrap_logloss_difference, information_gain_vs_uniform):
            with self.subTest(function=function.__name__):
                rng = random.Random(20261024)
                estimates = []
                for _ in range(20):
                    if function is bootstrap_logloss_difference:
                        estimates.append(sum(
                            differences[rng.randrange(len(pairs))] for _ in range(len(pairs))
                        ) / len(pairs))
                    else:
                        indices = [rng.randrange(len(pairs)) for _ in range(len(pairs))]
                        resampled_candidate = [pairs[index][0] for index in indices]
                        resampled_market = [pairs[index][1] for index in indices]
                        estimates.append(
                            pseudo_r2(resampled_candidate) - pseudo_r2(resampled_market)
                        )
                estimates.sort()
                report = function(candidate, market, samples=20, alpha=0.12)
                self.assertTrue(report["ci_resolution_sufficient"])
                self.assertAlmostEqual(report["ci"][1], estimates[18])
                self.assertGreater(estimates[18], estimates[17])
                self.assertGreater(report["ci"][1], estimates[17])

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
            "logloss_beats_uniform_baseline": True,
            "brier_not_worse_than_uniform": True,
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
            "bootstrap_resolution_sufficient": True,
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

    def test_unresolved_bootstrap_ci_cannot_promote(self):
        gate = self.eligible_gate()
        gate["bootstrap_resolution_sufficient"] = False
        self.assertFalse(promotion_eligible(gate))

        legacy_gate = self.eligible_gate()
        del legacy_gate["bootstrap_resolution_sufficient"]
        self.assertFalse(promotion_eligible(legacy_gate))

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

        features = ["cd_place_rate", "recent_finish_mean", "days_since_run", "intent_centered"]
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
        no_model = self.run_cli(
            "predict", "--db", self.db, "--race-id", live["race"]["race_id"], ok=False,
        )
        self.assertIn("use analyze", no_model.stderr)
        baseline_guard = self.run_cli(
            "predict", "--db", self.db, "--race-id", live["race"]["race_id"],
            "--model-version", "uniform-baseline", ok=False,
        )
        self.assertIn("not race-day predictions", baseline_guard.stderr)
        analysis_path = self.base / "predictions" / "live-analysis.json"
        self.run_cli(
            "analyze", "--db", self.db, "--race-id", live["race"]["race_id"],
            "--output", analysis_path,
        )
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        self.assertEqual(analysis["model_version"], "intelligence-analysis")
        self.assertTrue(any(item["deployment_support"]["score"] == 50 for item in analysis["runners"]))
        self.assertTrue(all("win_probability" not in item for item in analysis["runners"]))
        self.assertTrue(all("place_probability" not in item for item in analysis["runners"]))

        model_path = self.base / "models" / "candidate.json"
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "candidate-v1",
            "--features", "intent_centered,rating,draw,cd_place_rate,recent_finish_mean",
            "--train-before", "2026-10-09", "--min-races", 8, "--epochs", 120,
            "--output", model_path,
        )
        model = json.loads(model_path.read_text(encoding="utf-8"))
        self.assertEqual(model["training"]["race_count"], 8)
        self.assertTrue(all(math.isfinite(value) for value in model["coefficients"].values()))
        prediction_path = self.base / "predictions" / "live.json"
        self.run_cli(
            "predict", "--db", self.db, "--race-id", live["race"]["race_id"],
            "--model-version", "candidate-v1", "--output", prediction_path,
        )
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        self.assertEqual(prediction["model_version"], "candidate-v1")
        self.assertAlmostEqual(sum(item["win_probability"] for item in prediction["runners"]), 1.0)
        self.assertAlmostEqual(sum(item["place_probability"] for item in prediction["runners"]), 3.0)

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
            "analyze", "--db", self.db, "--race-id", live["race"]["race_id"],
            "--historical", ok=False,
        )
        evaluation_path = self.base / "evaluations" / "live.json"
        self.run_cli("evaluate", "--db", self.db, "--output", evaluation_path)
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        self.assertEqual(evaluation["models"]["candidate-v1"]["race_count"], 1)
        self.assertNotIn("intelligence-analysis", evaluation["models"])
        backtest_path = self.base / "evaluations" / "backtest.json"
        self.run_cli(
            "backtest", "--db", self.db, "--model-version", "candidate-v1",
            "--min-train-races", 3, "--min-test-races", 50, "--refit-every", 2,
            "--test-before", "2026-10-09", "--bootstrap", 200, "--output", backtest_path,
        )
        backtest = json.loads(backtest_path.read_text(encoding="utf-8"))
        self.assertEqual(backtest["candidate"]["race_count"], 5)
        self.assertEqual(backtest["uniform_baseline"]["race_count"], 5)
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
            "--features", "rating", "--train-before", "2026-10-09",
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

    def test_evaluate_skips_intelligence_analysis(self):
        card, _ = self.make_card("2026-01-05")
        card_path = self.base / "cards" / "recon.json"
        self.dump(card_path, card)
        self.run_cli("import-card", "--db", self.db, "--file", card_path)
        self.run_cli(
            "analyze", "--db", self.db, "--race-id", card["race"]["race_id"], "--historical"
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
        self.assertEqual(legacy["models"], {})

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
        with closing(sqlite3.connect(self.db)) as connection:
            stored_card = json.loads(connection.execute(
                "SELECT payload_json FROM card_snapshots WHERE race_id = ?",
                (card["race"]["race_id"],),
            ).fetchone()[0])
        prediction = uniform_baseline_card(stored_card)
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

    def insert_synthetic_model(self, version, required_blocks=None, calibration=None):
        model = {
            "algorithm": "conditional-logit-v1",
            "model_version": version,
            "created_at": "2026-08-08T00:00:00+00:00",
            "features": ["intent_centered", "intent_centered", "rating"],
            "feature_blocks": {
                "intent": ["intent_centered"],
                "rating": ["rating"],
            },
        }
        if required_blocks is not None:
            model["required_blocks"] = required_blocks
        if calibration is not None:
            model["calibration"] = calibration
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT INTO models(model_version, created_at, algorithm, config_json) VALUES (?, ?, ?, ?)",
                (version, model["created_at"], model["algorithm"], json.dumps(model)),
            )
        return model

    def insert_synthetic_backtest(
        self,
        model,
        evaluation_id,
        created_at,
        ablation_block=None,
        window="2026-08-06",
        family_count=1,
        source_hash=None,
        calibration_evidence=False,
        calibration_through=None,
    ):
        alpha = 0.05 / family_count
        required = model.get("required_blocks", [])
        legacy_required = "required_blocks" not in model and bool(model.get("feature_blocks"))
        ablation_recorded = ablation_block is not None
        gate = {
            "enough_test_races": True,
            "logloss_beats_uniform_baseline": True,
            "brier_not_worse_than_uniform": True,
            "logloss_improvement_ci95_excludes_zero": True,
            "logloss_improvement_adjusted_ci_excludes_zero": True,
            "champion_comparison_required": False,
            "champion_improvement_ci95_excludes_zero": True,
            "champion_improvement_adjusted_ci_excludes_zero": True,
            "beats_current_champion": True,
            "information_gain_required": False,
            "information_gain_positive": True,
            "information_gain_ci95_excludes_zero": True,
            "information_gain_adjusted_ci_excludes_zero": True,
            "block_ablation_required": legacy_required or ablation_block in required,
            "required_blocks": required,
            "required_block_ablation_missing": [
                name for name in required if name != ablation_block
            ],
            "block_ablation_recorded": ablation_recorded,
            "block_ablation_ci95_excludes_zero": ablation_recorded,
            "block_ablation_adjusted_ci_excludes_zero": ablation_recorded,
            "bootstrap_resolution_sufficient": True,
            "bootstrap_resolution_warning": None,
            "candidates_backtested": family_count,
            "candidates_in_family": family_count,
            "familywise_alpha": 0.05,
            "effective_alpha": alpha,
            "multiple_comparison_method": "bonferroni",
            "multiple_comparison_adjusted": True,
            "multiple_comparison_stale": False,
        }
        gate["eligible"] = promotion_eligible(gate)
        comparison = None
        ablation = None
        if ablation_recorded:
            comparison = {
                "ci95": [-0.20, -0.05],
                "ci": [-0.25, -0.04],
                "alpha": alpha,
                "ci_resolution_sufficient": True,
                "ci_warning": None,
            }
            ablation = {
                "block": ablation_block,
                "candidate_minus_without_block": comparison,
            }
        report = {
            "kind": "walk-forward-backtest-v1",
            "model_version": model["model_version"],
            "source_model_hash": source_hash or payload_hash(model),
            "champion_version": None,
            "method": {
                "validation": "expanding-window walk-forward; every fit uses races before the test race only",
                "refit_every_races": 2,
                "min_train_races": 5,
                "test_after": window,
                "test_before": None,
                "bootstrap_samples": 1000,
            },
            "candidate": {"logloss": 0.4, "brier": 0.2, "race_count": 2},
            "uniform_baseline": {"logloss": 0.5, "brier": 0.25, "race_count": 2},
            "candidate_minus_baseline_logloss": {
                "ci95": [-0.20, -0.05],
                "ci": [-0.25, -0.04],
                "alpha": alpha,
                "ci_resolution_sufficient": True,
                "ci_warning": None,
            },
            "information_gain_vs_uniform": {
                "ci_resolution_sufficient": True,
                "ci_warning": None,
            },
            "champion": None,
            "candidate_minus_champion_logloss": None,
            "ablation": ablation,
            "promotion_gate": gate,
            "per_race": [{
                "candidate": {"race_id": "synthetic-race", "logloss": 0.4},
                "uniform_baseline": {"race_id": "synthetic-race", "logloss": 0.5},
            }],
        }
        if calibration_evidence:
            report["method"]["calibration_applied"] = True
            report["method"]["calibration_through"] = (
                calibration_through or model["calibration"]["through"]
            )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    "backtest",
                    model["model_version"],
                    created_at,
                    json.dumps(report),
                ),
            )
        return report

    def test_value_backtest_settles_against_dividends(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 8)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "value-v1",
            "--features", "rating", "--train-before", "2026-08-08",
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
        self.build_history(1, 8)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "selective-v1",
            "--features", "intent_centered,rating", "--train-before", "2026-08-08",
            "--min-races", 6, "--epochs", 20,
        )
        for index in range(4):
            date = f"2026-09-{index + 1:02d}"
            card, probabilities = self.make_card(date)
            card_path = self.base / "cards" / f"s{index}.json"
            self.dump(card_path, card)
            self.run_cli("import-card", "--db", self.db, "--file", card_path)
            self.run_cli(
                "predict", "--db", self.db, "--race-id", card["race"]["race_id"],
                "--model-version", "selective-v1",
            )
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
        bands = report["models"]["selective-v1"]["selective_use"]
        self.assertEqual(bands[0]["coverage_pct"], 50)
        self.assertEqual(bands[0]["race_count"], 2)

    def test_unresolved_adjusted_ci_warns_and_rejects_large_family(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 8)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "resolution-v1",
            "--features", "rating", "--train-before", "2026-08-08",
            "--min-races", 6, "--epochs", 20,
        )
        connection = sqlite3.connect(self.db)
        try:
            for index in range(49):
                connection.execute(
                    """
                    INSERT INTO evaluation_runs
                        (evaluation_id, kind, model_version, created_at, report_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        f"synthetic-family-evaluation-{index}",
                        "backtest",
                        f"synthetic-family-candidate-{index}",
                        f"2026-08-09T00:00:{index:02d}+00:00",
                        json.dumps({"kind": "synthetic-family-placeholder"}),
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        report = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "resolution-v1",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-06", "--bootstrap", 1000,
            ).stdout
        )
        baseline = report["candidate_minus_baseline_logloss"]
        information_gain = report["information_gain_vs_uniform"]
        for comparison in (baseline, information_gain):
            self.assertEqual(comparison["alpha"], 0.001)
            self.assertEqual(comparison["ci"], [None, None])
            self.assertIsNotNone(comparison["ci95"][0])
            self.assertFalse(comparison["ci_resolution_sufficient"])
            self.assertIn("--bootstrap", comparison["ci_warning"])

        gate = report["promotion_gate"]
        self.assertEqual(gate["candidates_in_family"], 50)
        self.assertEqual(gate["effective_alpha"], 0.001)
        self.assertFalse(gate["bootstrap_resolution_sufficient"])
        self.assertIn("--bootstrap", gate["bootstrap_resolution_warning"])
        self.assertFalse(gate["multiple_comparison_adjusted"])
        self.assertFalse(gate["multiple_comparison_stale"])
        self.assertFalse(gate["eligible"])

        promotion = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "resolution-v1").stdout
        )
        self.assertEqual(promotion["event"], "rejected")
        self.assertFalse(promotion["promotion_gate"]["bootstrap_resolution_sufficient"])
        self.assertFalse(promotion["promotion_gate"]["multiple_comparison_stale"])

    def test_block_ablation_is_recorded_and_gates_promotion(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 10)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "abl-v1",
            "--features", "intent_centered,rating",
            "--block", "intent=intent_centered", "--required-block", "intent",
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
            "--features", "intent_centered,rating",
            "--block", "intent=intent_centered", "--required-block", "intent",
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
        self.assertEqual(without_ablation["information_gain_vs_uniform"]["alpha"], 0.025)
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

    def test_undeclared_required_block_is_rejected(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 3)
        failed = self.run_cli(
            "fit", "--db", self.db, "--model-version", "undeclared-required",
            "--features", "intent_centered", "--required-block", "intent",
            "--train-before", "2026-08-04", "--min-races", 2, "--epochs", 5,
            ok=False,
        )
        self.assertIn("Required blocks were never declared: intent", failed.stderr)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM models WHERE model_version = 'undeclared-required'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_two_required_blocks_promote_after_both_ablations(self):
        self.run_cli("init", "--db", self.db)
        model = self.insert_synthetic_model("two-required", ["intent", "rating"])
        self.insert_synthetic_backtest(
            model, "ev-intent", "2026-08-09T00:00:01+00:00", "intent"
        )
        first = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "two-required").stdout
        )
        self.assertEqual(first["event"], "rejected")
        self.assertEqual(first["promotion_gate"]["required_block_ablation_missing"], ["rating"])
        self.assertEqual(
            first["promotion_gate"]["required_block_ablation_evidence"],
            {"intent": "ev-intent"},
        )

        self.insert_synthetic_backtest(
            model, "ev-rating", "2026-08-09T00:00:02+00:00", "rating"
        )
        second = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "two-required").stdout
        )
        self.assertEqual(second["event"], "promoted")
        self.assertTrue(second["promotion_gate"]["eligible"])
        self.assertEqual(second["promotion_gate"]["required_block_ablation_missing"], [])
        self.assertEqual(
            second["promotion_gate"]["required_block_ablation_evidence"],
            {"intent": "ev-intent", "rating": "ev-rating"},
        )

    def test_required_ablation_rejects_other_windows_stale_family_and_model_hash(self):
        self.run_cli("init", "--db", self.db)
        model = self.insert_synthetic_model("required-context", ["intent", "rating"])
        self.insert_synthetic_backtest(
            model, "ev-intent-other-window", "2026-08-09T00:00:01+00:00",
            "intent", window="2026-08-05",
        )
        self.insert_synthetic_backtest(
            model, "ev-rating-current-window", "2026-08-09T00:00:02+00:00",
            "rating", window="2026-08-06",
        )
        different_window = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "required-context").stdout
        )
        self.assertEqual(different_window["event"], "rejected")
        self.assertEqual(
            different_window["promotion_gate"]["required_block_ablation_evidence"],
            {"rating": "ev-rating-current-window"},
        )
        self.assertEqual(
            different_window["promotion_gate"]["required_block_ablation_missing"], ["intent"]
        )

        self.insert_synthetic_backtest(
            model, "ev-intent-stale-family", "2026-08-09T00:00:03+00:00",
            "intent", window="2026-08-06", family_count=2,
        )
        stale = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "required-context").stdout
        )
        self.assertEqual(stale["event"], "rejected")
        self.assertTrue(stale["promotion_gate"]["multiple_comparison_stale"])
        self.assertEqual(stale["promotion_gate"]["required_block_ablation_missing"], ["intent"])

        self.insert_synthetic_backtest(
            model, "ev-intent-wrong-model", "2026-08-09T00:00:04+00:00",
            "intent", window="2026-08-06", source_hash="not-the-current-model",
        )
        wrong_model = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "required-context").stdout
        )
        self.assertEqual(wrong_model["event"], "rejected")
        self.assertFalse(wrong_model["promotion_gate"]["multiple_comparison_stale"])
        self.assertEqual(wrong_model["promotion_gate"]["required_block_ablation_missing"], ["intent"])
        self.assertEqual(
            wrong_model["promotion_gate"]["required_block_ablation_evidence"],
            {"rating": "ev-rating-current-window"},
        )

    def test_legacy_block_ablation_remains_latest_only(self):
        self.run_cli("init", "--db", self.db)
        model = self.insert_synthetic_model("legacy-ablation")
        self.insert_synthetic_backtest(
            model, "ev-legacy-earlier", "2026-08-09T00:00:01+00:00", "intent"
        )
        self.insert_synthetic_backtest(
            model, "ev-legacy-latest-missing", "2026-08-09T00:00:02+00:00"
        )
        rejected = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "legacy-ablation").stdout
        )
        self.assertEqual(rejected["event"], "rejected")
        self.assertTrue(rejected["promotion_gate"]["block_ablation_missing"])

        self.insert_synthetic_backtest(
            model, "ev-legacy-latest-pass", "2026-08-09T00:00:03+00:00", "intent"
        )
        promoted = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "legacy-ablation").stdout
        )
        self.assertEqual(promoted["event"], "promoted")
        self.assertTrue(promoted["promotion_gate"]["eligible"])

    def test_explicit_optional_model_has_no_ablation_gate(self):
        self.run_cli("init", "--db", self.db)
        model = self.insert_synthetic_model("explicit-optional", [])
        self.insert_synthetic_backtest(
            model, "ev-optional", "2026-08-09T00:00:01+00:00"
        )
        promotion = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "explicit-optional").stdout
        )
        self.assertEqual(promotion["event"], "promoted")
        self.assertTrue(promotion["promotion_gate"]["eligible"])
        self.assertNotIn("block_ablation_missing", promotion["promotion_gate"])

    def test_optional_block_does_not_gate_promotion(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 10)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "opt-v1",
            "--features", "intent_centered,rating",
            "--block", "intent=intent_centered",
            "--train-before", "2026-08-08", "--min-races", 6, "--epochs", 20,
        )
        report = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "opt-v1",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-06", "--bootstrap", 50,
            ).stdout
        )
        gate = report["promotion_gate"]
        self.assertEqual(gate["required_blocks"], [])
        self.assertFalse(gate["block_ablation_required"])
        self.assertEqual(gate["required_block_ablation_missing"], [])

    def test_required_block_needs_ablation_before_promotion(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 10)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "req-v1",
            "--features", "intent_centered,rating",
            "--block", "intent=intent_centered", "--required-block", "intent",
            "--train-before", "2026-08-08", "--min-races", 6, "--epochs", 20,
        )
        report = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "req-v1",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-06", "--bootstrap", 50,
            ).stdout
        )
        gate = report["promotion_gate"]
        self.assertEqual(gate["required_blocks"], ["intent"])
        self.assertEqual(gate["required_block_ablation_missing"], ["intent"])
        self.assertFalse(gate["eligible"])
        promotion = json.loads(
            self.run_cli("promote", "--db", self.db, "--model-version", "req-v1").stdout
        )
        self.assertEqual(promotion["event"], "rejected")

    def test_calibrate_creates_version_and_predict_applies_it(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 20)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "cal-base",
            "--features", "rating", "--train-before", "2026-08-12",
            "--min-races", 8, "--epochs", 40,
        )
        calibration = json.loads(
            self.run_cli(
                "calibrate", "--db", self.db, "--model-version", "cal-base",
                "--new-version", "cal-base-cal", "--method", "isotonic",
                "--min-train-races", 6, "--min-samples", 20, "--allow-worse-calibration",
            ).stdout
        )
        self.assertEqual(calibration["method"], "isotonic")
        self.assertEqual(calibration["source"], "post-train-out-of-sample")
        self.assertEqual(calibration["train_before"], "2026-08-12")
        self.assertEqual(calibration["samples"], 30)
        self.assertEqual(calibration["evaluation_samples"], 15)
        self.assertEqual(calibration["post_train_samples"], 45)
        self.assertEqual(calibration["fit_race_count"], 6)
        self.assertEqual(calibration["evaluation_race_count"], 3)
        self.assertEqual(calibration["post_train_race_count"], 9)
        self.assertEqual(
            calibration["fit_race_ids"],
            [f"2026-08-{day:02d}-ST-R1" for day in range(12, 18)],
        )
        self.assertEqual(
            calibration["evaluation_race_ids"],
            [f"2026-08-{day:02d}-ST-R1" for day in range(18, 21)],
        )
        self.assertTrue(set(calibration["fit_race_ids"]).isdisjoint(calibration["evaluation_race_ids"]))

        before_points = []
        after_points = []
        with closing(sqlite3.connect(self.db)) as connection, connection:
            source = json.loads(connection.execute(
                "SELECT config_json FROM models WHERE model_version = 'cal-base'"
            ).fetchone()[0])
            calibrated = json.loads(connection.execute(
                "SELECT config_json FROM models WHERE model_version = 'cal-base-cal'"
            ).fetchone()[0])
            for race_id in calibration["evaluation_race_ids"]:
                card = json.loads(connection.execute(
                    "SELECT payload_json FROM card_snapshots WHERE race_id = ? "
                    "ORDER BY captured_at DESC, rowid DESC LIMIT 1",
                    (race_id,),
                ).fetchone()[0])
                result = json.loads(connection.execute(
                    "SELECT payload_json FROM result_snapshots WHERE race_id = ? "
                    "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
                    (race_id,),
                ).fetchone()[0])
                prediction = predict_card(card, source)
                winners = {
                    int(row["horse_no"]) for row in result["results"]
                    if row.get("finish_position") == 1
                }
                race_before = [
                    (float(item["win_probability"]), 1 if int(item["horse_no"]) in winners else 0)
                    for item in prediction["runners"]
                ]
                race_after = apply_calibration(
                    calibrated, [point[0] for point in race_before]
                )
                before_points.extend(race_before)
                after_points.extend(
                    (probability, point[1])
                    for probability, point in zip(race_after, race_before)
                )
        self.assertEqual(len(before_points), calibration["evaluation_samples"])
        self.assertAlmostEqual(
            calibration["ece_before"], expected_calibration_error(before_points)
        )
        self.assertAlmostEqual(
            calibration["ece_after"], expected_calibration_error(after_points)
        )

        card, _ = self.make_card("2026-12-01", live=True)
        card_path = self.base / "cards" / "cal.json"
        self.dump(card_path, card)
        self.run_cli("import-card", "--db", self.db, "--file", card_path)
        prediction = json.loads(
            self.run_cli(
                "predict", "--db", self.db, "--race-id", card["race"]["race_id"],
                "--model-version", "cal-base-cal",
            ).stdout
        )
        self.assertEqual(prediction["calibration"]["method"], "isotonic")
        self.assertEqual(prediction["calibration"]["evaluation_samples"], 15)
        self.assertTrue(any("post-train-out-of-sample" in warning for warning in prediction["warnings"]))
        self.assertAlmostEqual(
            sum(item["win_probability"] for item in prediction["runners"]), 1.0, places=9
        )

    def test_calibrate_never_falls_back_to_training_window(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 10)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "cal-no-post",
            "--features", "rating", "--train-before", "2026-08-11",
            "--min-races", 8, "--epochs", 10,
        )
        failed = self.run_cli(
            "calibrate", "--db", self.db, "--model-version", "cal-no-post",
            "--new-version", "cal-no-post-cal", "--min-train-races", 6,
            "--min-samples", 1, ok=False,
        )
        self.assertIn("found 0", failed.stderr)
        self.assertIn("add at least 2 more post-training races", failed.stderr)
        with closing(sqlite3.connect(self.db)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM models WHERE model_version = 'cal-no-post-cal'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_calibrate_reports_post_training_race_and_sample_deficit(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 12)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "cal-short-post",
            "--features", "rating", "--train-before", "2026-08-11",
            "--min-races", 8, "--epochs", 10,
        )
        failed = self.run_cli(
            "calibrate", "--db", self.db, "--model-version", "cal-short-post",
            "--new-version", "cal-short-post-cal", "--min-train-races", 6,
            "--min-samples", 6, ok=False,
        )
        self.assertIn("found 5 (1 more needed)", failed.stderr)
        self.assertIn("1 fit races and 1 evaluation races", failed.stderr)
        self.assertIn("2 post-training races", failed.stderr)
        self.assertIn("At least 1 more completed post-training race is needed", failed.stderr)
        with closing(sqlite3.connect(self.db)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM models WHERE model_version = 'cal-short-post-cal'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


    def test_calibrated_backtest_requires_post_calibration_window(self):
        self.run_cli("init", "--db", self.db)
        self.build_history(1, 16)
        self.run_cli(
            "fit", "--db", self.db, "--model-version", "cb-base",
            "--features", "rating", "--train-before", "2026-08-10",
            "--min-races", 8, "--epochs", 30,
        )
        calibration = json.loads(
            self.run_cli(
                "calibrate", "--db", self.db, "--model-version", "cb-base",
                "--new-version", "cb-cal", "--method", "isotonic",
                "--min-train-races", 6, "--min-samples", 20, "--allow-worse-calibration",
            ).stdout
        )
        self.assertEqual(calibration["through"], "2026-08-16")

        overlapped = self.run_cli(
            "backtest", "--db", self.db, "--model-version", "cb-cal",
            "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
            "--test-after", "2026-08-14", "--bootstrap", 50, ok=False,
        )
        self.assertIn("after 2026-08-16", overlapped.stderr)

        self.build_history(17, 4)
        report = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "cb-cal",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-17", "--bootstrap", 50,
            ).stdout
        )
        self.assertTrue(report["method"]["calibration_applied"])
        self.assertEqual(report["method"]["calibration_through"], "2026-08-16")
        uncalibrated = json.loads(
            self.run_cli(
                "backtest", "--db", self.db, "--model-version", "cb-base",
                "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
                "--test-after", "2026-08-17", "--bootstrap", 50,
            ).stdout
        )
        self.assertNotAlmostEqual(
            report["candidate"]["logloss"], uncalibrated["candidate"]["logloss"]
        )

        # A synthetic champion event isolates the champion-side leakage guard.
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT INTO model_events VALUES (?, ?, ?, ?, ?)",
                ("synthetic_cb_champion", "cb-cal", "promoted", "2099-01-01T00:00:00+00:00", "{}"),
            )
        champion_overlap = self.run_cli(
            "backtest", "--db", self.db, "--model-version", "cb-base",
            "--min-train-races", 5, "--min-test-races", 2, "--refit-every", 2,
            "--test-after", "2026-08-14", "--bootstrap", 50, ok=False,
        )
        self.assertIn("after 2026-08-16", champion_overlap.stderr)

    def test_calibrated_promotion_requires_current_calibrated_backtest(self):
        self.run_cli("init", "--db", self.db)
        model = self.insert_synthetic_model(
            "calibrated-promotion",
            required_blocks=[],
            calibration={
                "method": "platt",
                "model": {"a": 1.0, "b": 0.0},
                "through": "2026-08-08",
            },
        )

        def promote():
            return json.loads(
                self.run_cli(
                    "promote", "--db", self.db, "--model-version", model["model_version"]
                ).stdout
            )

        self.insert_synthetic_backtest(
            model, "ev-calibrated-legacy", "2026-08-09T00:00:01+00:00"
        )
        legacy = promote()
        self.assertEqual(legacy["event"], "rejected")
        self.assertTrue(legacy["promotion_gate"]["calibration_evidence_required"])
        self.assertFalse(legacy["promotion_gate"]["calibration_evidence_valid"])

        self.insert_synthetic_backtest(
            model,
            "ev-calibrated-mismatched",
            "2026-08-09T00:00:02+00:00",
            calibration_evidence=True,
            calibration_through="2026-08-07",
        )
        mismatched = promote()
        self.assertEqual(mismatched["event"], "rejected")
        self.assertFalse(mismatched["promotion_gate"]["calibration_evidence_valid"])

        self.insert_synthetic_backtest(
            model,
            "ev-calibrated-current",
            "2026-08-09T00:00:03+00:00",
            calibration_evidence=True,
        )
        current = promote()
        self.assertEqual(current["event"], "promoted")
        self.assertTrue(current["promotion_gate"]["calibration_evidence_valid"])


class CalibrationGuardUnitTests(unittest.TestCase):
    def test_auto_method_prefers_platt_on_small_samples(self):
        from racing_lab import resolve_calibration_method

        method, policy = resolve_calibration_method("auto", 144)
        self.assertEqual(method, "platt")
        self.assertIn("auto", policy)
        self.assertEqual(resolve_calibration_method("auto", 900)[0], "isotonic")
        self.assertEqual(resolve_calibration_method("platt", 900)[0], "platt")

    def test_ece_guard_rejects_worse_and_allows_override(self):
        from racing_lab import calibration_ece_guard

        calibration_ece_guard(0.02, 0.01, allow_worse=False)
        with self.assertRaises(ValueError):
            calibration_ece_guard(0.02, 0.03, allow_worse=False)
        calibration_ece_guard(0.02, 0.03, allow_worse=True)

    def test_apply_calibration_clamps_extremes(self):
        from racing_model import CALIBRATION_MAX, apply_calibration

        model = {"calibration": {"method": "isotonic", "model": {"x": [0.0, 1.0], "y": [0.0, 0.0]}}}
        adjusted = apply_calibration(model, [0.1, 0.4, 0.9])
        self.assertAlmostEqual(sum(adjusted), 1.0, places=9)
        self.assertTrue(all(0 < value < CALIBRATION_MAX for value in adjusted))


class ValueDiagnosticsUnitTests(unittest.TestCase):
    @staticmethod
    def _stream(races):
        for race_id, winner in races:
            record = {
                "result": {"results": [
                    {"horse_no": 1, "finish_position": 1 if winner == 1 else 2,
                     "win_dividend": 30.0 if winner == 1 else None},
                    {"horse_no": 2, "finish_position": 1 if winner == 2 else 2,
                     "win_dividend": 30.0 if winner == 2 else None},
                ]}
            }
            prediction = {
                "race_id": race_id,
                "runners": [
                    {"horse_no": 1, "win_probability": 0.6, "win_odds_snapshot": 3.0},
                    {"horse_no": 2, "win_probability": 0.4, "win_odds_snapshot": 2.5},
                ],
            }
            yield record, prediction

    def test_settlement_reports_risk_and_concentration(self):
        from decision import settle_stream

        races = [(f"r{i}", 1 if i % 3 != 0 else 2) for i in range(12)]
        result = settle_stream(
            self._stream(races), stake_mode="flat", cap=0.05, bankroll=1000.0, min_edge=-1.0
        )
        self.assertEqual(result["bets"], 12)
        self.assertAlmostEqual(
            result["bankroll_end"], result["bankroll_start"] + result["profit"], places=4
        )
        self.assertAlmostEqual(result["max_single_stake_fraction"], 0.05, places=6)
        self.assertIsNotNone(result["profit_concentration"])
        self.assertLessEqual(result["profit_concentration"], 1.0)
        low, high = result["roi_ci95"]
        self.assertIsNotNone(low)
        self.assertLessEqual(low, high)

    def test_max_single_stake_uses_bet_not_race_total(self):
        from decision import settle_stream

        record = {"result": {"results": [
            {"horse_no": 1, "finish_position": 1, "win_dividend": 60.0},
            {"horse_no": 2, "finish_position": 2, "win_dividend": None},
            {"horse_no": 3, "finish_position": 3, "win_dividend": None},
        ]}}
        prediction = {
            "race_id": "multi-bet-race",
            "runners": [
                {"horse_no": 1, "win_probability": 0.2, "win_odds_snapshot": 6.0},
                {"horse_no": 2, "win_probability": 0.2, "win_odds_snapshot": 8.0},
                {"horse_no": 3, "win_probability": 0.2, "win_odds_snapshot": 10.0},
            ],
        }
        result = settle_stream(
            [(record, prediction)], stake_mode="kelly", cap=0.2,
            bankroll=1000.0, min_edge=0.01, kelly_multiplier=1.0,
            max_race_fraction=0.4, unit=1.0,
        )
        self.assertEqual(result["bets"], 3)
        self.assertAlmostEqual(result["turnover"], 236.0, places=4)
        self.assertAlmostEqual(result["max_single_stake_fraction"], 0.111, places=6)


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

class ReconstructionCardTests(unittest.TestCase):
    def _card(self, *, mode="historical", late=True, late_value=True, retrieved="2026-10-26T10:00:00+08:00"):
        card = {
            "schema_version": "1.1",
            "collection_mode": mode,
            "captured_at": "2026-10-26T11:00:00+08:00",
            "data_cutoff": "2026-06-24T09:00:00+08:00",
            "race": {
                "race_id": "2026-06-24-HV-R1", "meeting_date": "2026-06-24", "venue": "HV",
                "race_no": 1, "off_time": "2026-06-24T19:15:00+08:00", "distance_m": 1200,
                "place_slots": 3, "declared_starters": 8,
            },
            "sources": [{
                "source_id": "S1", "title": "HKJC results", "url": "https://example.invalid/r",
                "retrieved_at": retrieved,
            }],
            "runners": [
                {"horse_no": 1, "horse_name": "A", "features": {}},
                {"horse_no": 2, "horse_name": "B", "features": {}},
            ],
        }
        if late:
            card["retrieval_after_cutoff"] = late_value
        return card

    def test_historical_card_may_use_late_sources_when_flagged(self):
        from racing_lab import validate_card

        warnings = validate_card(self._card())
        self.assertTrue(any("事後重建" in warning for warning in warnings))

    def test_flag_is_rejected_without_it(self):
        from racing_lab import validate_card

        with self.assertRaises(ValueError) as raised:
            validate_card(self._card(late=False))
        self.assertIn("retrieval_after_cutoff", str(raised.exception))

    def test_live_card_cannot_use_the_flag(self):
        from racing_lab import validate_card

        with self.assertRaises(ValueError):
            validate_card(self._card(mode="live"))

    def test_flag_must_be_boolean(self):
        from racing_lab import validate_card

        with self.assertRaises(ValueError):
            validate_card(self._card(late_value="yes"))


class CalibrationDriftUnitTests(unittest.TestCase):
    def test_period_buckets(self):
        from drift import period_of

        self.assertEqual(period_of("2026-06-24"), "2026-06")
        self.assertEqual(period_of("2026-06-24", mode="meeting"), "2026-06-24")

    def test_drift_flags_only_reliable_periods(self):
        from drift import summarize_periods

        good = [(0.2, 0), (0.8, 1)] * 60
        bad = [(0.9, 0), (0.1, 1)] * 60
        tiny = [(0.5, 0)]
        report = summarize_periods(
            {"2026-06": good, "2026-07": bad, "2026-08": tiny},
            baseline_ece=0.0, min_samples=100, threshold=0.02,
        )
        self.assertTrue(report["drift_detected"])
        self.assertIn("2026-07", report["drifted_periods"])
        self.assertNotIn("2026-08", report["drifted_periods"])
        self.assertFalse(next(p for p in report["periods"] if p["period"] == "2026-08")["drift"])

    def test_missing_baseline_never_flags(self):
        from drift import summarize_periods

        report = summarize_periods({"2026-06": [(0.9, 0), (0.1, 1)] * 60},
                                   baseline_ece=None, min_samples=100, threshold=0.02)
        self.assertFalse(report["drift_detected"])
        self.assertIsNone(report["periods"][0]["delta_vs_baseline"])


class ObsidianSyncTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import obsidian_sync

        self.sync = obsidian_sync

    def test_legacy_picks_note_becomes_partial_cards(self):
        note = """---
title: fixture
date: 2026-06-24
venue: Happy Valley
---

| Race | Dist | Class | Pick 1 | Pick 2 | Pick 3 | Pick 4 | Top-4 Hits | Brier |
|------|------|-------|--------|--------|--------|--------|------------|-------|
| R1 | 2200m | C5 | #3 Alpha 🥇 | #4 Beta 🥈 | #5 | #6 | 2/4 | 1.25 |
| R2 | 1650m | C5 | N/A | N/A | N/A | N/A | N/A | N/A |
"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-06-24 HV.md"
            path.write_text(note, encoding="utf-8")
            cards, results, stats = self.sync.legacy_note_to_records(path)
        self.assertEqual(stats["cards"], 1)
        self.assertEqual(cards[0]["race"]["race_id"], "2026-06-24-HV-R1")
        self.assertEqual(cards[0]["race"]["venue"], "HV")
        self.assertEqual(len(cards[0]["runners"]), 4)
        self.assertTrue(cards[0]["partial"])
        self.assertEqual(len(results), 1)
        positions = {row["horse_no"]: row["finish_position"] for row in results[0]["results"]}
        self.assertEqual(positions, {3: 1, 4: 2})

    def test_canonical_note_round_trips(self):
        card = {
            "schema_version": "1.1",
            "collection_mode": "historical",
            "captured_at": "2026-06-24T09:00:00+08:00",
            "data_cutoff": "2026-06-24T09:00:00+08:00",
            "race": {
                "race_id": "2026-06-24-HV-R1", "meeting_date": "2026-06-24", "venue": "HV",
                "race_no": 1, "off_time": "2026-06-24T19:15:00+08:00", "distance_m": 1200,
                "place_slots": 3,
            },
            "sources": [{"source_id": "S1", "title": "fixture", "url": "https://example.invalid"}],
            "runners": [
                {"horse_no": 1, "horse_name": "Alpha", "jockey": "J1", "trainer": "T1",
                 "draw": 1, "weight_lb": 120, "rating": 70, "win_odds": 3.5, "scratched": False},
                {"horse_no": 2, "horse_name": "Beta", "jockey": "J2", "trainer": "T2",
                 "draw": 2, "weight_lb": 118, "rating": 68, "win_odds": 4.0, "scratched": False},
            ],
        }
        result = {"results": [{"horse_no": 1, "finish_position": 1, "win_dividend": 35.0}]}
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text(self.sync.note_for_card(card, result), encoding="utf-8")
            parsed_card, parsed_result, problems = self.sync.canonical_note_to_records(path)
        self.assertEqual(problems, [])
        self.assertEqual(parsed_card["race"]["race_id"], "2026-06-24-HV-R1")
        self.assertEqual(len(parsed_card["runners"]), 2)
        self.assertEqual(parsed_card["runners"][0]["win_odds"], 3.5)
        self.assertEqual(parsed_result["results"][0]["finish_position"], 1)

    def test_venue_mapping(self):
        self.assertEqual(self.sync.venue_code("Happy Valley"), "HV")
        self.assertEqual(self.sync.venue_code("沙田"), "ST")
        self.assertEqual(self.sync.venue_code("Goodwood"), "GD")


if __name__ == "__main__":
    unittest.main()

