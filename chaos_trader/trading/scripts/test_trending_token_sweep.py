#!/usr/bin/env python3
"""Golden-contract tests for Chaos trending sweep gating."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import trending_token_sweep as sweep

MINT = "AXLmMWkRmSPdPxkuMqAD4nzYBK7QRssNkYZ6RXzLpump"


def read(verdict: str, *, gate: str | None = None) -> dict:
    return {
        "mint": MINT,
        "market": {"symbol": "EAGLE250"},
        "classification": {
            "verdict": verdict,
            "attention_phase": "late" if "exit" in verdict or verdict == "avoid" else "live",
            "why_not_watch": "surface momentum lacks organic-flow validation.",
            "risk_flags": ["high volume/liquidity churn"],
            "flow": {
                "volume_liquidity_ratio": 151.7,
                "avg_tx_usd": 46,
                "tx_count": 66000,
                "flags": ["extreme volume/liquidity churn 151.7x"],
            },
        },
        "entry_gate": {"action": gate} if gate else {},
    }


class TrendingSweepGateTests(unittest.TestCase):
    def test_alpha_mode_filters_avoid_and_exit_liquidity(self):
        self.assertTrue(sweep.is_trap_read(read("avoid")))
        self.assertTrue(sweep.is_trap_read(read("study", gate="exit-liquidity-watch")))
        self.assertFalse(sweep.should_display_deep_read(read("avoid"), "alpha"))
        self.assertFalse(sweep.should_display_deep_read(read("exit-liquidity-watch"), "alpha"))

    def test_trap_mode_shows_only_trap_reads(self):
        self.assertTrue(sweep.should_display_deep_read(read("avoid"), "trap"))
        self.assertFalse(sweep.should_display_deep_read(read("manual-review"), "trap"))

    def test_all_mode_preserves_every_deep_read(self):
        self.assertTrue(sweep.should_display_deep_read(read("avoid"), "all"))
        self.assertTrue(sweep.should_display_deep_read(read("manual-review"), "all"))

    def test_filtered_read_keeps_reason_without_private_expansion(self):
        compact = sweep.compact_filtered_read(read("avoid"))
        self.assertEqual(compact["mint"], MINT)
        self.assertEqual(compact["label"], "avoid")
        self.assertIn("surface momentum", compact["why_not_watch"])
        self.assertIn("volume_liquidity_ratio", compact["flow"])


if __name__ == "__main__":
    unittest.main()
