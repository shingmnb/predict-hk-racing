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
from racing_model import (  # noqa: E402
    assess_intent,
    expected_calibration_error,
    fit_model,
    isotonic_apply,
    isotonic_fit,
    market_probabilities,
    pair_probabilities,
    place_probabilities,
    platt_apply,
    platt_fit,
    shin_probabilities,
)

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


if __name__ == "__main__":
    unittest.main()
