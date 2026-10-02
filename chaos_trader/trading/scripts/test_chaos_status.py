#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import chaos_status


class ChaosStatusTests(unittest.TestCase):
    def test_signal_ledger_without_outcomes_reports_zero_not_error(self):
        with tempfile.TemporaryDirectory() as td:
            old_home = chaos_status.PROFILE_HOME
            try:
                home = Path(td)
                db_dir = home / "trading" / "db"
                db_dir.mkdir(parents=True)
                con = sqlite3.connect(db_dir / "signal_ledger.sqlite")
                con.execute("CREATE TABLE signals(id INTEGER PRIMARY KEY)")
                con.execute("INSERT INTO signals DEFAULT VALUES")
                con.commit(); con.close()
                chaos_status.PROFILE_HOME = home
                payload = chaos_status._signal_ledger_status()
            finally:
                chaos_status.PROFILE_HOME = old_home
        self.assertEqual(payload["signals"], 1)
        self.assertEqual(payload["outcomes"], 0)
        self.assertNotIn("error", payload)


if __name__ == "__main__":
    unittest.main()
