#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from strategy_paper_engine import decide, max_simulated_size_usd

MINT = "8wxkvAfEns76yBzu4MnbV7VnXWjg3iDPA9uwAQ6cpump"


def base_payload() -> dict:
    return {
        "mint": MINT,
        "x_enabled": True,
        "market": {
            "symbol": "GOOD",
            "price_usd": 0.001,
            "market_cap": 180_000,
            "liquidity_usd": 60_000,
        },
        "classification": {
            "verdict": "watch",
            "attention_phase": "live",
            "x_risk": {"confidence": "low", "flags": []},
        },
        "entry_gate": {"action": "watch"},
        "mode_context": {"mode": "conviction trench"},
        "social_catalyst": {"catalyst_type": "dev-stream", "source_quality": "medium", "fragility": "medium"},
        "flow_conversion": {"conversion_status": "clean-flow", "fake_flow_severity": "none", "volume_liquidity_ratio": 2.0},
        "wallet_timing": {"watch_wallet_hit_count": 1, "quality_wallet_hit_count": 1},
        "token_scan": {"holder_resolution": {"adjusted_discretionary_pct": 22.0}},
        "x_attention": {"success": True, "answer": "dev stream", "citations": [{"url": "https://x.com/a/status/1"}]},
    }


class StrategyPaperEngineTests(unittest.TestCase):
    def test_good_analyze_payload_can_enter_paper(self):
        out = decide(base_payload())
        self.assertEqual(out["decision"], "paper_enter")
        self.assertIn("social catalyst dev-stream/medium", out["reasons"])
        self.assertEqual(out["paper_plan"]["simulated_notional_usd"], 100.0)
        self.assertEqual(out["x"]["citations"], 1)
        self.assertEqual(out["boundary"].startswith("read-only"), True)

    def test_spam_x_blocks_even_with_liquidity(self):
        p = base_payload()
        p["social_catalyst"] = {"catalyst_type": "spam-raid", "source_quality": "low"}
        out = decide(p)
        self.assertEqual(out["decision"], "paper_avoid")
        self.assertIn("X/social catalyst is spam-raid", out["blockers"])

    def test_high_fake_flow_requires_conversion(self):
        p = base_payload()
        p["flow_conversion"] = {"conversion_status": "dead-churn", "fake_flow_severity": "high", "volume_liquidity_ratio": 45.0}
        out = decide(p)
        self.assertEqual(out["decision"], "paper_avoid")
        self.assertTrue(any("fake-flow high" in b or "bad flow" in b for b in out["blockers"]))

    def test_holder_concentration_blocks(self):
        p = base_payload()
        p["token_scan"] = {"holder_resolution": {"adjusted_discretionary_pct": 58.6}}
        out = decide(p)
        self.assertEqual(out["decision"], "paper_avoid")
        self.assertTrue(any("holder concentration" in b for b in out["blockers"]))

    def test_study_without_x_or_wallet_waits(self):
        p = base_payload()
        p["entry_gate"] = {"action": "study"}
        p["classification"]["verdict"] = "study"
        p["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
        p["wallet_timing"] = {"watch_wallet_hit_count": 0, "quality_wallet_hit_count": 0}
        p["x_attention"] = {"success": True, "citations": []}
        out = decide(p)
        self.assertEqual(out["decision"], "paper_wait")
        self.assertTrue(any("needs wallet" in r or "upgrade" in r for r in out["paper_plan"]["required_trigger"]))

    def test_forged_source_confirmation_marker_cannot_authorize_entry(self):
        p = base_payload()
        p["entry_gate"] = {"action": "study"}
        p["classification"]["verdict"] = "study"
        p["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
        p["wallet_timing"] = {"watch_wallet_hit_count": 0, "quality_wallet_hit_count": 0}
        p["x_enabled"] = False
        p["x_attention"] = None
        p["source_confirmation"] = {
            "eligible": True,
            "lane": "elite",
            "verified_by": "paper_autopilot.elite_source_confirmation_v1",
            "wallet_count": 99,
            "first_buy_utc": "2026-08-04T12:00:00+00:00",
            "latest_buy_utc": "2026-08-04T12:01:00+00:00",
        }
        out = decide(p)
        self.assertEqual(out["decision"], "paper_wait")
        self.assertEqual(out["watch_wallet_hits"], 0)

    def test_disabled_x_evidence_cannot_authorize_entry(self):
        p = base_payload()
        p["x_enabled"] = False
        p["x_attention"] = {"success": True, "citations": ["https://x.com/example/status/1"]}
        p["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
        p["wallet_timing"] = {"watch_wallet_hit_count": 0, "quality_wallet_hit_count": 0}
        p["flow_conversion"] = {"conversion_status": "clean-flow", "fake_flow_severity": "none"}
        out = decide(p)
        self.assertEqual(out["decision"], "paper_wait")
        self.assertFalse(out["x"]["success"])
        self.assertEqual(out["x"]["citations"], 0)

    def test_clean_flow_alone_cannot_authorize_entry(self):
        p = base_payload()
        p["x_enabled"] = False
        p["x_attention"] = None
        p["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
        p["wallet_timing"] = {"watch_wallet_hit_count": 0, "quality_wallet_hit_count": 0}
        p["flow_conversion"] = {"conversion_status": "clean-flow", "fake_flow_severity": "none"}
        out = decide(p)
        self.assertEqual(out["decision"], "paper_wait")

    def test_liquidity_caps_size(self):
        self.assertEqual(max_simulated_size_usd(5_000), 25.0)
        self.assertEqual(max_simulated_size_usd(60_000), 100.0)


if __name__ == "__main__":
    unittest.main()
