"""Acceptance tests for the Intel-First / Odds-as-Reference spec.

Spec: predict-hk-racing/references/intel-first-odds-reference.md

These tests are skipped until KIMI implements `scripts/intel_score.py` and the
related validation hooks. Remove the @unittest.skip decorators as each item
lands; the spec is the contract, not the implementation.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "predict-hk-racing" / "scripts"
sys.path.insert(0, str(SCRIPTS))


class IntelFirstSpecTests(unittest.TestCase):
    @unittest.skip("pending KIMI: scripts/intel_score.py")
    def test_default_weights_sum_to_one(self):
        import intel_score

        weights = intel_score.DEFAULT_WEIGHTS
        self.assertEqual(set(weights), {"intel", "fundamentals", "odds"})
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        self.assertAlmostEqual(weights["intel"], 0.50, places=9)
        self.assertAlmostEqual(weights["fundamentals"], 0.30, places=9)
        self.assertAlmostEqual(weights["odds"], 0.20, places=9)

    @unittest.skip("pending KIMI: scripts/intel_score.py")
    def test_intel_score_is_independent_of_odds(self):
        import intel_score

        evidence = [
            {"category": "trial", "direction": "positive", "strength": 0.8,
             "confidence": 0.9, "source_id": "S1", "quote": "試閘第一名，末段加速明顯",
             "observed_at": "2026-10-24T09:00:00+08:00"},
            {"category": "gear", "direction": "positive", "strength": 0.6,
             "confidence": 0.8, "source_id": "S1", "quote": "是日配備改動：加戴眼罩",
             "observed_at": "2026-10-24T09:00:00+08:00"},
        ]
        a = intel_score.intel_centered(evidence)
        b = intel_score.intel_centered(evidence, odds=1.5)
        c = intel_score.intel_centered(evidence, odds=40.0)
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    @unittest.skip("pending KIMI: scripts/intel_score.py")
    def test_evidence_without_quote_does_not_score(self):
        import intel_score

        bad = [{"category": "stable_intent", "direction": "positive",
                "strength": 1.0, "confidence": 1.0, "source_id": "S1",
                "quote": "", "observed_at": "2026-10-24T09:00:00+08:00"}]
        self.assertEqual(intel_score.intel_centered(bad), 0.0)

    @unittest.skip("pending KIMI: scripts/intel_score.py")
    def test_odds_veto_only_on_drift_or_crash(self):
        import intel_score

        self.assertFalse(intel_score.odds_veto(place_drift_pct=-5.0, sp_crash_pct=0.0, final_sp=6.0))
        self.assertTrue(intel_score.odds_veto(place_drift_pct=-12.0, sp_crash_pct=0.0, final_sp=6.0))
        self.assertTrue(intel_score.odds_veto(place_drift_pct=0.0, sp_crash_pct=-50.0, final_sp=4.9))
        self.assertFalse(intel_score.odds_veto(place_drift_pct=0.0, sp_crash_pct=-50.0, final_sp=8.0))

    @unittest.skip("pending KIMI: scripts/intel_score.py")
    def test_pick_requires_message_reason(self):
        import intel_score

        pick = {"horse_no": 4, "horse_name": "正本巨星", "message_reason": ""}
        with self.assertRaises(ValueError):
            intel_score.validate_pick(pick)

        pick["message_reason"] = "欠"
        with self.assertRaises(ValueError):
            intel_score.validate_pick(pick)

        pick["message_reason"] = "試閘第一、初戴眼罩、馬房連日出試，狀態明顯回升。"
        intel_score.validate_pick(pick)

    @unittest.skip("pending KIMI: scripts/intel_score.py")
    def test_message_reason_length_bounds(self):
        import intel_score

        ok = "同程三戰兩勝、檔位內移、騎師續配，賠率只作參考。"
        self.assertTrue(18 <= len(ok) <= 50)
        intel_score.validate_pick({"horse_no": 1, "message_reason": ok})

        too_long = "試" * 51
        with self.assertRaises(ValueError):
            intel_score.validate_pick({"horse_no": 1, "message_reason": too_long})


if __name__ == "__main__":
    unittest.main()
