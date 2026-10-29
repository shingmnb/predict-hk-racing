from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "predict-hk-racing" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from longshot import SNAPSHOT_POLICY, backtest, validate_decision  # noqa: E402


def make_card(race_id="2026-10-25-ST-R1", *, captured=None, mode="live"):
    captured = captured or race_id[:10] + "T10:30:00+08:00"
    odds = [12, 20, 5, 6, 7, 8, 9, 11]
    return {
        "schema_version": "1.1",
        "collection_mode": mode,
        "captured_at": captured,
        "data_cutoff": captured,
        "race": {
            "race_id": race_id,
            "meeting_date": race_id[:10],
            "venue": "ST",
            "race_no": 1,
            "off_time": race_id[:10] + "T13:00:00+08:00",
            "declared_starters": 8,
            "place_slots": 3,
        },
        "sources": [{"source_id": "M1", "title": "合成全場賠率", "official": True}],
        "runners": [
            {"horse_no": index + 1, "horse_name": f"馬{index + 1}", "scratched": False, "win_odds": value}
            for index, value in enumerate(odds)
        ],
    }


def make_decision(race_id="2026-10-25-ST-R1"):
    return {
        "schema_version": "longshot-decision-v1",
        "rule_version": "longshot-v1",
        "race_id": race_id,
        "created_at": race_id[:10] + "T10:40:00+08:00",
        "fundamental_created_at": race_id[:10] + "T09:50:00+08:00",
        "fundamental_run_id": "synthetic-fundamental-run",
        "market_blind": True,
        "snapshot_policy": SNAPSHOT_POLICY,
        "sources": [
            {"source_id": "S1", "excerpt": "官方原文：上仗四百米處受困未能望空，其後恢復走勢。"},
            {"source_id": "S2", "excerpt": "官方原文：末段時間較同場大部分馬匹為快，但須配合步速解讀。"},
        ],
        "reviews": [
            {
                "horse_no": 1,
                "status": "selected",
                "evidence": [
                    {"family": "race_replay", "source_id": "S1", "quote": "上仗四百米處受困未能望空"},
                    {"family": "sectional", "source_id": "S2", "quote": "末段時間較同場大部分馬匹為快"},
                ],
                "strongest_counterevidence": "整體名次仍屬一般，單一事件未必解釋全部落差。",
                "unknowns": ["賽日健康尚未核實"],
                "withdraw_if": "賽前確認健康異常。",
            },
            {"horse_no": 2, "status": "rejected", "reason": "只有降班，缺乏第二類證據。"},
            {"horse_no": 8, "status": "rejected", "reason": "近績反證未解決。"},
        ],
        "abstain_reason": None,
    }


def make_result(race_id="2026-10-25-ST-R1"):
    return {
        "schema_version": "1.0",
        "race_id": race_id,
        "recorded_at": race_id[:10] + "T14:00:00+08:00",
        "sources": [{"source_id": "R1", "official": True, "url": "https://example.invalid/result"}],
        "results": [
            {"horse_no": 1, "finish_position": 1, "status": "OK", "win_dividend": 120, "place_dividend": 30},
            {"horse_no": 2, "finish_position": 2, "status": "OK", "place_dividend": 45},
            {"horse_no": 3, "finish_position": 3, "status": "OK", "place_dividend": 20},
            {"horse_no": 4, "finish_position": 4, "status": "OK"},
            {"horse_no": 5, "finish_position": 5, "status": "OK"},
            {"horse_no": 6, "finish_position": 6, "status": "OK"},
            {"horse_no": 7, "finish_position": 7, "status": "OK"},
            {"horse_no": 8, "finish_position": 8, "status": "OK"},
        ],
    }


