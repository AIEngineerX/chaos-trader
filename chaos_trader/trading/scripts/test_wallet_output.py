#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wallet_output import (
    DEFAULT_WATCHLIST_DIR,
    build_wallet_scan_render_descriptor,
    normalize_wallet_records,
    render_wallet_scan_telegram,
    write_wallet_artifacts,
)
from tg_card_policy import validate_button_policy

W1 = "11111111111111111111111111111111"
W2 = "22222222222222222222222222222222"
W3 = "33333333333333333333333333333333"


class WalletOutputTests(unittest.TestCase):
    def test_top_holders_are_discovery_only_and_bot_card_style(self) -> None:
        records = [
            {"wallet": W1, "quality_label": "top_holder", "confidence": "medium"},
            {"wallet": W2, "quality_label": "top_holder", "confidence": "low"},
        ]
        msg = render_wallet_scan_telegram({"scan_name": "holders", "source": "token top holders"}, records)
        self.assertTrue(msg.startswith("💊 holders"))
        self.assertIn("🎯 Wallets: 2 • discovery only", msg)
        self.assertLess(msg.index("🎯 Wallets"), msg.index("```text\n" + W1))
        self.assertNotIn("| rank |", msg)
        self.assertIn("Top holder / early buyer ≠ good wallet", msg)

    def test_early_buyer_does_not_become_copyable(self) -> None:
        records = [{"wallet": W1, "quality_label": "early_buyer", "copyability_score": 99}]
        normalized = normalize_wallet_records(records)
        self.assertEqual(normalized[0]["quality_label"], "early_buyer")
        msg = render_wallet_scan_telegram({"scan_name": "early"}, records)
        self.assertIn("early_buyer", msg)
        self.assertIn("🎯 Wallets: 1 • discovery only", msg)
        self.assertNotIn("copyable_candidate", msg)

    def test_pnl_verified_wallet_can_display_when_evidence_exists(self) -> None:
        records = [{
            "wallet": W1,
            "quality_label": "pnl_verified_onchain",
            "realized_pnl_sol": 12.5,
            "transfer_adjusted": True,
            "win_rate": 0.61,
            "trade_count": 44,
            "dead_bag_rate": 0.18,
            "confidence": "high",
        }]
        normalized = normalize_wallet_records(records)
        self.assertEqual(normalized[0]["quality_label"], "pnl_verified_onchain")
        msg = render_wallet_scan_telegram({"scan_name": "pnl"}, records)
        self.assertIn("🎯 Wallets: 1 • PnL-verified", msg)
        self.assertIn("pnl_verified_onchain", msg)

    def test_missing_pnl_evidence_downgrades_claim(self) -> None:
        records = [{"wallet": W1, "quality_label": "pnl_verified_onchain", "source": "external leaderboard"}]
        normalized = normalize_wallet_records(records)
        self.assertEqual(normalized[0]["quality_label"], "top_pnl_claimed_external")
        self.assertEqual(normalized[0]["verdict"], "investigate")
        self.assertIn("downgraded", normalized[0]["reason"])

    def test_sniper_or_custom_program_flag_forces_do_not_copy(self) -> None:
        records = [{"wallet": W1, "quality_label": "copyable_candidate", "risk_flags": ["sniper"]}]
        normalized = normalize_wallet_records(records)
        self.assertEqual(normalized[0]["quality_label"], "do_not_copy")
        self.assertEqual(normalized[0]["verdict"], "avoid")
        msg = render_wallet_scan_telegram({"scan_name": "sniper"}, records)
        self.assertIn("do_not_copy", msg)
        self.assertIn("avoid", msg)

    def test_copyable_candidate_missing_evidence_is_blocked(self) -> None:
        records = [{"wallet": W1, "quality_label": "copyable_candidate", "copyability_score": 88}]
        normalized = normalize_wallet_records(records)
        self.assertEqual(normalized[0]["quality_label"], "do_not_copy")
        self.assertIn("insufficient_copyability_evidence", normalized[0]["risk_flags"])

    def test_mixed_scan_does_not_overstate_all_wallet_quality(self) -> None:
        records = [
            {"wallet": W1, "quality_label": "top_holder"},
            {
                "wallet": W2,
                "quality_label": "pnl_verified_onchain",
                "realized_pnl_sol": 12.5,
                "transfer_adjusted": True,
                "win_rate": 0.61,
                "trade_count": 44,
                "dead_bag_rate": 0.18,
            },
        ]
        msg = render_wallet_scan_telegram({"scan_name": "mixed"}, records)
        self.assertIn("🎯 Wallets: 2 • mixed; PnL-verified evidence present", msg)

    def test_token_alert_card_matches_tg_bot_shape(self) -> None:
        records = [{"wallet": W1, "quality_label": "top_holder"}]
        msg = render_wallet_scan_telegram({
            "title": "first dolphin on win..",
            "headline_metric": "22.8K",
            "headline_change": "-37%",
            "symbol": "KAIRU",
            "chain": "Solana",
            "venue": "Pump",
            "usd_price": "0.00002284",
            "fdv": "22.8K",
            "fdv_to": "39K",
            "fdv_window": "4m",
            "liquidity": "5.9K",
            "liquidity_mult": "x4",
            "volume": "43.3K",
            "age": "19mo",
            "price_change_1h": "-36.8%",
            "buys": 461,
            "sells": 334,
            "top_holders": ["3.4", "3.4", "3.1", "3.1", "3.1"],
            "top_holder_total": "27%",
            "total_holders": 244,
            "avg_holder_age": "22w",
            "fresh_1d": "5%",
            "fresh_7d": "12%",
            "chart_links": ["DEX", "DEF"],
            "more_links": ["🫧", "🎨", "💬", "🌍", "🐦", "[↻]"],
            "token_address": "Ap3jN5zsj3vpR4r4qvq26iyFGv2vAf1XWLn8QWhTpump",
            "code_links": ["MAE", "BAN", "BNK", "PDR", "BLO", "STB", "OKX"],
        }, records)
        self.assertIn("💊 first dolphin on win.. [22.8K/-37%]", msg)
        self.assertIn("$KAIRU", msg)
        self.assertIn("🟪 Solana @ Pump", msg)
        self.assertIn("💎 FDV: 22.8K ⇨ 39K [4m]", msg)
        self.assertIn("👥 TH: 3.4•3.4•3.1•3.1•3.1 [27%]", msg)
        self.assertIn("Ap3jN5zsj3vpR4r4qvq26iyFGv2vAf1XWLn8QWhTpump", msg)
        self.assertIn("MAE•BAN•BNK•PDR•BLO•STB•OKX", msg)

    def test_large_list_writes_txt_csv_json_import_and_report(self) -> None:
        records = [
            {"wallet": W1, "quality_label": "raw_holder"},
            {"wallet": W2, "quality_label": "early_buyer"},
            {"wallet": W3, "quality_label": "top_holder"},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts = write_wallet_artifacts(
                "My Scan: Test/Unsafe Name",
                records,
                {"scan_name": "My Scan", "source": "unit test"},
                watchlist_dir=root / "watchlists",
                report_dir=root / "reports",
            )
            for path in [artifacts.txt, artifacts.csv, artifacts.json, artifacts.import_json, artifacts.report]:
                self.assertTrue(path.exists(), path)
                self.assertGreater(path.stat().st_size, 0)
            self.assertEqual(artifacts.txt.read_text(encoding="utf-8").splitlines(), [W1, W2, W3])
            full = json.loads(artifacts.json.read_text(encoding="utf-8"))
            minimal = json.loads(artifacts.import_json.read_text(encoding="utf-8"))
            self.assertEqual(full[0]["wallet"], W1)
            self.assertEqual(minimal[0]["trackedWalletAddress"], W1)
            self.assertIn("wallet,name,emoji,tags,quality_label", artifacts.csv.read_text(encoding="utf-8").splitlines()[0])

    def test_empty_scan_makes_no_fake_quality(self) -> None:
        msg = render_wallet_scan_telegram({"scan_name": "empty"}, [])
        self.assertIn("No wallets qualified", msg)
        self.assertIn("🎯 Wallets: 0 • discovery only", msg)
        self.assertNotIn("pnl_verified_onchain", msg)

    def test_policy_blocks_execution_and_deep_links(self) -> None:
        bad = [
            {"text": "Buy", "url": "https://dexscreener.com/solana/abc"},
            {"text": "Open chart: t.me", "url": "https://t.me/evil?start=abc"},
            {"text": "Open chart: dexscreener.com", "url": "https://bit.ly/x"},
            {"text": "Open chart: dexscreener.com", "url": "http://dexscreener.com/solana/abc"},
            {"text": "Open chart: dexscreener.com", "url": "https://dexscreener.com.evil.tld/solana/abc"},
            {"text": "Open Padre: trade.padre.gg", "url": "https://trade.padre.gg.evil.tld/index.html"},
            {"text": "Open Padre", "url": "https://trade.padre.gg/index.html"},
            {"text": "Open chart: dexscreener.com", "url": "https://dexscreener.com/solana/abc?cluster=shadow"},
        ]
        for button in bad:
            self.assertEqual(validate_button_policy(button).decision, "block", button)

    def test_policy_allows_copy_and_visible_read_only_urls(self) -> None:
        self.assertEqual(validate_button_policy({"text": "Copy CA", "copy_text": "abc"}).decision, "allow")
        self.assertEqual(validate_button_policy({
            "text": "Open DEX: dexscreener.com",
            "url": "https://dexscreener.com/solana/abc",
        }).decision, "allow")
        self.assertEqual(validate_button_policy({
            "text": "Open SOL: solscan.io",
            "url": "https://solscan.io/token/abc",
        }).decision, "allow")
        self.assertEqual(validate_button_policy({
            "text": "Open Padre: trade.padre.gg",
            "url": "https://trade.padre.gg/index.html",
        }).decision, "block")

    def test_policy_downgrades_oversized_copy_payload(self) -> None:
        decision = validate_button_policy({"text": "Copy wallets", "copy_text": "x" * 257})
        self.assertEqual(decision.decision, "downgrade_to_plain_text")

    def test_descriptor_contains_read_only_buttons_artifacts_and_visible_ca(self) -> None:
        records = [{"wallet": W1, "quality_label": "top_holder", "risk_flags": ["fresh"]}]
        artifacts = {
            "csv": str(DEFAULT_WATCHLIST_DIR / "descriptor.csv"),
            "json": str(DEFAULT_WATCHLIST_DIR / "descriptor.json"),
            "txt": str(DEFAULT_WATCHLIST_DIR / "descriptor.txt"),
            "report": str(DEFAULT_WATCHLIST_DIR / "not_allowed.md"),
        }
        descriptor = build_wallet_scan_render_descriptor(
            {
                "scan_name": "descriptor",
                "mint": "So11111111111111111111111111111111111111112",
                "risk_flags": ["fresh", "dev unresolved"],
            },
            records,
            artifacts,
        )
        self.assertEqual(descriptor["format"], "plain")
        self.assertEqual(descriptor["link_preview"], {"disabled": True})
        self.assertIn("So11111111111111111111111111111111111111112", descriptor["text"])
        flat = [button for row in descriptor["buttons"] for button in row]
        labels = [button["text"] for button in flat]
        self.assertIn("Copy CA", labels)
        self.assertIn("Open DEX: dexscreener.com", labels)
        self.assertNotIn("Open Padre: trade.padre.gg", labels)
        self.assertIn("Open SOL: solscan.io", labels)
        self.assertIn("Copy wallets", labels)
        copy_wallets = next(button for button in flat if button["text"] == "Copy wallets")
        self.assertEqual(copy_wallets["copy_text"], W1)
        self.assertIn("Show risk notes", labels)
        self.assertFalse(any(label.lower() in {"buy", "sell", "swap", "snipe", "copy trade"} for label in labels))
        self.assertTrue(all(artifact["type"] in {"csv", "json", "txt"} for artifact in descriptor["artifacts"]))
        self.assertEqual(
            {
                str((DEFAULT_WATCHLIST_DIR / "descriptor.csv").resolve()),
                str((DEFAULT_WATCHLIST_DIR / "descriptor.json").resolve()),
                str((DEFAULT_WATCHLIST_DIR / "descriptor.txt").resolve()),
            },
            {artifact["path"] for artifact in descriptor["artifacts"]},
        )


if __name__ == "__main__":
    unittest.main()
