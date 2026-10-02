#!/usr/bin/env python3
"""`chaos wallets --review` against a real smart_wallets.sqlite built from the shipped schema.

Each test gets its own temp CHAOS_HOME with a home roster, and runs chaos_cmd.py as a subprocess.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR / "fixtures") not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR / "fixtures"))
from damaged_sqlite import damage_table, junk_with_header  # noqa: E402

SCHEMA = SCRIPT_DIR.parent / "schemas" / "smart_wallets_schema.sql"
BOUNDARY = "Advisory + paper only. No wallet, signing, routing, or live execution."
NO_INGEST = "No ingest yet. Run chaos run chaos_alpha_elite_ingest first."

W_RECENT = "ReviewRecentwa11et11111111111111111111111111"
W_FRESH = "ReviewFreshwa11et111111111111111111111111111"
W_OLD = "ReviewAgedwa11et111111111111111111111111111"
W_NONE = "ReviewNonewa11et111111111111111111111111111"
MINT = "MintReview111111111111111111111111111111111"


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class WalletsReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        roster = self.home / "trading" / "config" / "roster.json"
        roster.parent.mkdir(parents=True)
        roster.write_text(json.dumps({"version": "test-v1", "wallets": [
            {"address": W_RECENT, "tier": "A"},
            {"address": W_FRESH, "tier": "B"},
            {"address": W_OLD, "tier": "B"},
            {"address": W_NONE, "tier": "C"},
        ]}), encoding="utf-8")
        self.db = self.home / "trading" / "db" / "smart_wallets.sqlite"
        self.env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME")}
        self.env.update(CHAOS_HOME=str(self.home), PYTHONIOENCODING="utf-8")
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.recent_at = self.now - timedelta(days=1)
        self.old_at = self.now - timedelta(days=30)

    def schema_only_db(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript(SCHEMA.read_text(encoding="utf-8"))
            con.execute("INSERT INTO sources(source_id,source_type) VALUES('solana_rpc','rpc')")
            con.execute("INSERT INTO ingestion_runs(run_id,source_id,status) VALUES('elite-failed','solana_rpc','failed')")
            con.commit()

    def build_db(self) -> None:
        self.schema_only_db()
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("INSERT INTO ingestion_runs(run_id,source_id,status) VALUES('elite-done','solana_rpc','completed')")
            con.execute("INSERT INTO tokens(mint) VALUES(?)", (MINT,))
            for wallet in (W_RECENT, W_FRESH, W_OLD, W_NONE):
                con.execute("INSERT INTO wallets(address) VALUES(?)", (wallet,))
            events = [
                (W_RECENT, iso(self.recent_at - timedelta(hours=5)), "buy"),
                (W_RECENT, iso(self.recent_at), "sell"),
                (W_FRESH, iso(self.recent_at), "buy"),
                (W_OLD, iso(self.old_at), "buy"),
            ]
            for wallet, at, kind in events:
                con.execute(
                    "INSERT INTO wallet_token_events(wallet,mint,block_time_utc,event_type,source_id) VALUES(?,?,?,?,?)",
                    (wallet, MINT, at, kind, "solana_rpc"),
                )
            # (wallet, status, transfer_contaminated, realized_pnl_sol); only closed, clean rows with a result count.
            positions = [
                (W_RECENT, "closed", 0, 0.4), (W_RECENT, "closed", 0, -0.1), (W_RECENT, "closed", 0, 0.0),
                (W_RECENT, "closed", 1, 0.2), (W_RECENT, "open", 0, None), (W_RECENT, "closed", 0, None),
                (W_FRESH, "closed", 0, 0.3), (W_FRESH, "closed", 0, 0.1), (W_FRESH, "closed", 1, 0.5),
                (W_OLD, "closed", 0, 0.2), (W_OLD, "closed", 0, 0.2), (W_OLD, "closed", 0, -0.2),
            ]
            for n, (wallet, status, contaminated, pnl) in enumerate(positions):
                con.execute(
                    "INSERT INTO positions(wallet,mint,opened_at_utc,status,transfer_contaminated,realized_pnl_sol) VALUES(?,?,?,?,?,?)",
                    (wallet, MINT, iso(self.old_at + timedelta(minutes=n)), status, contaminated, pnl),
                )
            scores = [
                (W_RECENT, 20.0, "study", iso(self.now - timedelta(days=3))),
                (W_RECENT, 70.4, "candidate", iso(self.now - timedelta(hours=2))),
            ]
            for wallet, score, copyability, at in scores:
                con.execute(
                    "INSERT INTO wallet_scores(wallet,score,copyability,source_id,scored_at) VALUES(?,?,?,?,?)",
                    (wallet, score, copyability, "solana_rpc", at),
                )
            con.commit()

    def run_review(self, *extra: str) -> subprocess.CompletedProcess:
        p = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "chaos_cmd.py"), "wallets", "--review", *extra],
            capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120, check=False,
        )
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return p

    def test_no_database_prints_the_sentence_and_creates_nothing(self):
        p = self.run_review()
        self.assertEqual(p.stdout.strip(), NO_INGEST)
        self.assertFalse(self.db.exists())

    def test_database_without_a_completed_run_prints_the_sentence(self):
        # `chaos wallets` creates the schema with no runs; a failed run is not an ingest either.
        self.schema_only_db()
        self.assertEqual(self.run_review().stdout.strip(), NO_INGEST)
        self.assertEqual(self.run_review("--raw").stdout.strip(), NO_INGEST)

    def run_wallets_failing(self, *args: str) -> str:
        """Run a `chaos wallets` read that must fail with one stderr line and exit 1; return that line."""
        p = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "chaos_cmd.py"), "wallets", *args],
            capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120, check=False,
        )
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1, p.stderr)
        return p.stderr.strip()

    def test_corrupt_database_exits_1_with_one_line_on_every_wallets_read(self):
        # A junk file fails on the header ("file is not a database"); a damaged first page fails on the
        # schema ("database disk image is malformed"). Both are corruption, so both say to move it aside.
        # Junk behind a valid header is the shape some SQLite builds open before failing.
        move_aside = "smart_wallets.sqlite is not a readable SQLite database. Move it aside and run the ingest again."
        self.db.parent.mkdir(parents=True, exist_ok=True)
        for damage in ("junk", "junk behind a header", "malformed"):
            self.db.unlink(missing_ok=True)
            if damage == "junk":
                self.db.write_bytes(bytes(range(256)) * 24)
            elif damage == "junk behind a header":
                junk_with_header(self.db)
            else:
                self.build_db()
                data = bytearray(self.db.read_bytes())
                data[100:4096] = b"\xff" * (4096 - 100)
                self.db.write_bytes(bytes(data))
            for args in (("--review",), ("--review", "--raw"), ("--discover", "1"), ()):
                with self.subTest(damage=damage, args=args):
                    self.assertTrue(self.run_wallets_failing(*args).endswith(move_aside))
        # An intact first page over a damaged events table opens on every platform; the review fails only
        # when it reads each wallet's newest event.
        self.db.unlink()
        self.build_db()
        damage_table(self.db, "wallet_token_events")
        for args in (("--review",), ("--review", "--raw")):
            with self.subTest(damage="damaged events table", args=args):
                self.assertTrue(self.run_wallets_failing(*args).endswith(move_aside))

    def test_locked_database_shows_the_error_and_says_to_try_again(self):
        # A lock is not damage: moving the file aside would throw away a good database.
        self.build_db()
        with closing(sqlite3.connect(self.db)) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            line = self.run_wallets_failing("--review")
            holder.rollback()
        self.assertTrue(line.startswith("Could not read "), line)
        self.assertIn("smart_wallets.sqlite: database is locked. Try again;", line)
        self.assertNotIn("Move it aside", line)

    def test_card_labels_each_roster_wallet(self):
        self.build_db()
        lines = self.run_review().stdout.strip().splitlines()
        self.assertEqual(lines[0], "☄️ ROSTER REVIEW")
        self.assertEqual(lines[-1], BOUNDARY)
        recent = self.recent_at.strftime("%Y-%m-%d %H:%M UTC")
        old = self.old_at.strftime("%Y-%m-%d %H:%M UTC")
        self.assertEqual(lines[1], f"A {W_RECENT} last {recent} clean-closed 3 score 70 candidate  scoring")
        self.assertEqual(lines[2], f"B {W_FRESH} last {recent} clean-closed 2 score -  no track record yet")
        self.assertEqual(lines[3], f"B {W_OLD} last {old} clean-closed 3 score -  dormant")
        self.assertEqual(lines[4], f"C {W_NONE} last none clean-closed 0 score -  dormant")
        self.assertEqual(lines[5], "2 of 4 roster wallets active in the last 14 days")

    def test_days_widens_the_window_and_unscored_scoring_names_the_next_step(self):
        self.build_db()
        lines = self.run_review("--days", "60").stdout.strip().splitlines()
        self.assertEqual(lines[3], f"B {W_OLD} last {self.old_at.strftime('%Y-%m-%d %H:%M UTC')} clean-closed 3 score -  "
                                   f"scoring (not scored yet: run chaos run smart_wallet_tracker {W_OLD})")
        self.assertTrue(lines[4].endswith("  dormant"), lines[4])
        self.assertEqual(lines[5], "3 of 4 roster wallets active in the last 60 days")

    def test_hand_edited_tiers_and_padded_addresses_still_list(self):
        # A hand-edited roster: a padded lower-case tier, an unknown tier, and a padded address.
        (self.home / "trading" / "config" / "roster.json").write_text(json.dumps({"version": "test-v1", "wallets": [
            {"address": W_RECENT, "tier": " b "},
            {"address": W_FRESH, "tier": "x"},
            {"address": f"  {W_OLD} ", "tier": "A"},
            {"address": W_NONE, "tier": "C"},
        ]}), encoding="utf-8")
        self.build_db()
        lines = self.run_review().stdout.strip().splitlines()
        recent = self.recent_at.strftime("%Y-%m-%d %H:%M UTC")
        old = self.old_at.strftime("%Y-%m-%d %H:%M UTC")
        self.assertEqual(lines[1], f"B {W_RECENT} last {recent} clean-closed 3 score 70 candidate  scoring")
        self.assertEqual(lines[2], f"? {W_FRESH} last {recent} clean-closed 2 score -  no track record yet")
        self.assertEqual(lines[3], f"A {W_OLD} last {old} clean-closed 3 score -  dormant")
        self.assertEqual(lines[4], f"C {W_NONE} last none clean-closed 0 score -  dormant")
        rows = json.loads(self.run_review("--raw").stdout)
        self.assertEqual([(r["tier"], r["address"]) for r in rows], [("B", W_RECENT), ("?", W_FRESH), ("A", W_OLD), ("C", W_NONE)])

    def test_raw_is_a_json_list_with_the_card_fields(self):
        self.build_db()
        rows = json.loads(self.run_review("--raw").stdout)
        self.assertIsInstance(rows, list)
        self.assertEqual([r["address"] for r in rows], [W_RECENT, W_FRESH, W_OLD, W_NONE])
        for r in rows:
            self.assertEqual(set(r), {"tier", "address", "last_event_utc", "clean_closed", "score", "copyability", "label"})
        self.assertEqual([r["label"] for r in rows], ["scoring", "no track record yet", "dormant", "dormant"])
        self.assertEqual(rows[0]["last_event_utc"], iso(self.recent_at))
        self.assertEqual((rows[0]["clean_closed"], rows[0]["score"], rows[0]["copyability"]), (3, 70.4, "candidate"))
        self.assertEqual((rows[3]["last_event_utc"], rows[3]["clean_closed"], rows[3]["score"], rows[3]["copyability"]), (None, 0, None, None))


if __name__ == "__main__":
    unittest.main()
