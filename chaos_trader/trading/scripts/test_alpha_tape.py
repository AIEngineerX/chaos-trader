#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alpha_tape
import dexscreener_client

MINT = "5hiLgyybrAYPpUwNFa38agfZ8iEtnahWKAPixcfspump"


class AlphaTapeTests(unittest.TestCase):
    def make_db(self, path: Path) -> None:
        now = datetime.now(timezone.utc)
        first_buy = (now - timedelta(minutes=4)).isoformat()
        created = (now - timedelta(minutes=3)).isoformat()
        captured = (now - timedelta(minutes=2)).isoformat()
        concentration_at = (now - timedelta(minutes=2)).isoformat()
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE token_signals (
                mint TEXT, signal_type TEXT, wallet_count INTEGER, tg_channel_count INTEGER,
                total_sol_amount REAL, call_market_cap_usd REAL, current_market_cap_usd REAL,
                ath_market_cap_usd REAL, ath_multiplier REAL, is_hit INTEGER,
                first_buy_utc TEXT, created_at_utc TEXT, captured_at_utc TEXT
            );
            CREATE TABLE wallet_token_events (
                wallet TEXT, mint TEXT, signature TEXT, block_time_utc TEXT, event_type TEXT,
                side TEXT, amount_usd REAL, amount_sol REAL, market_cap_usd REAL,
                sol_delta REAL, source_id TEXT, confidence TEXT, run_id TEXT
            );
            CREATE TABLE wallet_scores (
                wallet TEXT, score REAL, secondary_score REAL, actor_score REAL, classification TEXT,
                tier TEXT, copyability TEXT, pnl_all REAL
            );
            CREATE TABLE token_concentration_snapshots (
                mint TEXT, supply_pct REAL, holder_count INTEGER, snapshot_at_utc TEXT
            );
            CREATE TABLE tokens (
                mint TEXT, symbol TEXT, name TEXT, latest_market_cap_usd REAL
            );
        """)
        con.execute("INSERT INTO token_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            MINT, "multi_buy", 4, 5, 15.0, 75000.0, 120000.0, 400000.0, 5.33, 1,
            first_buy, created, captured
        ))
        for idx in range(4):
            wallet = f"Wallet{idx}111111111111111111111111111111111111"
            event_time = (now - timedelta(minutes=4, seconds=idx)).isoformat()
            con.execute("INSERT INTO wallet_token_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                wallet, MINT, f"sig{idx}", event_time, "buy", "buy", 250.0, 1.2, 70000.0 + idx,
                -1.2, "helius_rpc", "medium", "elite-test-1"
            ))
            # Discovery-enriched (non-elite) buys on another mint must never
            # surface as elite candidates.
            con.execute("INSERT INTO wallet_token_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                wallet, "DiscoveryMint111111111111111111111111111111", f"dsig{idx}", event_time, "buy", "buy", 250.0, 1.2, 70000.0,
                -1.2, "helius_rpc", "medium", "helius-test-1"
            ))
            con.execute("INSERT INTO wallet_scores VALUES (?,?,?,?,?,?,?,?)", (
                wallet, 80.0 - idx, None, None, "copyable-candidate", None, None, 100000.0
            ))
        con.execute("INSERT INTO token_concentration_snapshots VALUES (?,?,?,?)", (MINT, 7.5, 312, concentration_at))
        con.execute("INSERT INTO tokens VALUES (?,?,?,?)", (MINT, "RICH", "Rich", 120000.0))
        con.commit()
        con.close()

    def test_token_payload_uses_local_tape_without_dex(self):
        with tempfile.TemporaryDirectory() as td:
            old = (alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR)
            try:
                root = Path(td)
                db = root / "smart_wallets.sqlite"
                self.make_db(db)
                alpha_tape.DB_PATH = db
                alpha_tape.SIGNAL_LEDGER_PATH = root / "missing-ledger.sqlite"
                alpha_tape.SECONDARY_SUMMARY_PATH = root / "missing-summary.json"
                alpha_tape.SECONDARY_SCORE_DIR = root / "missing-scores"
                payload = alpha_tape.token_payload(MINT, dex=False)
            finally:
                alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR = old
        self.assertEqual(payload["mode"], "alpha_tape_token")
        self.assertEqual(payload["gate"]["verdict"], "deep-check")
        self.assertEqual(payload["wallets"]["buy_wallets"], 4)
        msg = alpha_tape.render_token(payload)
        self.assertIn("ALPHA TAPE", msg)
        self.assertIn("$RICH", msg)
        self.assertIn("tracked multi-buy cluster", msg)
        self.assertTrue(msg.rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))

    def test_sweep_payload_is_local_and_compact(self):
        with tempfile.TemporaryDirectory() as td:
            old = (alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR)
            try:
                root = Path(td)
                db = root / "smart_wallets.sqlite"
                self.make_db(db)
                alpha_tape.DB_PATH = db
                alpha_tape.SIGNAL_LEDGER_PATH = root / "missing-ledger.sqlite"
                alpha_tape.SECONDARY_SUMMARY_PATH = root / "missing-summary.json"
                alpha_tape.SECONDARY_SCORE_DIR = root / "missing-scores"
                payload = alpha_tape.sweep_payload(limit=3)
            finally:
                alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR = old
        self.assertEqual(payload["mode"], "alpha_tape_sweep")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertGreater(payload["candidates"][0]["candidate_score"], 0)
        msg = alpha_tape.render_sweep(payload)
        self.assertIn("ALPHA TAPE SWEEP", msg)
        self.assertIn(MINT, msg)
        self.assertTrue(msg.rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))

    def test_sweep_dex_enrichment_fills_candidate_contract(self):
        with tempfile.TemporaryDirectory() as td:
            old = (alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR)
            try:
                root = Path(td)
                db = root / "smart_wallets.sqlite"
                self.make_db(db)
                alpha_tape.DB_PATH = db
                alpha_tape.SIGNAL_LEDGER_PATH = root / "missing-ledger.sqlite"
                alpha_tape.SECONDARY_SUMMARY_PATH = root / "missing-summary.json"
                alpha_tape.SECONDARY_SCORE_DIR = root / "missing-scores"
                summary = {"liquidity_usd": 42_000.0, "priceUsd": 0.0015, "marketCap": 130_000.0}
                with patch.object(alpha_tape, "dex_context", return_value=(summary, None)):
                    payload = alpha_tape.sweep_payload(limit=3, dex=True)
            finally:
                alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR = old
        candidate = payload["candidates"][0]
        self.assertAlmostEqual(candidate["market"]["liquidity_usd"], 42_000.0)
        self.assertAlmostEqual(candidate["market"]["price_usd"], 0.0015)
        self.assertGreater(candidate["candidate_score"], 0)

    def test_elite_signal_caps_hyperactive_wallet_before_aggregation(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            self.make_db(db)
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            now = datetime.now(timezone.utc)
            wallet = "HyperWallet11111111111111111111111111111111"
            for idx in range(5):
                event_time = (now - timedelta(minutes=idx + 1)).isoformat()
                con.execute("INSERT INTO wallet_token_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    wallet, f"Mint{idx}11111111111111111111111111111111111", f"hyper{idx}", event_time,
                    "buy", "buy", 100.0, 1.0, 50000.0, -1.0, "helius_rpc", "medium", "elite-hyper",
                ))
            con.commit()
            rows = alpha_tape.elite_buy_signals(con, window_minutes=45, limit=20, max_mints_per_wallet=2)
            con.close()
        hyper_rows = [row for row in rows if str(row["mint"]).startswith("Mint")]
        self.assertEqual(2, len(hyper_rows))
        self.assertEqual(["Mint011111111111111111111111111111111111", "Mint111111111111111111111111111111111111"], [row["mint"] for row in hyper_rows])

    def test_elite_buys_feed_sweep_even_when_token_signals_dead(self):
        with tempfile.TemporaryDirectory() as td:
            old = (alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR)
            try:
                root = Path(td)
                db = root / "smart_wallets.sqlite"
                self.make_db(db)
                # Reproduce the live failure mode: imported signal feed dead for months,
                # and wallet events 20 minutes old — past the 15m tape threshold but
                # inside one 30m ingest cadence.
                events_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
                con = sqlite3.connect(db)
                con.execute("UPDATE token_signals SET created_at_utc='2026-05-29T20:31:42+00:00', first_buy_utc='2026-05-29T20:00:00+00:00', captured_at_utc='2026-05-29T20:31:42+00:00'")
                con.execute("UPDATE wallet_token_events SET block_time_utc=?", (events_at,))
                con.commit()
                con.close()
                alpha_tape.DB_PATH = db
                alpha_tape.SIGNAL_LEDGER_PATH = root / "missing-ledger.sqlite"
                alpha_tape.SECONDARY_SUMMARY_PATH = root / "missing-summary.json"
                alpha_tape.SECONDARY_SCORE_DIR = root / "missing-scores"
                payload = alpha_tape.sweep_payload(limit=3)
            finally:
                alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH, alpha_tape.SECONDARY_SUMMARY_PATH, alpha_tape.SECONDARY_SCORE_DIR = old
        self.assertEqual(payload["candidate_count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["mint"], MINT)  # the helius-run discovery mint is excluded
        self.assertEqual(candidate["signal"]["signal_type"], "elite-wallet-buys")
        self.assertEqual(candidate["signal"]["wallet_count"], 4)
        # The dead imported feed must not mark the live elite candidate stale.
        self.assertEqual(candidate["freshness"]["status"], "fresh")
        self.assertNotEqual(candidate["gate"]["verdict"], "stale-tape")
        # The global tape freshness still reports the dead feed honestly.
        self.assertEqual(payload["freshness"]["status"], "stale")

    def test_connect_ro_reads_wal_database_and_refuses_writes(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "wal.sqlite"
            con = sqlite3.connect(db)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE t(x INTEGER)")
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
            con.close()
            ro = alpha_tape.connect_ro(db)
            self.assertIsNotNone(ro)
            self.assertEqual(1, ro.execute("SELECT x FROM t").fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO t VALUES (2)")
            ro.close()

    def test_missing_db_returns_explicit_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            old = (alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH)
            try:
                alpha_tape.DB_PATH = Path(td) / "missing.sqlite"
                alpha_tape.SIGNAL_LEDGER_PATH = Path(td) / "missing-ledger.sqlite"
                payload = alpha_tape.token_payload(MINT, dex=False)
                sweep = alpha_tape.sweep_payload(limit=-10)
            finally:
                alpha_tape.DB_PATH, alpha_tape.SIGNAL_LEDGER_PATH = old
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["gate"]["verdict"], "tape-unavailable")
        self.assertIn("missing", " ".join(payload["errors"]))
        self.assertFalse(sweep["ok"])
        self.assertEqual(sweep["limit"], 1)
        self.assertIn("UNAVAILABLE", alpha_tape.render_token(payload))
        self.assertIn("UNAVAILABLE", alpha_tape.render_sweep(sweep))

    def test_corrupt_db_returns_explicit_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            old = alpha_tape.DB_PATH
            try:
                db = Path(td) / "smart_wallets.sqlite"
                db.write_text("not sqlite")
                alpha_tape.DB_PATH = db
                payload = alpha_tape.token_payload(MINT, dex=False)
            finally:
                alpha_tape.DB_PATH = old
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["gate"]["verdict"], "tape-unavailable")

    def test_dex_client_reads_fresh_cache_before_network(self):
        with tempfile.TemporaryDirectory() as td:
            old_cache = dexscreener_client.CACHE_DIR
            old_get = dexscreener_client.get_json
            try:
                dexscreener_client.CACHE_DIR = Path(td)
                dexscreener_client.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path = dexscreener_client.cache_path("solana", MINT)
                path.write_text('{"ok": true, "summary": {"symbol": "CACHE"}}')
                dexscreener_client.get_json = lambda _path, timeout=20: (_ for _ in ()).throw(AssertionError("network should not be called"))
                out = dexscreener_client.fetch_token("solana", MINT, cache=True, ttl_seconds=3600)
            finally:
                dexscreener_client.CACHE_DIR = old_cache
                dexscreener_client.get_json = old_get
        self.assertTrue(out["cache"]["hit"])
        self.assertEqual(out["summary"]["symbol"], "CACHE")
    def test_dex_cache_key_is_sanitized(self):
        path = dexscreener_client.cache_path("sol/../../ana", "mint/../bad?x=1")
        self.assertEqual(path.parent, dexscreener_client.CACHE_DIR)
        self.assertNotIn("..", path.name)
        self.assertNotIn("/", path.name)
        self.assertTrue(path.name.endswith(".json"))


if __name__ == "__main__":
    unittest.main()
