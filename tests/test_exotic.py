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
from exotic import canonical_sha256, settle_ticket, validate_ticket  # noqa: E402
from tests.test_longshot import make_card, make_decision, make_result  # noqa: E402


def make_ticket(card, decision):
    return {
        "schema_version": "exotic-ticket-v1",
        "rule_version": "exotic-v1",
        "race_id": card["race"]["race_id"],
        "created_at": "2026-10-25T10:45:00+08:00",
        "core_horse_no": 1,
        "longshot_decision_sha256": canonical_sha256(decision),
        "market_card_sha256": canonical_sha256(card),
        "budget_limit_hkd": 50,
        "sources": [
            {
                "source_id": "S1",
                "url": "https://example.invalid/sectionals",
                "retrieved_at": "2026-10-25T09:45:00+08:00",
                "sha256": "a" * 64,
                "excerpt": "官方分段原文：末段時間較同場大部分馬匹為快，但須配合步速解讀。",
            },
            {
                "source_id": "S2",
                "url": "https://example.invalid/incidents",
                "retrieved_at": "2026-10-25T09:46:00+08:00",
                "sha256": "b" * 64,
                "excerpt": "賽事報告原文：上仗四百米處受困未能望空，其後恢復走勢。",
            },
            {
                "source_id": "S3",
                "url": "https://example.invalid/audit",
                "retrieved_at": "2026-10-25T10:42:00+08:00",
                "sha256": "c" * 64,
                "excerpt": "獨立核查原文：排位及退出資料已於賽前重新核對。",
            },
        ],
        "researchers": [
            {
                "agent_id": "A",
                "role": "form_and_race_shape",
                "completed_at": "2026-10-25T09:55:00+08:00",
                "blind_to_other_researcher_output": True,
                "source_ids": ["S1"],
            },
            {
                "agent_id": "B",
                "role": "stable_and_intelligence",
                "completed_at": "2026-10-25T09:56:00+08:00",
                "blind_to_other_researcher_output": True,
                "source_ids": ["S2"],
            },
        ],
        "audit": {
            "agent_id": "C",
            "started_at": "2026-10-25T10:41:00+08:00",
            "completed_at": "2026-10-25T10:44:00+08:00",
            "status": "passed",
            "critical_errors": [],
            "recommendations_exposed_before_completion": False,
            "independent_source_search": True,
            "source_ids": ["S1", "S3"],
            "checks": {
                "identity": True,
                "timestamps": True,
                "quotes": True,
                "counterevidence": True,
                "candidate_coverage": True,
            },
        },
        "combinations": [
            make_combination("Q", 1, 3),
            make_combination("Q", 3, 4),
            make_combination("PQ", 1, 3),
            make_combination("PQ", 1, 4),
            make_combination("PQ", 3, 4),
        ],
        "abstain_reason": None,
    }


def make_combination(pool, first, second):
    return {
        "pool": pool,
        "horses": [first, second],
        "stake_hkd": 10,
        "scenario_id": "normal-pace",
        "mechanism": "正常步速下，一馬可跟前保留遮擋，另一馬有同程追趕證據，可同時進入目標名次。",
        "strongest_counterevidence": "若前段互相消耗，跟前馬亦可能同時失位。",
        "unknowns": ["賽日臨場健康及實際取位尚未知"],
        "withdraw_if": "核心馬出現健康異常或配腳退出。",
        "horse_evidence": [
            {
                "horse_no": first,
                "agent_id": "A",
                "family": "sectional",
                "source_id": "S1",
                "quote": "末段時間較同場大部分馬匹為快",
            },
            {
                "horse_no": second,
                "agent_id": "B",
                "family": "race_replay",
                "source_id": "S2",
                "quote": "上仗四百米處受困未能望空",
            },
        ],
    }


def make_official_result():
    result = make_result()
    positions = {3: 1, 4: 2, 1: 3, 2: 4, 5: 5, 6: 6, 7: 7, 8: 8}
    for row in result["results"]:
        row["finish_position"] = positions[row["horse_no"]]
    result["exotic_dividends"] = {
        "Q": {
            "status": "final",
            "combinations": [{"horses": [3, 4], "dividend_hkd": 194.5}],
            "refunds": [],
        },
        "PQ": {
            "status": "final",
            "combinations": [
                {"horses": [3, 4], "dividend_hkd": 74.0},
                {"horses": [1, 3], "dividend_hkd": 112.5},
                {"horses": [1, 4], "dividend_hkd": 65.0},
            ],
            "refunds": [],
        },
    }
    return result


