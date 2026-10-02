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
import source_quality_report  # noqa: E402


_OPEN: list[sqlite3.Connection] = []  # closed in tearDownModule, so no connection is left to the garbage collector


def make_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    _OPEN.append(con)
    con.row_factory = sqlite3.Row
    event_outcome_tracker.ensure_schema(con)
    return con


def insert_outcome(con: sqlite3.Connection, *, event_id: str, source_key: str, event_type: str, ret: float, status: str = "positive", valid: int = 1) -> None:
    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")
    con.execute(
        """
        INSERT INTO event_outcome_events(event_id,event_type,source,source_key,source_endpoint,chain_id,token_address,observed_at,outcome_start_at,baseline_status,raw_event_json,created_at_utc,updated_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, event_type, "dexscreener", source_key, "/token-boosts/latest/v1", "solana", f"Mint{event_id}", now, now, "valid_baseline" if valid else "late_baseline", "{}", now, now),
    )
    con.execute(
        """
        INSERT INTO event_outcomes(event_id,window_label,checked_at_utc,age_seconds,return_pct,max_return_pct,max_drawdown_pct,dead_or_alive,outcome_status,calibration_valid,raw_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, "1h", now, 3600, ret, max(ret, 0), min(ret, 0), "dead" if status == "illiquid_or_missing" else "alive", status, valid, "{}"),
    )
    con.commit()


class SourceQualityReportTests(unittest.TestCase):
    def test_positive_source_requires_min_count_and_good_outcomes(self):
        con = make_con()
        for i in range(5):
            insert_outcome(con, event_id=f"pos{i}", source_key="dex_boosts_latest", event_type="dex_boost", ret=120 if i < 2 else 35, status="runner" if i < 2 else "positive")
        rows = source_quality_report.load_source_rows(con, window="1h")
        agg = source_quality_report.aggregate_sources(rows, min_count=5)
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]["verdict"], "positive-source-watch")
        self.assertGreater(agg[0]["score"], 50)

    def test_small_source_stays_unproven_even_if_positive(self):
        con = make_con()
        insert_outcome(con, event_id="one", source_key="dex_ads_latest", event_type="dex_ad", ret=200, status="runner")
        agg = source_quality_report.aggregate_sources(source_quality_report.load_source_rows(con), min_count=3)
        self.assertEqual(agg[0]["verdict"], "unproven-source")
        self.assertIn("sample below minimum", agg[0]["reasons"])

    def test_negative_source_is_flagged(self):
        con = make_con()
        for i in range(4):
            insert_outcome(con, event_id=f"neg{i}", source_key="dex_ads_latest", event_type="dex_ad", ret=-80, status="severe_drawdown")
        agg = source_quality_report.aggregate_sources(source_quality_report.load_source_rows(con), min_count=4)
        self.assertEqual(agg[0]["verdict"], "negative-source-avoid")

    def test_late_baselines_excluded_by_default(self):
        con = make_con()
        insert_outcome(con, event_id="late", source_key="dex_orders", event_type="dex_paid_order", ret=100, status="runner", valid=0)
        self.assertEqual(source_quality_report.load_source_rows(con), [])
        self.assertEqual(len(source_quality_report.load_source_rows(con, include_late_baseline=True)), 1)

    def test_cli_raw_on_temp_db(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "event_outcomes.sqlite"
            con = sqlite3.connect(db); con.row_factory = sqlite3.Row; event_outcome_tracker.ensure_schema(con)
            insert_outcome(con, event_id="cli", source_key="dex_boosts_latest", event_type="dex_boost", ret=25, status="positive")
            con.close()
            proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "source_quality_report.py"), "--db", str(db), "--raw", "--min-count", "1"], text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["sources_scored"], 1)


def tearDownModule() -> None:
    for con in _OPEN:
        con.close()


if __name__ == "__main__":
    unittest.main()
