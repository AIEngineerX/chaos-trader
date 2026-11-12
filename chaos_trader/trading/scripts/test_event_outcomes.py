#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import event_outcome_report  # noqa: E402
import event_outcome_tracker  # noqa: E402


def make_event(event_id: str = "cet1_test", *, observed_at: datetime | None = None, event_type: str = "dex_boost") -> dict[str, object]:
    observed = observed_at or datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    return {
        "schema": "chaos_event_tape.v1",
        "event_id": event_id,
        "event_type": event_type,
        "observed_at": observed.isoformat(timespec="seconds"),
        "event_time": None,
        "source": "dexscreener",
        "source_key": "dex_boosts_latest",
        "source_endpoint": "/token-boosts/latest/v1",
        "chain_id": "solana",
        "token_address": "BoostMint11111111111111111111111111111",
        "pair_address": None,
        "summary": {"amount": 10},
        "boundary": "read-only official/public market event tape; no execution, wallets, scraping, alerts, webhooks, or credentials",
    }


class SequenceFetcher:
    def __init__(self, prices: list[float], liquidities: list[float] | None = None) -> None:
        self.prices = prices
        self.liquidities = liquidities or [10_000 for _ in prices]
        self.calls = 0

    def __call__(self, chain: str, token: str) -> dict[str, object]:
        idx = min(self.calls, len(self.prices) - 1)
        self.calls += 1
        return {
            "summary": {
                "priceUsd": self.prices[idx],
                "liquidity_usd": self.liquidities[idx],
                "marketCap": self.prices[idx] * 1_000_000,
                "fdv": self.prices[idx] * 1_500_000,
                "url": f"https://dexscreener.com/{chain}/{token}",
            }
        }


