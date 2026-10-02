#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from signal_calibration_report import ensure_outcomes, load_rows  # noqa: E402
from signal_ledger import connect, normalize_signal, record_signal  # noqa: E402
from signal_outcome_tracker import due_signals, ensure_outcomes as ensure_tracker_outcomes, track_signal  # noqa: E402
from token_event_analyzer import sanitize_private_context  # noqa: E402


class SignalLedgerCalibrationTests(unittest.TestCase):
    def test_fresh_db_has_outcomes_table_and_empty_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "fresh.sqlite"
            con = connect(db)
            try:
                ensure_outcomes(con)
                self.assertEqual(load_rows(con), [])
                names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("signals", names)
                self.assertIn("outcomes", names)
            finally:
                con.close()

    def test_normalized_signal_keeps_entry_and_position_dimensions(self):
        row = normalize_signal({
            "generated_at": "2026-06-25T00:00:00+00:00",
            "mint": "So11111111111111111111111111111111111111112",
            "market": {"symbol": "SOL"},
            "classification": {"verdict": "watch", "flow": {}},
            "gate": {"gate": "avoid"},
            "entry_gate": {"action": "avoid-entry"},
            "position_context": {"owner_exposed": True, "position_action": "manage"},
            "social_catalyst": {"catalyst_type": "soft-shill"},
            "flow_conversion": {"conversion_status": "social-reflexivity", "fake_flow_severity": "none"},
            "owner_exposure": {"owner_wallet_file": "/private/path", "owner_wallet_hits": [{"wallet": "OwnerWallet111111111111111111111111111111111", "amount": 1}]},
            "wallet_timing": {
                "wallet_timing": [{"wallet": "OwnerWallet111111111111111111111111111111111", "private_owner_context": True}],
                "watch_wallet_hits": [{"wallet": "OwnerWallet111111111111111111111111111111111"}],
                "quality_wallet_hits": [{"wallet": "OwnerWallet111111111111111111111111111111111"}],
                "scout_wallet_hits": [{"wallet": "OwnerWallet111111111111111111111111111111111"}],
                "top_holders_sample": [{"owner": "OwnerWallet111111111111111111111111111111111", "amount": 1}],
                "top_signers_sample": [{"wallet": "OwnerWallet111111111111111111111111111111111", "count": 1}],
            },
        }, source_command="token")
        self.assertEqual(row["signal_kind"], "owner_position_read")
        self.assertEqual(row["verdict"], "manage")
        self.assertEqual(row["entry_action"], "avoid-entry")
        self.assertEqual(row["position_action"], "manage")
        raw = json.loads(row["raw_json"])
        self.assertEqual(raw["owner_exposure"]["owner_wallet_file"], "[redacted-owner-wallet-file]")
        self.assertNotIn("OwnerWallet111111111111111111111111111111111", row["raw_json"])

    def test_token_artifact_sanitizer_redacts_owner_wallets(self):
        raw_owner = "OwnerWallet111111111111111111111111111111111"
        clean = sanitize_private_context({
            "owner_exposure": {
                "owner_wallet_file": "/private/owner_wallets.json",
                "owner_wallet_hits": [{"wallet": raw_owner, "amount": 10}],
            },
            "wallet_timing": {
                "wallet_timing": [{"wallet": raw_owner, "role": "owner-private", "private_owner_context": True}],
                "watch_wallet_hits": [{"wallet": raw_owner}],
                "quality_wallet_hits": [{"wallet": raw_owner}],
                "scout_wallet_hits": [{"wallet": raw_owner}],
                "top_holders_sample": [{"owner": raw_owner, "amount": 1}],
                "top_signers_sample": [{"wallet": raw_owner, "count": 1}],
            },
        })
        dumped = json.dumps(clean)
        self.assertNotIn(raw_owner, dumped)
        self.assertIn("OwnerW", dumped)
        self.assertEqual(clean["owner_exposure"]["owner_wallet_file"], "[redacted-owner-wallet-file]")

    def test_outcome_due_signals_skip_completed_old_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "signals.sqlite"
            old = "2026-06-25T00:00:00+00:00"
            for i in range(10):
                record_signal({
                    "generated_at": f"2026-06-25T00:{i:02d}:00+00:00",
                    "mint": f"1111111111111111111111111111111{chr(65+i)}",
                    "market": {"symbol": f"OLD{i}", "price_usd": 1, "liquidity_usd": 1000},
                    "classification": {"verdict": "watch", "flow": {}},
                }, source_command="unit", db_path=db)
            record_signal({
                "generated_at": old,
                "mint": "22222222222222222222222222222222",
                "market": {"symbol": "DUE", "price_usd": 1, "liquidity_usd": 1000},
                "classification": {"verdict": "watch", "flow": {}},
            }, source_command="unit", db_path=db)
            con = connect(db)
            try:
                ensure_tracker_outcomes(con)
                first_ids = [r[0] for r in con.execute("SELECT id FROM signals WHERE symbol LIKE 'OLD%' ORDER BY id").fetchall()]
                for first_id in first_ids:
                    for window in ("15m", "1h", "4h", "24h"):
                        con.execute(
                            "INSERT INTO outcomes(signal_id,window_label,checked_at_utc,target_time_utc,target_lag_seconds,age_seconds,dead_or_alive,outcome_status,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
                            (first_id, window, old, old, 0, 0, "alive", "unit", "{}"),
                        )
                con.commit()
                due = due_signals(con, limit=1, include_candidates=True)
                self.assertEqual(len(due), 1)
                self.assertEqual(due[0]["symbol"], "DUE")
            finally:
                con.close()

    def test_exact_time_outcome_is_primary_eligible(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "signals.sqlite"
            base = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
            record_signal({
                "generated_at": base.isoformat(),
                "mint": "33333333333333333333333333333333",
                "market": {"symbol": "DUE", "price_usd": 1.0, "liquidity_usd": 10_000, "market_cap": 50_000},
                "classification": {"verdict": "watch", "flow": {}},
            }, source_command="unit", db_path=db)
            con = connect(db)
            try:
                ensure_tracker_outcomes(con)
                signal = dict(con.execute("SELECT * FROM signals").fetchone())
                with patch("signal_outcome_tracker.now", return_value=base + timedelta(minutes=15)), \
                     patch("signal_outcome_tracker.fetch_token", return_value={"summary": {"priceUsd": 1.25, "liquidity_usd": 12_000, "marketCap": 62_500, "url": "fixture"}}):
                    written = track_signal(con, signal)
                self.assertEqual(["15m"], [r["window"] for r in written])
                row = con.execute("SELECT primary_eligible,late_snapshot,outcome_status,return_pct FROM outcomes").fetchone()
                self.assertEqual(row[0], 1)
                self.assertEqual(row[1], 0)
                self.assertEqual(row[2], "observed")
                self.assertEqual(row[3], 25.0)
            finally:
                con.close()

    def test_due_signals_prioritize_timely_horizon_over_legacy_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "signals.sqlite"
            now_at = datetime(2026, 7, 25, 12, 15, tzinfo=timezone.utc)
            record_signal({
                "generated_at": (now_at - timedelta(days=2)).isoformat(),
                "mint": "55555555555555555555555555555555",
                "market": {"symbol": "OLD", "price_usd": 1, "liquidity_usd": 10_000},
                "classification": {"verdict": "watch", "flow": {}},
            }, source_command="unit", db_path=db)
            record_signal({
                "generated_at": (now_at - timedelta(minutes=15)).isoformat(),
                "mint": "66666666666666666666666666666666",
                "market": {"symbol": "TIMELY", "price_usd": 1, "liquidity_usd": 10_000},
                "classification": {"verdict": "watch", "flow": {}},
            }, source_command="unit", db_path=db)
            con = connect(db)
            try:
                ensure_tracker_outcomes(con)
                with patch("signal_outcome_tracker.now", return_value=now_at):
                    due = due_signals(con, limit=1, include_candidates=True)
                self.assertEqual(["TIMELY"], [r["symbol"] for r in due])
            finally:
                con.close()

    def test_mixed_late_and_timely_windows_do_not_reuse_current_mark(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "signals.sqlite"
            base = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
            record_signal({
                "generated_at": base.isoformat(),
                "mint": "77777777777777777777777777777777",
                "market": {"symbol": "MIXED", "price_usd": 1.0, "liquidity_usd": 10_000},
                "classification": {"verdict": "watch", "flow": {}},
            }, source_command="unit", db_path=db)
            con = connect(db)
            try:
                ensure_tracker_outcomes(con)
                signal = dict(con.execute("SELECT * FROM signals").fetchone())
                with patch("signal_outcome_tracker.now", return_value=base + timedelta(hours=1)), \
                     patch("signal_outcome_tracker.fetch_token", return_value={"summary": {"priceUsd": 1.5, "liquidity_usd": 12_000}}):
                    track_signal(con, signal)
                rows = {r["window_label"]: r for r in con.execute("SELECT * FROM outcomes")}
                self.assertEqual("missing_late", rows["15m"]["outcome_status"])
                self.assertIsNone(rows["15m"]["return_pct"])
                self.assertEqual("causality_guard_missing_late", rows["15m"]["observation_source"])
                self.assertEqual("observed", rows["1h"]["outcome_status"])
                self.assertEqual(50.0, rows["1h"]["return_pct"])
                self.assertEqual(1, rows["1h"]["primary_eligible"])
            finally:
                con.close()

    def test_overdue_multiple_windows_do_not_reuse_one_current_mark_as_primary(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "signals.sqlite"
            base = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
            record_signal({
                "generated_at": base.isoformat(),
                "mint": "44444444444444444444444444444444",
                "market": {"symbol": "OLD", "price_usd": 1.0, "liquidity_usd": 10_000, "market_cap": 50_000},
                "classification": {"verdict": "watch", "flow": {}},
            }, source_command="unit", db_path=db)
            con = connect(db)
            try:
                ensure_tracker_outcomes(con)
                signal = dict(con.execute("SELECT * FROM signals").fetchone())
                with patch("signal_outcome_tracker.now", return_value=base + timedelta(days=2)), \
                     patch("signal_outcome_tracker.fetch_token") as fetch:
                    written = track_signal(con, signal)
                self.assertEqual(["15m", "1h", "4h", "24h"], [r["window"] for r in written])
                fetch.assert_not_called()
                rows = con.execute("SELECT primary_eligible,late_snapshot,outcome_status,price_usd_now,return_pct FROM outcomes ORDER BY target_time_utc").fetchall()
                self.assertTrue(rows)
                self.assertTrue(all(r[0] == 0 for r in rows))
                self.assertTrue(all(r[1] == 1 for r in rows))
                self.assertTrue(all(r[2] == "missing_late" for r in rows))
                self.assertTrue(all(r[3] is None and r[4] is None for r in rows))
                self.assertEqual(load_rows(con), [])
                self.assertEqual(len(load_rows(con, primary_only=False)), len(rows))
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
