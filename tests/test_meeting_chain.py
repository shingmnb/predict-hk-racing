from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "predict-hk-racing" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from meeting_chain import (  # noqa: E402
    RESERVE_RE,
    RUNNER_RE,
    parse_runner_block,
    validate_result_coverage,
)
from racing_lab import validate_card  # noqa: E402

SHA = "a" * 64


def historical_card() -> dict:
    return {
        "schema_version": "1.1",
        "collection_mode": "historical",
        "notes": {"historical_reconstruction": True, "forward_kpi_eligible": False},
        "captured_at": "2026-10-27T15:00:00+08:00",
        "data_cutoff": "2026-10-25T12:30:00+08:00",
        "race": {
            "race_id": "2026-10-25-ST-R1",
            "meeting_date": "2026-10-25",
            "venue": "ST",
            "race_no": 1,
            "off_time": "2026-10-25T12:40:00+08:00",
            "distance_m": 1200,
            "declared_starters": 3,
            "place_slots": 3,
        },
        "sources": [{
            "source_id": "PDF",
            "title": "synthetic historical source",
            "url": "https://example.invalid/source.pdf",
            "retrieved_at": "2026-10-27T14:00:00+08:00",
            "available_at": "2026-10-25T12:00:00+08:00",
            "sha256": SHA,
            "availability_evidence": {
                "kind": "session_attachment",
                "reference": "sources/provenance/evidence.json",
                "sha256": SHA,
            },
            "excerpt": "更新時間: 25/10/2026 12:00",
        }],
        "runners": [
            {"horse_no": 1, "horse_name": "甲", "horse_id": "A001", "scratched": False, "intent_evidence": []},
            {"horse_no": 2, "horse_name": "乙", "horse_id": "A002", "scratched": False, "intent_evidence": []},
            {"horse_no": 3, "horse_name": "丙", "horse_id": "A003", "scratched": False, "intent_evidence": []},
            {"horse_no": 4, "horse_name": "丁", "horse_id": "A004", "scratched": True, "intent_evidence": []},
        ],
    }


def complete_result() -> dict:
    return {
        "schema_version": "1.0",
        "race_id": "2026-10-25-ST-R1",
        "recorded_at": "2026-10-25T14:00:00+08:00",
        "sources": [{"source_id": "R", "title": "synthetic result"}],
        "results": [
            {"horse_no": 1, "horse_name": "甲", "horse_id": "A001", "finish_position": 1, "status": "OK"},
            {"horse_no": 2, "horse_name": "乙", "horse_id": "A002", "finish_position": 2, "status": "OK"},
            {"horse_no": 3, "horse_name": "丙", "horse_id": "A003", "finish_position": 3, "status": "OK"},
            {"horse_no": 4, "horse_name": "丁", "horse_id": "A004", "finish_position": None, "status": "WV"},
        ],
    }


class HistoricalTimeTests(unittest.TestCase):
    def test_late_retrieval_is_valid_with_pre_cutoff_availability(self):
        warnings = validate_card(historical_card())
        self.assertTrue(any("歷史重建" in warning for warning in warnings))

    def test_missing_or_future_availability_is_rejected(self):
        for mutation in ["missing_time", "missing_proof", "future", "missing_hash"]:
            card = historical_card()
            source = card["sources"][0]
            if mutation == "missing_time":
                source.pop("available_at")
            elif mutation == "missing_proof":
                source.pop("availability_evidence")
            elif mutation == "future":
                source["available_at"] = "2026-10-25T12:31:00+08:00"
            else:
                source["sha256"] = "not-a-hash"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_card(card)

    def test_live_rule_does_not_allow_late_source_retrieval(self):
        card = historical_card()
        card["collection_mode"] = "live"
        card["captured_at"] = card["data_cutoff"]
        with self.assertRaisesRegex(ValueError, "retrieved after data_cutoff"):
            validate_card(card)

    def test_source_timestamp_requires_verbatim_quote(self):
        card = historical_card()
        source = card["sources"][0]
        source["availability_evidence"] = {
            "kind": "source_timestamp",
            "reference": "source.excerpt",
            "quote": "更新時間: 25/10/2026 12:00",
        }
        validate_card(card)
        source["availability_evidence"]["quote"] = "並不存在的時間"
        with self.assertRaisesRegex(ValueError, "availability quote"):
            validate_card(card)