class LongshotValidationTests(unittest.TestCase):
    def test_one_qualified_selection_passes(self):
        report = validate_decision(make_card(), make_decision())
        self.assertEqual(report["validation_status"], "passed")
        self.assertEqual(report["selected_horse_no"], 1)
        self.assertEqual([x["horse_no"] for x in report["eligible_horses"]], [1, 2, 8])
        self.assertTrue(report["forward_kpi_eligible"])

    def test_outside_window_forbids_selection_but_allows_abstention(self):
        card = make_card(captured="2026-10-25T09:00:00+08:00")
        report = validate_decision(card, make_decision())
        self.assertIn("outside T-3h to T-2h", " ".join(report["warnings"]))
        self.assertIn("selection is forbidden", " ".join(report["errors"]))
        decision = {"reviews": [], "abstain_reason": "沒有合格時點賠率。"}
        base = make_decision()
        base["fundamental_created_at"] = "2026-10-25T08:50:00+08:00"
        base.update(decision)
        report = validate_decision(card, base)
        self.assertEqual(report["validation_status"], "passed")
        self.assertTrue(report["abstained"])
        self.assertFalse(report["selected_horse_no"])
        self.assertFalse(report["forward_kpi_eligible"])

    def test_zero_eligible_horses_is_a_valid_abstention(self):
        card = make_card()
        for runner in card["runners"]:
            runner["win_odds"] = 8.0
        decision = make_decision()
        decision["reviews"] = []
        decision["abstain_reason"] = "全場沒有 10 倍以上馬匹。"
        report = validate_decision(card, decision)
        self.assertEqual(report["validation_status"], "passed")
        self.assertTrue(report["abstained"])

    def test_every_eligible_horse_must_be_reviewed(self):
        decision = make_decision()
        decision["reviews"].pop()
        report = validate_decision(make_card(), decision)
        self.assertIn("cover every eligible", " ".join(report["errors"]))

    def test_selected_horse_needs_two_evidence_families_and_counterevidence(self):
        decision = make_decision()
        selected = decision["reviews"][0]
        selected["evidence"] = selected["evidence"][:1]
        selected["strongest_counterevidence"] = ""
        report = validate_decision(make_card(), decision)
        self.assertIn("two independent evidence families", " ".join(report["errors"]))
        self.assertIn("strongest_counterevidence", " ".join(report["errors"]))

    def test_two_selections_are_rejected(self):
        decision = make_decision()
        second = copy.deepcopy(decision["reviews"][0])
        second["horse_no"] = 2
        decision["reviews"][1] = second
        report = validate_decision(make_card(), decision)
        self.assertIn("at most one", " ".join(report["errors"]))

    def test_market_blind_fundamental_must_precede_snapshot(self):
        decision = make_decision()
        decision["fundamental_created_at"] = "2026-10-25T10:35:00+08:00"
        report = validate_decision(make_card(), decision)
        self.assertIn("must be frozen before", " ".join(report["errors"]))


class LongshotBacktestTests(unittest.TestCase):
    def test_backtest_retains_abstention_and_computes_official_dividend_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ("cards", "decisions", "results"):
                (root / folder).mkdir()
            card = make_card()
            decision = make_decision()
            result = make_result()
            (root / "cards" / f"{card['race']['race_id']}.json").write_text(json.dumps(card), encoding="utf-8")
            (root / "decisions" / f"{card['race']['race_id']}.json").write_text(json.dumps(decision), encoding="utf-8")
            (root / "results" / f"{card['race']['race_id']}.json").write_text(json.dumps(result), encoding="utf-8")

            abstain_card = make_card("2026-10-26-ST-R1")
            for runner in abstain_card["runners"]:
                runner["win_odds"] = 8.0
            abstain = make_decision("2026-10-26-ST-R1")
            abstain["reviews"] = []
            abstain["abstain_reason"] = "沒有合資格馬。"
            abstain_result = make_result("2026-10-26-ST-R1")
            (root / "cards" / f"{abstain_card['race']['race_id']}.json").write_text(json.dumps(abstain_card), encoding="utf-8")
            (root / "decisions" / f"{abstain_card['race']['race_id']}.json").write_text(json.dumps(abstain), encoding="utf-8")
            (root / "results" / f"{abstain_card['race']['race_id']}.json").write_text(json.dumps(abstain_result), encoding="utf-8")

            report = backtest(root, bootstrap=100)
            metrics = report["metrics"]
            self.assertEqual(metrics["races_including_abstentions"], 2)
            self.assertEqual(metrics["selected_count"], 1)
            self.assertEqual(metrics["abstained_races"], 1)
            self.assertEqual(metrics["selected_top3_hit_rate_pct"], 100.0)
            self.assertEqual(metrics["actual_cold_top3_capture_rate_pct"], 50.0)
            self.assertEqual(metrics["all_eligible_runners_top3_baseline_pct"], 66.67)
            self.assertEqual(metrics["same_count_random_selection_expectation_pct"], 66.67)
            self.assertEqual(metrics["win_roi"]["roi_pct"], 1100.0)
            self.assertEqual(metrics["place_roi"]["roi_pct"], 200.0)
            self.assertFalse(report["maturity"]["minimum_races_met"])

    def test_cli_validate_writes_machine_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card_path = root / "card.json"
            decision_path = root / "decision.json"
            output_path = root / "validation.json"
            card_path.write_text(json.dumps(make_card()), encoding="utf-8")
            decision_path.write_text(json.dumps(make_decision()), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "longshot.py"), "validate", "--card", str(card_path),
                 "--decision", str(decision_path), "--output", str(output_path)],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["validation_status"], "passed")


if __name__ == "__main__":
    unittest.main()