class ExoticFreezeTests(unittest.TestCase):
    def setUp(self):
        self.card = make_card()
        self.decision = make_decision()
        self.ticket = make_ticket(self.card, self.decision)

    def test_freeze_binds_decision_and_separates_pool_costs(self):
        report = validate_ticket(self.card, self.decision, self.ticket)
        self.assertEqual(report["validation_status"], "passed", report["errors"])
        self.assertEqual(report["total_cost_hkd"], 50)
        self.assertEqual(report["cost_by_pool_hkd"], {"Q": 20, "PQ": 30})
        self.assertEqual(report["combination_counts"], {"Q": 2, "PQ": 3})
        self.assertEqual(report["agent_isolation"]["scope"], "procedural_isolation_only_not_evidence_independence")
        self.assertEqual(report["agent_isolation"]["auditor_shared_source_ids"], ["S1"])
        self.assertTrue(report["forward_kpi_eligible"])

    def test_same_pool_duplicate_is_blocked_but_cross_pool_pair_is_allowed(self):
        self.ticket["combinations"].append(make_combination("Q", 1, 3))
        report = validate_ticket(self.card, self.decision, self.ticket)
        self.assertIn("duplicate combination", " ".join(report["errors"]))
        self.assertIn("exceed budget", " ".join(report["errors"]))

    def test_pair_requires_both_horses_evidence_and_counterevidence(self):
        pair = self.ticket["combinations"][0]
        pair["horse_evidence"] = pair["horse_evidence"][:1]
        pair["strongest_counterevidence"] = ""
        report = validate_ticket(self.card, self.decision, self.ticket)
        self.assertIn("evidence for each horse", " ".join(report["errors"]))
        self.assertIn("strongest_counterevidence", " ".join(report["errors"]))

    def test_auditor_must_be_blind_and_start_after_both_researchers(self):
        self.ticket["audit"]["recommendations_exposed_before_completion"] = True
        self.ticket["audit"]["started_at"] = "2026-10-25T09:50:00+08:00"
        report = validate_ticket(self.card, self.decision, self.ticket)
        self.assertIn("before seeing recommendations", " ".join(report["errors"]))
        self.assertIn("after both researchers", " ".join(report["errors"]))

    def test_ticket_is_bound_to_exact_decision_and_core(self):
        self.ticket["longshot_decision_sha256"] = "0" * 64
        self.ticket["core_horse_no"] = 2
        report = validate_ticket(self.card, self.decision, self.ticket)
        self.assertIn("does not bind", " ".join(report["errors"]))
        self.assertIn("must equal", " ".join(report["errors"]))


