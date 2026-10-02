#!/usr/bin/env python3
"""`chaos outcomes` aggregation and card on a real SQLite ledger built from the shipped schema."""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from outcomes_report import BOUNDARY, ENTRY_LABELS, aggregate, render_card  # noqa: E402
from signal_outcome_tracker import ensure_outcomes  # noqa: E402

SCHEMA = SCRIPT_DIR.parent / "schemas" / "signal_ledger.sql"
DAY = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class Ledger:
    """signals + outcomes rows written the way the token read and the tracker write them."""

    def __init__(self, path: Path):
        self.path = path
        self.n = 0
        with closing(sqlite3.connect(path)) as con:
            con.executescript(SCHEMA.read_text(encoding="utf-8"))
            ensure_outcomes(con)

    def read(self, mint: str, verdict: str, *, at: datetime = DAY, window: str = "24h", status: str = "observed",
             ret: float | None = 10.0, kind: str = "entry_read") -> None:
        self.n += 1
        late = status == "missing_late"
        eligible = status in {"observed", "non_exitable", "delisted"}
        checked = at + timedelta(hours=24, minutes=10 if late else 1)
        with closing(sqlite3.connect(self.path)) as con:
            sid = con.execute(
                "INSERT INTO signals(signal_id,timestamp_utc,source_command,mint,verdict,signal_kind,raw_json,created_at_utc) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (f"sig-{self.n}", at.isoformat(), "token", mint, verdict, kind, "{}", at.isoformat()),
            ).lastrowid
            con.execute(
                "INSERT INTO outcomes(signal_id,window_label,checked_at_utc,target_time_utc,target_lag_seconds,late_snapshot,"
                "primary_eligible,age_seconds,return_pct,dead_or_alive,outcome_status,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, window, checked.isoformat(), (at + timedelta(hours=24)).isoformat(), 600 if late else 60,
                 int(late), int(eligible), 86400, None if late else ret,
                 "dead" if status in {"delisted", "non_exitable", "missing_late"} else "alive", status, "{}"),
            )
            con.commit()

    def many(self, verdict: str, count: int, *, prefix: str = "M", **kw) -> None:
        for i in range(count):
            self.read(f"{prefix}{verdict}{i:03d}".ljust(32, "1"), verdict, **kw)


class OutcomesReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "signal_ledger.sqlite"
        self.ledger = Ledger(self.db)

    def agg(self, window: str = "24h", **kw) -> dict:
        return aggregate(self.db, window=window, now=NOW, **kw)

    def test_empty_card_has_no_rates(self):
        for db in (self.db, self.db.parent / "never-created.sqlite"):
            with self.subTest(db=db.name):
                result = aggregate(db, window="24h", now=NOW)
                card = render_card(result, None)
                self.assertNotIn("%", card)
                self.assertIn("NO SCORES YET", card)
                self.assertIn("of 20", card)
                self.assertIn("Outcome tick has not run", card)
                self.assertEqual(0, result["scored_reads"])
                for label in ENTRY_LABELS:
                    self.assertEqual("below_n", result["labels"][label]["status"])
                    self.assertRegex(card, rf"(?m)^{re.escape(label.upper())} · 0 of 20 reads, not scored yet$")
        self.assertFalse((self.db.parent / "never-created.sqlite").exists())

    def test_below_n_refuses(self):
        self.ledger.many("watch", 19)
        result = self.agg()
        watch = result["labels"]["watch"]
        self.assertEqual(("below_n", 19), (watch["status"], watch["n"]))
        for key in ("median", "up_pct", "runner_pct", "severe_pct", "caught_pct"):
            self.assertIsNone(watch[key], key)
        card = render_card(result, None)
        self.assertIn("WATCH · 19 of 20 reads, not scored yet", card.splitlines())
        self.assertNotIn("%", card)
        self.assertIn("NO SCORES YET", card)

    def test_twenty_reads_are_scored(self):
        self.ledger.many("watch", 20, ret=150.0)
        result = self.agg()
        watch = result["labels"]["watch"]
        self.assertEqual(("scored", 20), (watch["status"], watch["n"]))
        self.assertEqual((150.0, 100.0, 100.0, 0.0), (watch["median"], watch["up_pct"], watch["runner_pct"], watch["severe_pct"]))
        self.assertIsNone(watch["caught_pct"])
        card = render_card(result, None)
        self.assertNotIn("NO SCORES YET", card)
        self.assertIn("WATCH · n 20", card)
        self.assertIn("  median +150.0% · up 100% · 2x+ 100% · -70% or worse 0%", card)

    def test_dedupe_per_mint_label_and_utc_day(self):
        mint = "D" * 32
        for minute in range(5):
            self.ledger.read(mint, "watch", at=DAY + timedelta(minutes=minute))
        self.assertEqual(1, self.agg()["labels"]["watch"]["n"])
        self.ledger.read(mint, "watch", at=DAY + timedelta(days=1))
        result = self.agg()
        self.assertEqual(2, result["labels"]["watch"]["n"])
        self.assertEqual((2, 1), (result["scored_reads"], result["scored_mints"]))
        self.assertEqual("2026-09-20", result["since"])
        # Another label on the same mint and day is its own read.
        self.ledger.read(mint, "study", at=DAY)
        self.assertEqual(1, self.agg()["labels"]["study"]["n"])
        # A read after `now` is not on the card yet.
        early = aggregate(self.db, window="24h", now=DAY + timedelta(hours=30))
        self.assertEqual(1, early["labels"]["watch"]["n"])

    def test_owner_rows_excluded(self):
        self.ledger.many("watch", 25, prefix="O", kind="owner_position_read")
        self.ledger.many("manage", 25, prefix="O", kind="owner_position_read")
        result = self.agg()
        self.assertEqual(0, result["labels"]["watch"]["n"])
        self.assertNotIn("manage", result["labels"])
        self.assertEqual(0, result["scored_reads"])
        self.assertEqual(50, result["not_counted"]["owner"])
        card = render_card(result, None)
        self.assertNotIn("MANAGE", card)
        self.assertNotIn("owner", card.lower())
        self.assertNotIn("%", card)

    def test_unread_sweep_candidates_excluded(self):
        self.ledger.many("sweep-candidate-unread", 25)
        result = self.agg()
        self.assertNotIn("sweep-candidate-unread", result["labels"])
        self.assertEqual(25, result["not_counted"]["unread"])
        self.assertEqual(0, result["scored_reads"])

    def test_late_marks_not_counted(self):
        self.ledger.many("watch", 20)
        self.ledger.many("watch", 5, prefix="L", status="missing_late")
        result = self.agg()
        self.assertEqual(("scored", 20), (result["labels"]["watch"]["status"], result["labels"]["watch"]["n"]))
        self.assertEqual(5, result["not_counted"]["late"])
        self.assertIn("Not counted: 5 late marks", render_card(result, None).splitlines())

    def test_delisted_counts_as_loss(self):
        self.ledger.many("avoid-entry", 10, status="delisted", ret=-100.0)
        self.ledger.many("avoid-entry", 10, prefix="K", ret=10.0)
        self.ledger.many("watch", 15, ret=10.0)
        self.ledger.many("watch", 5, prefix="G", status="delisted", ret=-100.0)
        result = self.agg()
        avoid, watch = result["labels"]["avoid-entry"], result["labels"]["watch"]
        self.assertEqual((20, 50.0, 50.0), (avoid["n"], avoid["severe_pct"], avoid["caught_pct"]))
        self.assertEqual((20, 25.0), (watch["n"], watch["severe_pct"]))
        card = render_card(result, None)
        self.assertIn("caught 50%", card)

    def test_non_exitable_is_caught_for_avoid_labels(self):
        self.ledger.many("avoid-entry", 5, status="non_exitable", ret=-20.0)
        self.ledger.many("avoid-entry", 15, prefix="K", ret=-20.0)
        avoid = self.agg()["labels"]["avoid-entry"]
        self.assertEqual((0.0, 25.0), (avoid["severe_pct"], avoid["caught_pct"]))

    def test_window_filters_rows(self):
        self.ledger.many("watch", 20, window="1h")
        self.assertEqual(0, self.agg()["labels"]["watch"]["n"])
        self.assertEqual(20, self.agg(window="1h")["labels"]["watch"]["n"])

    def test_card_fits_telegram(self):
        for label in ENTRY_LABELS:
            self.ledger.many(label, 20, ret=-12.5)
        result = self.agg()
        self.assertTrue(all(v["status"] == "scored" for v in result["labels"].values()))
        card = render_card(result, NOW - timedelta(minutes=4))
        self.assertLess(utf16_units(card), 4096)
        self.assertTrue(card.endswith(BOUNDARY))
        self.assertNotIn("edge", card.lower())
        self.assertIn("Small samples prove nothing.", card.splitlines())
        self.assertIn("Outcome tick last ran:", card)

    def test_last_run_age_is_shown(self):
        card = render_card(self.agg(), datetime.now(timezone.utc) - timedelta(minutes=4))
        self.assertIn("Outcome tick last ran: 4 min ago", card.splitlines())
        self.assertNotIn("Outcome tick has not run", card)


if __name__ == "__main__":
    unittest.main()