class ResultCoverageTests(unittest.TestCase):
    def test_complete_coverage_and_dead_heat(self):
        card = historical_card()
        result = complete_result()
        validate_result_coverage(result, card)
        result["results"][1]["finish_position"] = 1
        validate_result_coverage(result, card)

    def test_missing_runner_and_bad_scratch_are_rejected(self):
        card = historical_card()
        missing = complete_result()
        missing["results"].pop(0)
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            validate_result_coverage(missing, card)
        bad_scratch = complete_result()
        bad_scratch["results"][-1]["finish_position"] = 4
        with self.assertRaisesRegex(ValueError, "Scratched horse"):
            validate_result_coverage(bad_scratch, card)

    def test_active_runner_needs_position_or_legal_non_finish(self):
        card = historical_card()
        result = complete_result()
        result["results"][2]["finish_position"] = None
        with self.assertRaisesRegex(ValueError, "Active horse"):
            validate_result_coverage(result, card)
        result["results"][2]["status"] = "PU"
        validate_result_coverage(result, card)


class PdfRunnerParsingTests(unittest.TestCase):
    def test_active_scratched_and_reserve_headers(self):
        active_line = "2 ↓ 騰達勇士 K376 (TT) 方嘉柏 135 黃智弘 (8) 進口前 ：-"
        self.assertIsNotNone(RUNNER_RE.match(active_line))
        active = parse_runner_block([
            {"page": 8, "line": active_line},
            {"page": 8, "line": "TYCOON EXPRESS(AUS) 4 ch g PPG C Fownes (40) -3E C W Wong"},
        ])
        self.assertEqual(active["horse_id"], "K376")
        self.assertEqual(active["rating"], 40)
        self.assertEqual(active["draw"], 8)
        self.assertFalse(active["scratched"])
        minimum_line = "8 ↑ 風再起時 J534 (B/XB/TT) 巫偉傑 # 115 ?+2 梁家俊 (4) 進口前 ：-"
        minimum = parse_runner_block([
            {"page": 18, "line": minimum_line},
            {"page": 18, "line": "AERIS NOVA(AUS) 6 b g PP W K Mo (91) K C Leung"},
        ])
        self.assertTrue(minimum["minimum_weight_applied"])
        self.assertEqual(minimum["probable_overweight_lb"], 2)
        self.assertEqual(minimum["rating"], 91)
        scratched_line = "4 星月峰雲 L455 韋達 124"
        scratched = parse_runner_block([
            {"page": 6, "line": scratched_line},
            {"page": 6, "line": "CELESTIAL ZOUS(AUS) 3 b g PPG D J Whyte (52)"},
            {"page": 6, "line": "L455 Christopher Chain Siao Liang 退出"},
        ])
        self.assertTrue(scratched["scratched"])
        self.assertIsNone(scratched["jockey"])
        reserve_line = "S2 嘉嘉友福 K456 (CP2/TT) 沈集成 130 一 進口前 ：-"
        self.assertIsNotNone(RESERVE_RE.match(reserve_line))
        reserve = parse_runner_block([
            {"page": 9, "line": reserve_line},
            {"page": 9, "line": "SO MY FOLKS(AUS) 5 b/br g ISG C S Shum (35)"},
        ], reserve=True)
        self.assertEqual(reserve["reserve_no"], "S2")
        self.assertNotIn("horse_no", reserve)


if __name__ == "__main__":
    unittest.main()
