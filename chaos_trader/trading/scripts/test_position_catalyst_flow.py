#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from flow_conversion import classify_flow_conversion
from position_context import analyze_position_context, normalize_entry_gate
from social_catalyst_classifier import classify_social_catalyst
from token_delta_tracker import track_token_delta  # noqa: E402
from token_event_analyzer import flow_profile  # noqa: E402
from wallet_position_timing import enrich_wallet_timing  # noqa: E402

MINT = "8wxkvAfEns76yBzu4MnbV7VnXWjg3iDPA9uwAQ6cpump"


class PositionCatalystFlowTests(unittest.TestCase):
    def solangeles_fixture(self) -> dict:
        return {
            "mint": MINT,
            "market": {
                "symbol": "SOLANGELES",
                "price_usd": "0.00001833",
                "market_cap": 3_320_000,
                "liquidity_usd": 222_000,
                "price_change_h1": 22.1,
            },
            "gate": {"gate": "avoid", "why": ["quality wallet confirmation exists, but high concentration blocks conviction"], "risk": ["high adjusted holder concentration"]},
            "classification": {"verdict": "avoid", "flow": {"severity": 0, "volume_liquidity_ratio": 2.1, "tx_count": 1000, "avg_tx_usd": 300}, "risk_flags": []},
            "token_scan": {"holder_resolution": {"adjusted_discretionary_pct": 58.6}},
            "owner_exposure": {
                "owner_wallet_count": 5,
                "owner_wallet_hit_count": 1,
                "owner_wallet_hits": [{"wallet": "owner111", "label": "owner", "amount": 22_000_000}],
            },
            "x_attention": {
                "success": True,
                "answer": "Toly reply and quote cascade around SOLANGELES screenshot; soft-shill attention is live.",
                "citations": [{"title": "Toly quote", "url": "https://x.com/example/status/1"}],
            },
        }

    def fitness_fixture(self) -> dict:
        return {
            "mint": "9af7PmWRca2QYmknQehoLH19jG5ss9ajYFpgL8dMpump",
            "market": {"symbol": "FITNESS", "market_cap": 672_000, "liquidity_usd": 65_000, "price_change_h1": 18},
            "classification": {"flow": {"severity": 5, "volume_liquidity_ratio": 17.3, "tx_count": 52_000, "avg_tx_usd": 22}, "risk_flags": []},
            "wallet_timing": {"quality_wallet_hit_count": 1, "watch_wallet_hit_count": 1},
            "x_attention": {"success": True, "answer": "dev stream plus community grind; real attention despite churn", "citations": []},
        }

    def test_position_gate_separates_entry_avoid_from_owner_manage(self):
        result = self.solangeles_fixture()
        result["entry_gate"] = normalize_entry_gate(result["gate"], result["classification"])
        result["social_catalyst"] = classify_social_catalyst(result)
        result["flow_conversion"] = classify_flow_conversion(result)
        pos = analyze_position_context(result)
        self.assertEqual(result["entry_gate"]["action"], "avoid-entry")
        self.assertEqual(pos["position_action"], "manage")
        self.assertTrue(pos["owner_exposed"])
        self.assertEqual(pos["owner_wallet_hit_count"], 1)
        self.assertIn("owner exposed", pos["position_why"])
        self.assertTrue(pos["private_owner_context"])

    def test_social_catalyst_classifies_toly_quote_cascade(self):
        cat = classify_social_catalyst(self.solangeles_fixture())
        self.assertEqual(cat["catalyst_type"], "soft-shill")
        self.assertEqual(cat["catalyst_subtype"], "quote-cascade")
        self.assertEqual(cat["source_quality"], "high")
        self.assertEqual(cat["fragility"], "high")

    def test_social_links_alone_do_not_become_catalyst(self):
        cat = classify_social_catalyst({"market": {"socials": [{"type": "twitter", "url": "https://x.com/example"}]}, "x_attention": None})
        self.assertEqual(cat["catalyst_type"], "none")
        self.assertTrue(cat["raw_evidence_present"])

    def test_spam_and_generic_pump_mentions_do_not_upgrade_to_major_catalyst(self):
        spam = classify_social_catalyst({"x_attention": {"answer": "spam raid bot replies but Toly quote cascade screenshot"}, "market": {}})
        self.assertEqual(spam["catalyst_type"], "spam-raid")
        generic = classify_social_catalyst({"x_attention": {"answer": "posted on pump.fun with contract CA only, no major account"}, "market": {}})
        self.assertEqual(generic["catalyst_type"], "none")

    def test_fake_flow_can_be_attention_converting(self):
        result = self.fitness_fixture()
        result["social_catalyst"] = classify_social_catalyst(result)
        flow = classify_flow_conversion(result)
        self.assertEqual(flow["fake_flow_severity"], "high")
        self.assertIn(flow["conversion_status"], {"visibility-engine", "attention-converting"})
        self.assertEqual(flow["price_conversion"], "positive")

    def test_flow_profile_selects_most_severe_window(self):
        flow = flow_profile({
            "liquidity_usd": 100_000,
            "volume_h24": 100_000,
            "txns_h24": {"buys": 10, "sells": 10},
            "volume_h1": 3_000_000,
            "txns_h1": {"buys": 30_000, "sells": 30_000},
        })
        self.assertEqual(flow["window"], "h1")
        self.assertGreaterEqual(flow["severity"], 3)

    def test_wallet_timing_preserves_counts_and_marks_transfer_recipient(self):
        existing = {
            "quality_wallet_hit_count": 1,
            "watch_wallet_hit_count": 1,
            "quality_wallet_hits": [{"wallet": "wallet111", "label": "Alon", "role": "deep-first", "kind": "quality", "source": "final_study_set.json"}],
            "top_holders_sample": [{"owner": "wallet111", "amount": 22_000_000}],
        }
        result = {"owner_exposure": {"owner_wallet_hits": []}, "social_catalyst": {"catalyst_type": "dev-stream"}}
        enriched = enrich_wallet_timing(existing, result)
        self.assertEqual(enriched["quality_wallet_hit_count"], 1)
        self.assertEqual(enriched["wallet_timing"][0]["status"], "early-holder")
        self.assertIn("timing_summary", enriched)

    def test_delta_tracker_flags_catalyst_and_position_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            day = root / "2026-06-25"
            day.mkdir()
            prev = {
                "mint": MINT,
                "generated_at": "2026-06-25T00:00:00+00:00",
                "market": {"market_cap": 2_700_000, "liquidity_usd": 200_000},
                "entry_gate": {"action": "avoid-entry"},
                "position_context": {"position_action": "no-position", "owner_wallet_hit_count": 0},
                "social_catalyst": {"catalyst_type": "none"},
                "wallet_timing": {"quality_wallet_hit_count": 4},
                "flow_conversion": {"volume_liquidity_ratio": 2.0},
            }
            (day / f"token_event_SOLANGELES_{MINT[:8]}_20260625T000000Z.json").write_text(json.dumps(prev))
            cur = self.solangeles_fixture()
            cur["entry_gate"] = {"action": "avoid-entry"}
            cur["position_context"] = {"position_action": "manage", "owner_wallet_hit_count": 1}
            cur["social_catalyst"] = {"catalyst_type": "soft-shill"}
            cur["wallet_timing"] = {"quality_wallet_hit_count": 5}
            cur["flow_conversion"] = {"volume_liquidity_ratio": 2.1}
            delta = track_token_delta(cur, root)
            self.assertTrue(delta["useful"])
            self.assertEqual(delta["catalyst_delta"], "none → soft-shill")
            self.assertTrue(delta["position_action_changed"])


if __name__ == "__main__":
    unittest.main()