class ExoticSettlementTests(unittest.TestCase):
    def setUp(self):
        self.card = make_card()
        self.decision = make_decision()
        self.ticket = make_ticket(self.card, self.decision)
        self.result = make_official_result()

    def test_settles_each_pq_combination_and_keeps_hot_cash_separate(self):
        report = settle_ticket(self.card, self.decision, self.ticket, self.result)
        self.assertTrue(report["accounts"]["intelligence_hit_core_top3"])
        self.assertEqual(report["accounts"]["target_combination_hits"], 2)
        self.assertEqual(report["accounts"]["official_payout_hits"], 4)
        self.assertEqual(report["accounts"]["target_core_payout_hkd"], 177.5)
        self.assertEqual(report["accounts"]["non_core_payout_hkd"], 268.5)
        self.assertEqual(report["accounts"]["both_horses_win_odds_below_10_payout_hkd"], 268.5)
        self.assertEqual(report["cash"]["official_dividends_hkd"], 446.0)
        self.assertEqual(report["cash"]["net_profit_hkd"], 396.0)
        self.assertEqual(report["pools"]["Q"]["dividends_hkd"], 194.5)
        self.assertEqual(report["pools"]["PQ"]["dividends_hkd"], 251.5)
        hot_q = next(row for row in report["combinations"] if row["pool"] == "Q" and row["horses"] == [3, 4])
        self.assertTrue(hot_q["official_payout_hit"])
        self.assertFalse(hot_q["target_hit"])

    def test_no_official_dividend_record_means_no_settlement(self):
        del self.result["exotic_dividends"]
        with self.assertRaisesRegex(ValueError, "no odds fallback"):
            settle_ticket(self.card, self.decision, self.ticket, self.result)

    def test_refunds_reduce_exposure_without_becoming_target_hits(self):
        for row in self.result["results"]:
            if row["horse_no"] == 3:
                row["finish_position"] = None
                row["status"] = "WV"
        for pool in ("Q", "PQ"):
            data = self.result["exotic_dividends"][pool]
            data["combinations"] = []
            data["refunds"] = [
                {"horses": [1, 3], "amount_hkd": 10},
                {"horses": [3, 4], "amount_hkd": 10},
            ]
        report = settle_ticket(self.card, self.decision, self.ticket, self.result)
        self.assertEqual(report["cash"]["cost_hkd"], 50)
        self.assertEqual(report["cash"]["refunds_hkd"], 40)
        self.assertEqual(report["cash"]["net_stake_hkd"], 10)
        self.assertEqual(report["cash"]["net_profit_hkd"], -10)
        self.assertEqual(report["accounts"]["target_combination_hits"], 1)
        self.assertEqual(report["accounts"]["official_payout_hits"], 0)

    def test_unresolved_non_finisher_blocks_cash_settlement(self):
        for row in self.result["results"]:
            if row["horse_no"] == 3:
                row["finish_position"] = None
                row["status"] = "WV"
        with self.assertRaisesRegex(ValueError, "refund or adjudication"):
            settle_ticket(self.card, self.decision, self.ticket, self.result)

    def test_changed_ticket_does_not_match_freeze_receipt(self):
        receipt = validate_ticket(self.card, self.decision, self.ticket)
        self.ticket["combinations"][0]["stake_hkd"] = 20
        self.ticket["budget_limit_hkd"] = 60
        with self.assertRaisesRegex(ValueError, "differs from its ready freeze receipt"):
            settle_ticket(self.card, self.decision, self.ticket, self.result, receipt)

    def test_cli_freeze_then_settle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {}
            for name, payload in (
                ("card", self.card), ("decision", self.decision),
                ("ticket", self.ticket), ("result", self.result),
            ):
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            freeze_path = root / "freeze.json"
            common = [
                sys.executable, str(SCRIPTS / "exotic.py"),
                "--card", str(paths["card"]),
                "--longshot-decision", str(paths["decision"]),
                "--ticket", str(paths["ticket"]),
            ]
            frozen = subprocess.run(
                common[:2] + ["freeze"] + common[2:] + ["--output", str(freeze_path)],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(frozen.returncode, 0, frozen.stderr)
            self.assertEqual(json.loads(freeze_path.read_text(encoding="utf-8"))["freeze_status"], "ready")
            settled_path = root / "settled.json"
            settled = subprocess.run(
                common[:2] + ["settle"] + common[2:] + [
                    "--result", str(paths["result"]), "--freeze", str(freeze_path),
                    "--output", str(settled_path),
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(settled.returncode, 0, settled.stderr)
            self.assertEqual(json.loads(settled_path.read_text(encoding="utf-8"))["cash"]["net_profit_hkd"], 396)


class OfficialDividendParsingTests(unittest.TestCase):
    def test_result_parser_preserves_q_and_all_pq_dividends(self):
        from meeting_chain import result_from_html

        card = make_card()
        card["race"].update({"race_name": "測試賽事", "class_name": "第五班", "distance_m": 1200})
        cells = ["1", "3", "馬三 (A001)", "騎師", "練馬師", "128", "1000", "1", "鼻位", "1 1 1", "1:09.00", "5.0"]
        html = f"""
        <table><tr><td>場地狀況</td><td>賽道 測試賽事 1200米 第五班</td></tr></table>
        <div class='performance'><table><tbody><tr>{''.join(f'<td>{x}</td>' for x in cells)}</tr></tbody></table></div>
        <div class='dividend_tab'><table><tbody>
          <tr><td rowspan='1'>獨贏</td><td>3</td><td>50.00</td></tr>
          <tr><td rowspan='1'>連贏</td><td>3,4</td><td>194.50</td></tr>
          <tr><td rowspan='3'>位置Q</td><td>3,4</td><td>74.00</td></tr>
          <tr><td>1,3</td><td>112.50</td></tr>
          <tr><td>1,4</td><td>65.00</td></tr>
        </tbody></table></div>
        """.encode("utf-8")
        result = result_from_html(html, card, "https://example.invalid/result", "2026-10-25T14:00:00+08:00")
        self.assertEqual(result["exotic_dividends"]["Q"]["combinations"][0]["dividend_hkd"], 194.5)
        self.assertEqual(len(result["exotic_dividends"]["PQ"]["combinations"]), 3)
        self.assertEqual(result["exotic_dividends"]["PQ"]["combinations"][1]["horses"], [1, 3])


if __name__ == "__main__":
    unittest.main()
