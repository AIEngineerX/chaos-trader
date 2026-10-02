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

import wallet_quality_report  # noqa: E402

W1 = "Wallet111111111111111111111111111111111111111"
W2 = "Wallet222222222222222222222222222222222222222"
W3 = "Wallet333333333333333333333333333333333333333"


def make_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE positions(
            wallet TEXT,
            mint TEXT,
            status TEXT,
            buy_count INTEGER,
            sell_count INTEGER,
            sol_spent REAL,
            sol_received REAL,
            realized_pnl_sol REAL,
            hold_seconds INTEGER,
            transfer_contaminated INTEGER
        );
        CREATE TABLE wallet_edges(
            src_wallet TEXT,
            dst_wallet TEXT,
            edge_type TEXT,
            source_id TEXT,
            shared_mints INTEGER
        );
        """
    )
    return con


class WalletQualityReportTests(unittest.TestCase):
    def test_copyable_candidate_requires_realized_exits_pnl_and_low_contamination(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "wallets.sqlite"
            con = make_db(db)
            for idx, pnl in enumerate([1.2, 0.8, 2.0, -0.3]):
                con.execute(
                    "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (W1, f"Mint{idx}", "closed", 1, 1, 1.0, 1.0 + pnl, pnl, 600 + idx, 0),
                )
            con.commit(); con.close()
            payload = wallet_quality_report.run(db)
            row = payload["rows"][0]
            self.assertEqual(row["wallet"], W1)
            self.assertEqual(row["verdict"], "copyable-candidate")
            self.assertGreater(row["transfer_adjusted_pnl_sol"], 0)
            self.assertGreaterEqual(row["win_rate"], 0.5)
            self.assertEqual(row["transfer_contamination_rate"], 0.0)

    def test_transfer_contamination_demotes_even_with_raw_pnl(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "wallets.sqlite"
            con = make_db(db)
            for idx, contaminated in enumerate([1, 1, 1, 0]):
                con.execute(
                    "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (W2, f"Mint{idx}", "closed", 1, 1, 1.0, 4.0, 3.0, 1200, contaminated),
                )
            con.commit(); con.close()
            row = wallet_quality_report.run(db)["rows"][0]
            self.assertNotEqual(row["verdict"], "copyable-candidate")
            self.assertIn("high_transfer_contamination", row["negatives"])
            self.assertIn("transfer_contaminated", row["hard_flags"])

    def test_cluster_sensor_not_copy_wallet_without_realized_exits(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "wallets.sqlite"
            con = make_db(db)
            con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)", (W3, "MintA", "open", 1, 0, 1.0, 0.0, 0.0, None, 0))
            con.execute("INSERT INTO wallet_edges VALUES(?,?,?,?,?)", (W3, W1, "shared_mint_overlap", "helius_rpc", 45))
            con.commit(); con.close()
            row = wallet_quality_report.run(db)["rows"][0]
            self.assertEqual(row["verdict"], "cluster-sensor")
            self.assertIn("strong_cluster_sensor", row["positives"])
            self.assertIn("insufficient_realized_exits", row["negatives"])

    def test_missing_tables_and_missing_db_are_handled(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "empty.sqlite"
            con = sqlite3.connect(db); con.close()
            payload = wallet_quality_report.run(db)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["wallets_scored"], 0)
            missing = wallet_quality_report.run(Path(td) / "missing.sqlite")
            self.assertFalse(missing["ok"])

    def test_cli_raw_and_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "wallets.sqlite"
            con = make_db(db)
            con.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)", (W1, "MintA", "closed", 1, 1, 1.0, 2.0, 1.0, 300, 0))
            con.commit(); con.close()
            raw = subprocess.run([sys.executable, str(SCRIPT_DIR / "wallet_quality_report.py"), "--db", str(db), "--raw"], text=True, capture_output=True, check=False)
            self.assertEqual(raw.returncode, 0, raw.stderr)
            payload = json.loads(raw.stdout)
            self.assertTrue(payload["ok"])
            md = subprocess.run([sys.executable, str(SCRIPT_DIR / "wallet_quality_report.py"), "--db", str(db)], text=True, capture_output=True, check=False)
            self.assertEqual(md.returncode, 0, md.stderr)
            self.assertIn("Chaos Wallet Quality v1", md.stdout)
            self.assertIn("no execution", md.stdout.lower())


if __name__ == "__main__":
    unittest.main()
