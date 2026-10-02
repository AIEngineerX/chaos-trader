#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alpha_paper_trade


class AlphaPaperTradeTests(unittest.TestCase):
    def make_db(self, path: Path) -> None:
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE token_signals (
                mint TEXT, signal_type TEXT, wallet_count INTEGER, tg_channel_count INTEGER,
                total_sol_amount REAL, call_market_cap_usd REAL, current_market_cap_usd REAL,
                ath_market_cap_usd REAL, ath_multiplier REAL, is_hit INTEGER,
                first_buy_utc TEXT, created_at_utc TEXT, captured_at_utc TEXT
            );
            CREATE TABLE tokens (mint TEXT, symbol TEXT, name TEXT);
        """)
        rows = [
            ("MintRunner111111111111111111111111111111", "multi_buy", 4, 3, 10.0, 80_000, 240_000, 400_000, 5.0, 1, "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", "RUN"),
            ("MintLoser1111111111111111111111111111111", "multi_buy", 3, 1, 5.0, 100_000, 30_000, 120_000, 1.2, 0, "2026-06-01T01:00:00+00:00", "2026-06-01T01:00:00+00:00", "2026-06-01T01:00:00+00:00", "LOSE"),
            ("MintSkip11111111111111111111111111111111", "single", 1, 0, 1.0, 90_000, 95_000, 100_000, 1.1, 0, "2026-06-01T02:00:00+00:00", "2026-06-01T02:00:00+00:00", "2026-06-01T02:00:00+00:00", "SKIP"),
        ]
        for r in rows:
            con.execute("INSERT INTO token_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r[:-1])
            con.execute("INSERT INTO tokens VALUES (?,?,?)", (r[0], r[-1], r[-1]))
        con.commit(); con.close()

    def test_simulates_policy_without_execution(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            self.make_db(db)
            payload = alpha_paper_trade.run(db, limit=20, min_wallets=3, min_tg=1)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["paper_trades"], 2)
        self.assertEqual(payload["signals_skipped"], 1)
        self.assertEqual(payload["summary"]["take_profit_hits"], 1)
        self.assertIn("no wallet", payload["boundary"])
        self.assertIn("live execution", payload["boundary"])
        md = alpha_paper_trade.render_md(payload)
        self.assertIn("Paper Trade Simulation", md)
        self.assertIn("paper-only", md)

    def test_limit_uses_newest_signals_then_processes_chronologically(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            self.make_db(db)
            con = sqlite3.connect(db)
            con.execute("INSERT INTO token_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "OldMint111111111111111111111111111111111", "multi_buy", 10, 10, 1.0, 50_000, 500_000, 500_000, 10.0, 1,
                "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"
            ))
            con.execute("INSERT INTO tokens VALUES (?,?,?)", ("OldMint111111111111111111111111111111111", "OLD", "OLD"))
            con.commit(); con.close()
            payload = alpha_paper_trade.run(db, limit=1, min_wallets=1, min_tg=0)
        self.assertEqual(payload["signals_seen"], 1)
        self.assertNotEqual(payload["rows"][0]["symbol"], "OLD")

    def test_run_clamps_invalid_policy_thresholds(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            self.make_db(db)
            payload = alpha_paper_trade.run(db, take_profit_x=0.1, stop_loss_pct=20, min_wallets=0, min_tg=-5)
        self.assertEqual(payload["policy"]["take_profit_x"], 1.01)
        self.assertEqual(payload["policy"]["stop_loss_pct"], 0.0)
        self.assertEqual(payload["policy"]["min_wallets"], 1)
        self.assertEqual(payload["policy"]["min_tg"], 0)

    def test_missing_db_is_explicit(self):
        payload = alpha_paper_trade.run(Path("/tmp/definitely-missing-chaos-paper.sqlite"))
        self.assertFalse(payload["ok"])
        self.assertIn("missing", payload["error"])

    def test_cli_raw_outputs_json(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            self.make_db(db)
            proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "alpha_paper_trade.py"), "--db", str(db), "--raw"], text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "chaos_alpha_paper_trade_v1")


if __name__ == "__main__":
    unittest.main()
