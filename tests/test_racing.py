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
from racing_model import (  # noqa: E402
    assess_intent,
    market_probabilities,
    pair_probabilities,
    place_probabilities,
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


if __name__ == "__main__":
    unittest.main()
