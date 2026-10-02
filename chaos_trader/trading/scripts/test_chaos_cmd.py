#!/usr/bin/env python3
"""Golden-contract tests for Chaos command-router Telegram output."""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import chaos_cmd

MINT = "FMqh9mqR6drPZqqW6wPqLHxX4rqNDWGhYLaMfoaJpump"


class ChaosCommandOutputTests(unittest.TestCase):
    def sample_token_payload(self) -> dict:
        return {
            "mint": MINT,
            "market": {
                "symbol": "world",
                "liquidity_usd": 148000,
                "market_cap": 2980000,
                "price_change_h1": 10.34,
                "url": f"https://dexscreener.com/solana/{MINT}",
            },
            "classification": {
                "verdict": "exit-liquidity-watch",
                "attention_phase": "late",
                "score": 0,
                "why_not_watch": "surface momentum lacks organic-flow validation.",
                "reasons": ["liquidity >= 25k"],
                "risk_flags": ["micro-churn"],
                "flow": {
                    "volume_liquidity_ratio": 24.3,
                    "avg_tx_usd": 92,
                    "tx_count": 39160,
                    "flags": ["elevated volume/liquidity churn 24.3x"],
                },
                "x_risk": {"confidence": "low", "flags": []},
            },
            "wallet_timing": {"watch_wallet_hit_count": 0},
            "x_attention": {"citations": [], "inline_citations": []},
            "markdown_path": "/tmp/token.md",
        }

    def test_token_card_is_copy_first_and_read_only(self):
        msg = chaos_cmd.compact_token(self.sample_token_payload())
        self.assertTrue(msg.startswith("☄️"))
        self.assertIn("📋 Copy CA", msg)
        self.assertIn(f"```text\n{MINT}\n```", msg)
        self.assertIn("🧭 Open:", msg)
        self.assertIn("[DEX]", msg)
        self.assertIn("[SOL]", msg)
        self.assertNotIn("Padre", msg)
        self.assertIn("⚡ Next", msg)
        self.assertIn(f"analyze token {MINT}", msg)
        self.assertTrue(msg.rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))
        self.assertNotIn("Files:", msg)

    def test_token_card_prints_the_timing_label_once(self):
        cases = [
            ({"quality_wallet_hit_count": 1, "timing_label": "timing unresolved"}, "WALLETS: Q 1 · timing unresolved · owner 0/0"),
            ({"quality_wallet_hit_count": 0}, "WALLETS: Q 0 · timing unresolved · owner 0/0"),
            ({"quality_wallet_hit_count": 1, "timing_label": "transfer-recipient"}, "WALLETS: Q 1 · timing transfer-recipient · owner 0/0"),
            ({"quality_wallet_hit_count": 0, "timing_label": "none"}, "WALLETS: Q 0 · timing none · owner 0/0"),
        ]
        for wallet_timing, line in cases:
            with self.subTest(label=wallet_timing.get("timing_label")):
                payload = self.sample_token_payload()
                payload["wallet_timing"] = wallet_timing
                msg = chaos_cmd.compact_token(payload)
                self.assertIn(line, msg.splitlines())
                self.assertEqual(msg.count("timing unresolved"), 1 if "unresolved" in line else 0)
                self.assertNotIn("timing timing", msg)

    def test_token_artifact_is_opt_in(self):
        msg = chaos_cmd.compact_token(self.sample_token_payload(), include_artifact=True)
        self.assertIn("Files: `/tmp/token.md`", msg)

    def test_failed_chain_reads_show_on_the_card(self):
        chain = "CHAIN: unavailable (on-chain reads failed; see the saved JSON)"
        for key in ("token_scan_error", "pumpfun_error"):
            with self.subTest(key=key):
                payload = self.sample_token_payload()
                payload[key] = "timeout after 75s"
                self.assertIn(chain, chaos_cmd.compact_token(payload).splitlines())
        self.assertNotIn(chain, chaos_cmd.compact_token(self.sample_token_payload()))
        # A holder line already says what failed, so the chain line does not repeat it.
        payload = self.sample_token_payload()
        payload.update(token_scan_error="rate limited", holder_data="unavailable (rate limited)")
        lines = chaos_cmd.compact_token(payload).splitlines()
        self.assertIn("HOLDERS: unavailable (rate limited)", lines)
        self.assertNotIn(chain, lines)

    def test_child_timeout_is_one_line_not_a_traceback(self):
        # A real child that outlives its bound; run_raw appends --raw, which python -c ignores.
        with self.assertRaises(SystemExit) as raised:
            chaos_cmd.run_raw(["-c", "import time; time.sleep(30)"], timeout=1)
        self.assertEqual(str(raised.exception.code), "☄️ Chaos command timed out after 1s. Raise --timeout, or check the RPC.")

    def test_markdown_open_rows_ignore_malicious_market_url(self):
        payload = self.sample_token_payload()
        payload["market"]["url"] = "https://evil.example/path"

        token_msg = chaos_cmd.compact_token(payload)
        self.assertNotIn("https://evil.example/path", token_msg)
        self.assertIn(f"https://dexscreener.com/solana/{MINT}", token_msg)
        self.assertIn(f"https://solscan.io/token/{MINT}", token_msg)

        sweep_msg = chaos_cmd.compact_sweep({"candidate_count": 1, "x_enabled": False, "ranked_candidates": [], "deep_reads": [payload]})
        self.assertNotIn("https://evil.example/path", sweep_msg)
        self.assertIn(f"https://dexscreener.com/solana/{MINT}", sweep_msg)

    def test_sweep_card_has_raw_ca_list_and_next_commands(self):
        payload = {
            "candidate_count": 1,
            "x_enabled": False,
            "ranked_candidates": [],
            "deep_reads": [self.sample_token_payload()],
        }
        msg = chaos_cmd.compact_sweep(payload)
        self.assertIn("☄️ TREND SWEEP", msg)
        self.assertIn("📋 Copy CA list", msg)
        self.assertIn(f"```text\n{MINT}\n```", msg)
        self.assertIn("⚡ Next", msg)
        self.assertIn(f"analyze token {MINT}", msg)
        self.assertNotRegex(msg.lower(), r"\b(buy|sell|swap|copy\s*trade|snipe|execute)\b")

    def test_token_render_descriptor_has_clean_buttons_and_plain_text(self):
        descriptor = chaos_cmd.token_render_descriptor(self.sample_token_payload())
        self.assertEqual(descriptor["format"], "plain")
        self.assertEqual(descriptor["link_preview"], {"disabled": True})
        self.assertIn(MINT, descriptor["text"])
        self.assertNotIn("[DEX](", descriptor["text"])
        flat = [button for row in descriptor["buttons"] for button in row]
        labels = [button["text"] for button in flat]
        self.assertIn("Copy CA", labels)
        self.assertIn("Open DEX: dexscreener.com", labels)
        self.assertIn("Open SOL: solscan.io", labels)
        self.assertNotIn("Open Padre: trade.padre.gg", labels)
        self.assertIn("Show risk notes", labels)
        self.assertNotRegex(" ".join(labels).lower(), r"\b(buy|sell|swap|copy\s*trade|snipe|execute)\b")

    def test_sweep_render_descriptor_targets_top_ca_without_execution_links(self):
        payload = {
            "candidate_count": 1,
            "x_enabled": False,
            "ranked_candidates": [],
            "deep_reads": [self.sample_token_payload()],
        }
        descriptor = chaos_cmd.sweep_render_descriptor(payload)
        self.assertIn("Buttons target top CA.", descriptor["text"])
        self.assertTrue(descriptor["text"].rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))
        flat = [button for row in descriptor["buttons"] for button in row]
        labels = [button["text"] for button in flat]
        self.assertIn("Copy CA list", labels)
        self.assertNotIn("Open Padre: trade.padre.gg", labels)
        self.assertFalse(any("padre" in str(button.get("url", "")).lower() for button in flat))

    def test_alpha_sweep_with_filtered_traps_does_not_copy_trap_as_opportunity(self):
        payload = {
            "candidate_count": 1,
            "x_enabled": False,
            "sweep_mode": "alpha",
            "caps": {"deep": 1},
            "ranked_candidates": [{"mint": MINT, "candidate_score": 999, "summary": {"liquidity_usd": 20_000, "marketCap": 64_000}}],
            "deep_reads": [],
            "filtered_deep_reads": [{"mint": MINT, "symbol": "EAGLE250", "label": "avoid", "why_not_watch": "surface momentum lacks organic-flow validation."}],
        }
        msg = chaos_cmd.compact_sweep(payload)
        self.assertIn("No alpha candidates passed the organic-flow gate.", msg)
        self.assertIn("sweep --mode trap", msg)
        self.assertNotIn("📋 Copy CA list", msg)
        self.assertNotIn(f"analyze token {MINT}", msg)
        self.assertTrue(msg.rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))

    def test_invalid_mint_fails_closed_before_script_call(self):
        self.assertFalse(chaos_cmd.MINT_RE.match("not-a-mint"))

    def test_x_context_is_opt_in_for_command_router(self):
        with mock.patch.dict(os.environ, {"X_SEARCH_PROVIDER": "xai"}):
            self.assertFalse(chaos_cmd.x_requested(argparse.Namespace(with_x=False, no_x=False)))
            self.assertTrue(chaos_cmd.x_requested(argparse.Namespace(with_x=True, no_x=False)))
            self.assertFalse(chaos_cmd.x_requested(argparse.Namespace(with_x=True, no_x=True)))

    def test_loose_parse_splits_token_and_analyze_intent(self):
        self.assertEqual(chaos_cmd.parse_loose(["token", MINT]), ["token", MINT])
        self.assertEqual(chaos_cmd.parse_loose(["ca", MINT]), ["token", MINT])
        self.assertEqual(chaos_cmd.parse_loose(["analyze", "token", MINT]), ["analyze", MINT])
        self.assertEqual(chaos_cmd.parse_loose(["scan", "trends"]), ["sweep"])


if __name__ == "__main__":
    unittest.main()
