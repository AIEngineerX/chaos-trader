#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from gate_classifier import classify_gate
from mode_classifier import classify_mode, venue_label


class ModeGateTests(unittest.TestCase):
    def test_low_cap_trench_treats_low_liq_as_size_warning(self):
        result = {
            "market": {"market_cap": 50_000, "liquidity_usd": 4_000, "dex_id": "pumpswap"},
            "classification": {"flow": {"severity": 0}, "risk_flags": []},
            "pumpfun": {"pumpfun_activity_visible": True},
        }
        mode = classify_mode(result)
        self.assertEqual(mode["mode"], "low-cap trench")
        self.assertIn("dust size only", " ".join(mode["risk_notes"]))
        self.assertEqual(mode["venue"], "pumpfun/pumpswap")

    def test_dead_fake_on_severe_drawdown_and_extreme_churn(self):
        result = {
            "market": {"market_cap": 12_000, "liquidity_usd": 3_000, "price_change_h1": -80},
            "classification": {"flow": {"severity": 6}, "risk_flags": ["severe drawdown"]},
        }
        self.assertEqual(classify_mode(result)["mode"], "dead/fake")

    def test_bonding_curve_liquidity_does_not_trigger_fake_flow_failure(self):
        result = {
            "market": {"market_cap": 12_000, "liquidity_usd": 3_000},
            "classification": {"flow": {"severity": 6}, "risk_flags": []},
            "pumpfun": {"complete": False},
        }
        self.assertEqual(classify_mode(result)["mode"], "low-cap trench")

    def test_conviction_trench_requires_liquidity_and_mcap(self):
        result = {
            "market": {"market_cap": 500_000, "liquidity_usd": 80_000, "dex_id": "raydium"},
            "classification": {"flow": {"severity": 0}, "risk_flags": []},
        }
        mode = classify_mode(result)
        self.assertEqual(mode["mode"], "conviction trench")
        self.assertEqual(venue_label(result["market"], {}), "raydium")

    def test_low_cap_gate_micro_watch_with_early_scouts(self):
        result = {
            "mint": "abc",
            "mode_context": {"mode": "low-cap trench"},
            "market": {"market_cap": 60_000, "liquidity_usd": 8_000},
            "classification": {"flow": {"severity": 0}, "risk_flags": []},
            "wallet_timing": {"wallet_timing": [{"first_touch_utc": "2026-06-25T00:00:00Z", "first_touch_type": "buy"}]},
            "secondary_evidence": {"metrics": {"early_hidden_count": 2, "tracked_buyer_count": 3}},
        }
        gate = classify_gate(result)
        self.assertEqual(gate["gate"], "micro-watch")
        self.assertIn("dust", " ".join(gate["risk"]).lower())

    def test_low_cap_secondary_without_timing_stays_micro_study(self):
        result = {
            "mint": "abc",
            "mode_context": {"mode": "low-cap trench"},
            "market": {"market_cap": 60_000, "liquidity_usd": 8_000},
            "classification": {"flow": {"severity": 0}, "risk_flags": []},
            "wallet_timing": {"wallet_timing": [{"first_touch_utc": None, "first_touch_type": "sample-holder"}]},
            "secondary_evidence": {"metrics": {"early_hidden_count": 2, "tracked_buyer_count": 3}},
        }
        gate = classify_gate(result)
        self.assertEqual(gate["gate"], "micro-study")
        self.assertIn("unproven", " ".join(gate["why"]).lower())

    def test_conviction_gate_deep_check_with_structure_and_exits(self):
        result = {
            "mint": "abc",
            "mode_context": {"mode": "conviction trench"},
            "market": {"market_cap": 600_000, "liquidity_usd": 100_000},
            "classification": {"flow": {"severity": 0}, "validation": {}},
            "wallet_timing": {"wallet_timing": [{"first_touch_utc": "2026-06-25T00:00:00Z", "first_touch_type": "buy"}]},
            "secondary_evidence": {"metrics": {"tracked_buyer_count": 4, "tracked_seller_count": 2}},
        }
        self.assertEqual(classify_gate(result)["gate"], "deep-check")


if __name__ == "__main__":
    unittest.main()