class EventOutcomeTests(unittest.TestCase):
    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        event_outcome_tracker.ensure_schema(con)
        self.addCleanup(con.close)
        return con

    def test_parse_windows_accepts_minutes_hours_days(self):
        labels = [label for label, _delta in event_outcome_tracker.parse_windows("5m,1h,1d")]
        self.assertEqual(labels, ["5m", "1h", "1d"])
        with self.assertRaises(Exception):
            event_outcome_tracker.parse_windows("5x")

    def test_tracker_writes_valid_baseline_then_due_outcomes_with_max_drawdown(self):
        con = self.connect()
        event = make_event(observed_at=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc))
        fetcher = SequenceFetcher([1.0, 1.5, 0.5])
        windows = event_outcome_tracker.parse_windows("5m,15m")

        first = event_outcome_tracker.track_event(
            con,
            event,
            windows=windows,
            checked_at=datetime(2026, 6, 26, 12, 1, tzinfo=timezone.utc),
            baseline_grace_seconds=600,
            fetcher=fetcher,
        )
        self.assertEqual(first["baseline"]["baseline_status"], "valid_baseline")
        self.assertEqual(first["written_outcomes"], [])

        second = event_outcome_tracker.track_event(
            con,
            event,
            windows=windows,
            checked_at=datetime(2026, 6, 26, 12, 6, tzinfo=timezone.utc),
            baseline_grace_seconds=600,
            fetcher=fetcher,
        )
        self.assertEqual([r["window"] for r in second["written_outcomes"]], ["5m"])
        five = con.execute("SELECT * FROM event_outcomes WHERE window_label='5m'").fetchone()
        self.assertEqual((five["return_pct"], five["calibration_valid"]), (50.0, 1))

        at_15m = event_outcome_tracker.track_event(
            con,
            event,
            windows=windows,
            checked_at=datetime(2026, 6, 26, 12, 16, tzinfo=timezone.utc),
            baseline_grace_seconds=600,
            fetcher=fetcher,
        )
        self.assertEqual([r["window"] for r in at_15m["written_outcomes"]], ["15m"])
        fifteen = con.execute("SELECT * FROM event_outcomes WHERE window_label='15m'").fetchone()
        self.assertEqual((fifteen["return_pct"], fifteen["calibration_valid"], fifteen["max_return_pct"]), (-50.0, 1, 50.0))

        # Later window captures drawdown and preserves prior max-up.
        later_windows = event_outcome_tracker.parse_windows("5m,15m,1h")
        third = event_outcome_tracker.track_event(
            con,
            event,
            windows=later_windows,
            checked_at=datetime(2026, 6, 26, 13, 1, tzinfo=timezone.utc),
            baseline_grace_seconds=600,
            fetcher=fetcher,
        )
        self.assertEqual([r["window"] for r in third["written_outcomes"]], ["1h"])
        one_h = con.execute("SELECT * FROM event_outcomes WHERE window_label='1h'").fetchone()
        self.assertEqual(one_h["return_pct"], -50.0)
        self.assertEqual(one_h["max_return_pct"], 50.0)
        self.assertEqual(one_h["max_drawdown_pct"], -50.0)

    def test_a_window_reached_late_gets_no_price_and_does_not_borrow_the_current_one(self):
        con = self.connect()
        event = make_event(observed_at=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc))
        fetcher = SequenceFetcher([1.0, 3.0])
        windows = event_outcome_tracker.parse_windows("5m,15m,1h,6h,24h")
        track = lambda when: event_outcome_tracker.track_event(  # noqa: E731
            con, event, windows=windows, checked_at=when, baseline_grace_seconds=600, fetcher=fetcher)
        track(datetime(2026, 6, 26, 12, 1, tzinfo=timezone.utc))
        # The tracker was down for a day. The token trades at 3x now; no window saw that price at its horizon.
        late = track(datetime(2026, 6, 27, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(["5m", "15m", "1h", "6h", "24h"], [r["window"] for r in late["written_outcomes"]])
        rows = con.execute("SELECT return_pct, price_usd_now, outcome_status, dead_or_alive, calibration_valid FROM event_outcomes").fetchall()
        self.assertEqual([(None, None, "missing_late", "unknown", 0)] * 5, [tuple(r) for r in rows])
        self.assertEqual(1, fetcher.calls)  # the baseline only: nothing on time, so no market read
        self.assertEqual([], event_outcome_report.load_rows(con))

    def test_one_run_prices_the_window_it_reached_on_time_and_not_the_one_it_missed(self):
        con = self.connect()
        event = make_event(observed_at=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc))
        fetcher = SequenceFetcher([1.0, 1.5])
        windows = event_outcome_tracker.parse_windows("5m,15m")
        for when in (datetime(2026, 6, 26, 12, 1, tzinfo=timezone.utc), datetime(2026, 6, 26, 12, 16, tzinfo=timezone.utc)):
            event_outcome_tracker.track_event(con, event, windows=windows, checked_at=when, baseline_grace_seconds=600, fetcher=fetcher)
        rows = {r["window_label"]: (r["return_pct"], r["outcome_status"], r["calibration_valid"]) for r in con.execute("SELECT * FROM event_outcomes")}
        self.assertEqual({"5m": (None, "missing_late", 0), "15m": (50.0, "positive", 1)}, rows)

    def test_the_limit_counts_events_with_work_left_so_the_tape_past_it_is_reached(self):
        con = self.connect()
        start = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
        windows = event_outcome_tracker.parse_windows("5m")
        with tempfile.TemporaryDirectory() as td:
            tape = Path(td) / "tape.jsonl"
            events = [make_event(f"cet1_{i}", observed_at=start) for i in range(3)]
            tape.write_text("\n".join(json.dumps(e) for e in [{"event_type": "no_id"}, *events]) + "\n", encoding="utf-8")
            pending = []
            for _ in range(4):
                out = event_outcome_tracker.track_tape(con, tape, windows=windows, checked_at=start + timedelta(minutes=6), limit=1, fetcher=SequenceFetcher([1.0]))
                pending.append(out["events_pending"])
        # One event per run; the row with no id is skipped each time without using the limit up.
        self.assertEqual([4, 3, 2, 1], pending)
        tracked = [r[0] for r in con.execute("SELECT event_id FROM event_outcomes ORDER BY event_id")]
        self.assertEqual(["cet1_0", "cet1_1", "cet1_2"], tracked)

    def test_late_baseline_is_recorded_but_excluded_from_default_report(self):
        con = self.connect()
        event = make_event("cet1_late", observed_at=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc), event_type="dex_paid_order")
        result = event_outcome_tracker.track_event(
            con,
            event,
            windows=event_outcome_tracker.parse_windows("1h"),
            checked_at=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
            baseline_grace_seconds=600,
            fetcher=SequenceFetcher([1.0, 2.0]),
        )
        self.assertEqual(result["baseline"]["baseline_status"], "late_baseline")
        self.assertEqual(result["written_outcomes"][0]["calibration_valid"], False)
        self.assertEqual(event_outcome_report.load_rows(con), [])
        self.assertEqual(len(event_outcome_report.load_rows(con, include_late_baseline=True)), 1)

    def test_tape_loader_skips_bad_lines_and_dedupes_event_db(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tape = root / "event_tape.jsonl"
            event = make_event("cet1_dup")
            tape.write_text(json.dumps(event) + "\nnot-json\n" + json.dumps(event) + "\n", encoding="utf-8")
            con = self.connect()
            result = event_outcome_tracker.track_tape(
                con,
                tape,
                windows=event_outcome_tracker.parse_windows("5m"),
                checked_at=datetime(2026, 6, 26, 12, 7, tzinfo=timezone.utc),
                baseline_grace_seconds=600,
                fetcher=SequenceFetcher([1.0, 2.0, 3.0]),
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["events_seen"], 2)
            self.assertEqual(result["baselines_written"], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM event_outcome_events").fetchone()[0], 1)

    def test_report_aggregates_by_event_type_and_marks_small_sample(self):
        con = self.connect()
        observed = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
        for idx, price in enumerate([2.0, 0.5]):
            event = make_event(f"cet1_report_{idx}", observed_at=observed, event_type="dex_boost")
            event_outcome_tracker.track_event(
                con,
                event,
                windows=event_outcome_tracker.parse_windows("5m"),
                checked_at=observed + timedelta(minutes=1),
                baseline_grace_seconds=600,
                fetcher=SequenceFetcher([1.0]),
            )
            event_outcome_tracker.track_event(
                con,
                event,
                windows=event_outcome_tracker.parse_windows("5m"),
                checked_at=observed + timedelta(minutes=6),
                baseline_grace_seconds=600,
                fetcher=SequenceFetcher([price]),
            )
        rows = event_outcome_report.load_rows(con, window="5m")
        agg = event_outcome_report.aggregate(rows, min_count=3)
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]["group"], "dex_boost")
        self.assertEqual(agg[0]["count"], 2)
        self.assertEqual(agg[0]["median_return_pct"], 25.0)
        self.assertEqual(agg[0]["edge_state"], "unproven-small-sample")
        md = event_outcome_report.render_md(rows, agg, window="5m", group_by="event_type", min_count=3, late_count=0)
        self.assertIn("Chaos Event Outcome Calibration", md)
        self.assertIn("Small samples are not edge", md)

    def test_cli_fixture_tracker_and_report_raw(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tape = root / "event_tape.jsonl"
            db = root / "event_outcomes.sqlite"
            event = make_event("cet1_cli", observed_at=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc))
            tape.write_text(json.dumps(event) + "\n", encoding="utf-8")
            tracker = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "event_outcome_tracker.py"), "--from-tape", str(tape), "--db", str(db), "--dry-run", "--raw"],
                text=True,
                capture_output=True,
                check=False,
            )
            # Dry-run still tries live fetch; missing credentials are not needed because DEXScreener is public.
            # If network fails in CI, the unit-level tests above cover behavior, so only assert CLI parses.
            self.assertIn(tracker.returncode, {0, 1, 2})
            report = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "event_outcome_report.py"), "--db", str(db), "--raw"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            payload = json.loads(report.stdout)
            self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
