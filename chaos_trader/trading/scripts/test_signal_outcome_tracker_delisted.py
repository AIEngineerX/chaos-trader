#!/usr/bin/env python3
"""A pair gone from DexScreener inside the mark window is a -100% mark; an unanswered fetch is not.

The real `fetch_token` runs; only its HTTP call (`dexscreener_client.get_json`) is swapped, which is the
network boundary. DexScreener answers an unknown mint with `{"pairs": null}` on the search endpoint and `[]`
on the two token endpoints.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import signal_outcome_tracker  # noqa: E402
from signal_ledger import connect, record_signal  # noqa: E402
from signal_outcome_tracker import ensure_outcomes, track_signal  # noqa: E402

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINT = "8" * 32


def gone(path: str):
    return {"schemaVersion": "1.0.0", "pairs": None} if path.startswith("/latest/") else []


def unreachable(path: str):
    raise urllib.error.URLError("connection refused")


class DelistedMarkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "signal_ledger.sqlite"

    def seed(self, *, price: float | None) -> None:
        market = {"symbol": "GONE", "liquidity_usd": 10_000}
        if price is not None:
            market["price_usd"] = price
        record_signal({"generated_at": BASE.isoformat(), "mint": MINT, "market": market,
                       "classification": {"verdict": "watch", "flow": {}}}, source_command="token", db_path=self.db)

    def track(self, answer, *, at: datetime) -> tuple[list[dict], dict]:
        con = connect(self.db)
        try:
            ensure_outcomes(con)
            signal = dict(con.execute("SELECT * FROM signals").fetchone())
            with patch("signal_outcome_tracker.now", return_value=at), \
                 patch("signal_outcome_tracker.time.sleep"), \
                 patch("dexscreener_client.get_json", side_effect=answer):
                written = track_signal(con, signal)
            row = dict(con.execute("SELECT * FROM outcomes WHERE window_label='15m'").fetchone())
        finally:
            con.close()
        return written, row

    def test_gone_inside_the_window_is_a_minus_100_eligible_mark(self):
        self.seed(price=1.0)
        written, row = self.track(gone, at=BASE + timedelta(minutes=16))
        self.assertEqual("delisted", row["outcome_status"])
        self.assertEqual(-100.0, row["return_pct"])
        self.assertEqual(1, row["primary_eligible"])
        self.assertEqual(0, row["late_snapshot"])
        self.assertEqual("dead", row["dead_or_alive"])
        self.assertIsNone(row["price_usd_now"])
        self.assertFalse(written[0]["fetch_failed"])

    def test_gone_without_a_scan_price_stays_missing(self):
        self.seed(price=None)
        _written, row = self.track(gone, at=BASE + timedelta(minutes=16))
        self.assertEqual("missing", row["outcome_status"])
        self.assertIsNone(row["return_pct"])
        self.assertEqual(0, row["primary_eligible"])

    def test_an_unanswered_fetch_is_missing_not_delisted(self):
        self.seed(price=1.0)
        written, row = self.track(unreachable, at=BASE + timedelta(minutes=16))
        self.assertEqual("missing", row["outcome_status"])
        self.assertIsNone(row["return_pct"])
        self.assertEqual(0, row["primary_eligible"])
        self.assertTrue(written[0]["fetch_failed"])

    def test_one_failed_endpoint_with_no_pairs_is_not_delisted(self):
        self.seed(price=1.0)

        def partly(path: str):
            if path.startswith("/tokens/"):
                raise urllib.error.URLError("timed out")
            return gone(path)

        written, row = self.track(partly, at=BASE + timedelta(minutes=16))
        self.assertEqual("missing", row["outcome_status"])
        self.assertTrue(written[0]["fetch_failed"])

    def test_gone_after_the_window_is_a_late_mark(self):
        self.seed(price=1.0)
        _written, row = self.track(gone, at=BASE + timedelta(minutes=21))
        self.assertEqual("missing_late", row["outcome_status"])
        self.assertEqual(0, row["primary_eligible"])
        self.assertIsNone(row["return_pct"])

    def run_main(self, answer, *, at: datetime) -> tuple[int, str]:
        out = io.StringIO()
        argv = ["signal_outcome_tracker.py", "--db", str(self.db)]
        with patch.object(sys, "argv", argv), patch("signal_outcome_tracker.now", return_value=at), \
             patch("signal_outcome_tracker.time.sleep"), \
             patch("dexscreener_client.get_json", side_effect=answer), redirect_stdout(out):
            try:
                signal_outcome_tracker.main()
                code = 0
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue()

    def test_main_exits_2_when_dexscreener_did_not_answer(self):
        self.seed(price=1.0)
        code, out = self.run_main(unreachable, at=BASE + timedelta(minutes=16))
        self.assertEqual(2, code, out)
        self.assertIn("1 due", out)
        self.assertIn("DexScreener did not answer for 1 read", out)

    def test_main_exits_0_on_a_delisting(self):
        self.seed(price=1.0)
        code, out = self.run_main(gone, at=BASE + timedelta(minutes=16))
        self.assertEqual(0, code, out)
        self.assertIn("status=delisted", out)


if __name__ == "__main__":
    unittest.main()
