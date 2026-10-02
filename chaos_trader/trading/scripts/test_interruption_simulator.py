#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import event_outcome_tracker  # noqa: E402
import interruption_simulator  # noqa: E402


_OPEN: list[sqlite3.Connection] = []  # closed in tearDownModule, so no connection is left to the garbage collector


def make_con(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or ":memory:")
    _OPEN.append(con)
    con.row_factory = sqlite3.Row
    event_outcome_tracker.ensure_schema(con)
    return con


def insert_row(con: sqlite3.Connection, *, idx: int, event_type: str = "dex_boost", source_key: str = "dex_boosts_latest", ret: float = 50.0, status: str = "positive", valid: int = 1) -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
    event_id = f"cet1_sim_{idx}_{event_type}_{source_key}".replace("/", "_")
    con.execute(
        """
        INSERT INTO event_outcome_events(event_id,event_type,source,source_key,source_endpoint,chain_id,token_address,observed_at,outcome_start_at,baseline_status,raw_event_json,created_at_utc,updated_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, event_type, "dexscreener", source_key, "/token-boosts/latest/v1", "solana", f"Mint{idx}", now, now, "valid_baseline" if valid else "late_baseline", "{}", now, now),
    )
    con.execute(
        """
        INSERT INTO event_outcomes(event_id,window_label,checked_at_utc,age_seconds,return_pct,max_return_pct,max_drawdown_pct,dead_or_alive,outcome_status,calibration_valid,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, "1h", now, 3600, ret, max(ret, 0), min(ret, 0), "dead" if status == "illiquid_or_missing" else "alive", status, valid, "{}"),
    )
    con.commit()


class InterruptionSimulatorTests(unittest.TestCase):
    def test_positive_event_and_source_classes_simulate_interrupts(self):
        con = make_con()
        for i in range(5):
            insert_row(con, idx=i, ret=120 if i < 2 else 40, status="runner" if i < 2 else "positive")
        rows = interruption_simulator.load_event_rows(con, window="1h")
        source_rows = interruption_simulator.load_source_rows(con, window="1h")
        payload = interruption_simulator.simulate(rows, source_rows, min_count=5)
        self.assertEqual(payload["simulated_interrupts"], 5)
        self.assertGreater(payload["summary"]["runner_rate_pct"], 0)
        self.assertIn("lookahead_caveat", payload)

    def test_small_sample_blocks_interrupt_even_if_runner(self):
        con = make_con()
        insert_row(con, idx=1, ret=200, status="runner")
        payload = interruption_simulator.simulate(interruption_simulator.load_event_rows(con), interruption_simulator.load_source_rows(con), min_count=3)
        self.assertEqual(payload["simulated_interrupts"], 0)

    def test_negative_source_or_event_blocks_interrupt(self):
        con = make_con()
        for i in range(5):
            insert_row(con, idx=i, event_type="dex_ad", source_key="dex_ads_latest", ret=-80, status="severe_drawdown")
        payload = interruption_simulator.simulate(interruption_simulator.load_event_rows(con), interruption_simulator.load_source_rows(con), min_count=5)
        self.assertEqual(payload["simulated_interrupts"], 0)

    def test_late_baseline_excluded_by_default(self):
        con = make_con()
        for i in range(5):
            insert_row(con, idx=i, ret=120, status="runner", valid=0)
        payload = interruption_simulator.simulate(interruption_simulator.load_event_rows(con), interruption_simulator.load_source_rows(con), min_count=1)
        self.assertEqual(payload["rows_seen"], 0)
        self.assertEqual(payload["simulated_interrupts"], 0)

    def test_cli_raw(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "event_outcomes.sqlite"
            con = make_con(db)
            for i in range(2):
                insert_row(con, idx=i, ret=120, status="runner")
            con.close()
            proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "interruption_simulator.py"), "--db", str(db), "--raw", "--min-count", "1"], text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["ok"])
            self.assertIn("no alerts", payload["boundary"])


def tearDownModule() -> None:
    for con in _OPEN:
        con.close()


if __name__ == "__main__":
    unittest.main()
