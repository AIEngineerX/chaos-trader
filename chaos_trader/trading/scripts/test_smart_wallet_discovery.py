#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import smart_wallet_tracker as swt
from smart_wallet_promoter import run as promoter_run

WALLET = "WalletA111111111111111111111111111111111111"


def make_db(path: Path | str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    swt.ensure_db(con)
    con.execute("INSERT OR IGNORE INTO ingestion_runs(run_id,source_id,started_at,status) VALUES(?,?,?,?)", ("run-t", "helius_rpc", "t0", "running"))
    return con


def insert_event(con: sqlite3.Connection, wallet: str, mint: str | None, event_type: str, t: str | None, *, token_delta: float = 0.0, sol_delta: float = 0.0, confidence: str = "medium") -> None:
    sig = f"sig-{event_type}-{t}"
    con.execute("INSERT OR IGNORE INTO wallets(address,updated_at) VALUES(?,CURRENT_TIMESTAMP)", (wallet,))
    if mint:
        con.execute("INSERT OR IGNORE INTO tokens(mint,updated_at) VALUES(?,CURRENT_TIMESTAMP)", (mint,))
    con.execute("INSERT OR IGNORE INTO transactions(signature,source_id) VALUES(?,?)", (sig, "helius_rpc"))
    con.execute(
        "INSERT INTO wallet_token_events(wallet,mint,signature,block_time_utc,event_type,side,token_delta,sol_delta,fee_sol,source_id,run_id,confidence,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wallet, mint, sig, t, event_type, event_type, token_delta, sol_delta, 0.0001, "helius_rpc", "run-t", confidence, "{}"),
    )


class EnsureDbRepeatSafetyTests(unittest.TestCase):
    def test_ensure_db_is_repeat_safe_once_fts_exists(self):
        # SQLite reserves wallet_notes_fts_content as a shadow name once the FTS
        # table exists; a second ensure_db pass must not trip over it.
        con = make_db()
        self.assertTrue(con.execute("SELECT 1 FROM sqlite_master WHERE name='wallet_notes_fts'").fetchone())
        swt.ensure_db(con)
        swt.ensure_db(con)
        con.close()


class FetchTransactionsPaginationTests(unittest.TestCase):
    def test_fetch_txs_uses_helius_pagination_token(self):
        calls = []

        def fake_rpc(method, params, **kwargs):
            calls.append((method, params))
            opts = params[1]
            if "paginationToken" not in opts:
                return {"data": [{"signature": f"sig-{i}"} for i in range(100)], "paginationToken": "page-2"}
            self.assertEqual(opts["paginationToken"], "page-2")
            self.assertNotIn("before", opts)
            return {"data": [{"signature": "sig-100"}], "paginationToken": None}

        with patch.object(swt, "is_helius_endpoint", return_value=True), \
             patch.object(swt, "rpc_request", side_effect=fake_rpc), patch.object(swt.time, "sleep", return_value=None):
            rows = swt.fetch_txs(WALLET, 100, 3)
        self.assertEqual(len(rows), 101)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "getTransactionsForAddress")


class RebuildPositionContaminationTests(unittest.TestCase):
    def test_sell_only_and_open_positions_do_not_create_realized_wallet_edge(self):
        con = make_db()
        insert_event(con, WALLET, "SELLONLY", "sell", "2026-08-01T01:00:00+00:00", token_delta=-100.0, sol_delta=10.0)
        insert_event(con, WALLET, "OPEN", "buy", "2026-08-01T02:00:00+00:00", token_delta=100.0, sol_delta=-1.0)
        positions = {p["mint"]: p for p in swt.rebuild_positions(con, WALLET)}
        self.assertEqual(positions["SELLONLY"]["status"], "unresolved")
        self.assertIsNone(positions["SELLONLY"]["realized_pnl_sol"])
        self.assertEqual(positions["OPEN"]["status"], "open")
        self.assertIsNone(positions["OPEN"]["realized_pnl_sol"])
        con.close()

    def test_matched_cost_basis_and_quote_assets(self):
        con = make_db()
        insert_event(con, WALLET, "M1", "buy", "2026-08-01T01:00:00+00:00", token_delta=100.0, sol_delta=-1.0)
        insert_event(con, WALLET, "M1", "sell", "2026-08-01T01:10:00+00:00", token_delta=-100.0, sol_delta=1.5)
        insert_event(con, WALLET, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "sell", "2026-08-01T01:20:00+00:00", token_delta=-100.0, sol_delta=50.0)
        positions = {p["mint"]: p for p in swt.rebuild_positions(con, WALLET)}
        self.assertEqual(positions["M1"]["status"], "closed")
        self.assertAlmostEqual(positions["M1"]["realized_pnl_sol"], 0.5)
        self.assertNotIn("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", positions)
        con.close()

    def test_sol_move_contaminates_only_overlapping_position_windows(self):
        con = make_db()
        # M1 trade window 01:00-01:10 with a pure SOL move inside it at 01:05.
        insert_event(con, WALLET, "M1", "buy", "2026-08-01T01:00:00+00:00", token_delta=100.0, sol_delta=-1.0)
        insert_event(con, WALLET, None, "sol_transfer_out", "2026-08-01T01:05:00+00:00", sol_delta=-5.0)
        insert_event(con, WALLET, "M1", "sell", "2026-08-01T01:10:00+00:00", token_delta=-100.0, sol_delta=1.5)
        # M2 trade window 03:00-03:10, no SOL move inside.
        insert_event(con, WALLET, "M2", "buy", "2026-08-01T03:00:00+00:00", token_delta=50.0, sol_delta=-0.5)
        insert_event(con, WALLET, "M2", "sell", "2026-08-01T03:10:00+00:00", token_delta=-50.0, sol_delta=0.8)
        positions = {p["mint"]: p for p in swt.rebuild_positions(con, WALLET)}
        self.assertEqual(positions["M1"]["contaminated"], 1)
        self.assertEqual(positions["M2"]["contaminated"], 0)
        # Wallet-level SOL movement stays recorded on every position.
        self.assertAlmostEqual(positions["M2"]["transfer_out_sol"], 5.0)
        con.close()

    def test_untimed_sol_move_falls_back_to_contaminating_all(self):
        con = make_db()
        insert_event(con, WALLET, "M1", "buy", "2026-08-01T01:00:00+00:00", token_delta=100.0, sol_delta=-1.0)
        insert_event(con, WALLET, "M1", "sell", "2026-08-01T01:10:00+00:00", token_delta=-100.0, sol_delta=1.5)
        insert_event(con, WALLET, None, "sol_transfer_in", None, sol_delta=3.0)
        positions = {p["mint"]: p for p in swt.rebuild_positions(con, WALLET)}
        self.assertEqual(positions["M1"]["contaminated"], 1)
        con.close()


class DiscoveredWalletTests(unittest.TestCase):
    def seed(self, con: sqlite3.Connection) -> None:
        scored = "Scored1111111111111111111111111111111111111"
        unscored = "Unscored11111111111111111111111111111111111"
        isolated = "Isolated11111111111111111111111111111111111"
        exchange = "Exchange11111111111111111111111111111111111"
        for address, category in ((scored, None), (unscored, None), (isolated, None), (exchange, "Exchange")):
            con.execute("INSERT INTO wallets(address,category,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)", (address, category))
        con.execute("INSERT INTO wallet_scores(wallet,score,source_id,scored_at) VALUES(?,?,?,?)", (scored, 55.0, "helius_rpc", "t"))
        for src, dst in ((scored, unscored), (unscored, exchange), (scored, exchange)):
            con.execute(
                "INSERT INTO wallet_edges(src_wallet,dst_wallet,edge_type,confidence,source_id,run_id,metadata_json) VALUES(?,?,?,?,?,?,?)",
                (src, dst, "sent_sol_to", "medium", "helius_rpc", "run-t", "{}"),
            )
        con.commit()
        self.unscored = unscored

    def test_returns_only_unscored_edge_connected_non_exchange_wallets(self):
        con = make_db()
        self.seed(con)
        self.assertEqual(swt.discovered_wallets(con, 10), [self.unscored])
        con.close()

    def test_promoter_payload_surfaces_discovery_queue(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            con = make_db(db)
            self.seed(con)
            con.close()
            payload = promoter_run(db, 10, False)
        self.assertIn(self.unscored, payload["discovery_queue"])
        self.assertEqual(payload["summary"]["discovery_queue"], 1)


class EnrichmentRollbackTests(unittest.TestCase):
    def test_failed_enrichment_rolls_back_before_next_wallet_commits(self):
        w_fail = "A" * 40
        w_ok = "B" * 40
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            con = make_db(db)
            for w, score in ((w_fail, 61.0), (w_ok, 50.0)):
                con.execute("INSERT OR IGNORE INTO wallets(address,updated_at) VALUES(?,CURRENT_TIMESTAMP)", (w,))
                con.execute("INSERT INTO wallet_scores(wallet,score,source_id,scored_at) VALUES(?,?,?,?)", (w, score, "helius_rpc", "t"))
            con.commit()
            con.close()
            argv = ["smart_wallet_tracker", "--top-db", "2", "--db", str(db), "--no-wallet-api"]
            with patch.object(sys, "argv", argv), \
                 patch.object(swt, "is_helius_endpoint", return_value=True), \
                 patch.object(swt, "fetch_txs", side_effect=[RuntimeError("helius down"), []]), \
                 self.assertRaises(SystemExit) as ctx:
                swt.main()
            # A partially failed batch must signal failure to cron/health.
            self.assertEqual(ctx.exception.code, 1)
            check = sqlite3.connect(db)
            try:
                # The failed wallet's pre-enrichment deletes must not be sealed
                # by the next successful wallet's commit.
                survivor = check.execute("SELECT COUNT(*) FROM wallet_scores WHERE wallet=? AND score=61.0", (w_fail,)).fetchone()[0]
                refreshed = check.execute("SELECT COUNT(*) FROM wallet_scores WHERE wallet=?", (w_ok,)).fetchone()[0]
            finally:
                check.close()
            self.assertEqual(survivor, 1)
            self.assertGreaterEqual(refreshed, 1)


class CmdWalletsRollbackTests(unittest.TestCase):
    def test_discover_path_rolls_back_failed_wallet_and_exits_nonzero(self):
        import argparse
        import chaos_cmd
        sentinel = "C" * 40
        w_fail = "D" * 40
        w_ok = "E" * 40
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "smart_wallets.sqlite"
            con = make_db(db)
            for w in (sentinel, w_fail, w_ok):
                con.execute("INSERT OR IGNORE INTO wallets(address,updated_at) VALUES(?,CURRENT_TIMESTAMP)", (w,))
            con.execute("INSERT INTO wallet_scores(wallet,score,source_id,scored_at) VALUES(?,?,?,?)", (sentinel, 77.0, "helius_rpc", "t"))
            for src, dst in ((w_fail, sentinel), (w_fail, w_ok), (w_ok, sentinel)):
                con.execute(
                    "INSERT INTO wallet_edges(src_wallet,dst_wallet,edge_type,confidence,source_id,run_id,metadata_json) VALUES(?,?,?,?,?,?,?)",
                    (src, dst, "sent_sol_to", "medium", "helius_rpc", "run-t", "{}"),
                )
            con.commit()
            con.close()

            def fake_enrich(con, wallet, limit, pages, include_api):
                if wallet == w_fail:
                    con.execute("DELETE FROM wallet_scores WHERE wallet=?", (sentinel,))
                    raise RuntimeError("helius down")
                con.execute("INSERT INTO wallet_scores(wallet,score,source_id,scored_at) VALUES(?,?,?,?)", (wallet, 10.0, "helius_rpc", "t"))
                con.commit()
                return {"wallet": wallet, "score": 10.0, "copyability": "study", "sample_realized_pnl_sol": 0.0}

            args = argparse.Namespace(discover=2, limit=10, db=str(db), raw=True, render_json=False)
            with patch.object(swt, "enrich_wallet", side_effect=fake_enrich), self.assertRaises(SystemExit) as ctx:
                chaos_cmd.cmd_wallets(args)
            self.assertEqual(ctx.exception.code, 1)
            check = sqlite3.connect(db)
            try:
                survivor = check.execute("SELECT COUNT(*) FROM wallet_scores WHERE wallet=? AND score=77.0", (sentinel,)).fetchone()[0]
            finally:
                check.close()
            self.assertEqual(survivor, 1)


class WalletApiErrorSurfacingTests(unittest.TestCase):
    def test_wallet_get_records_failure_instead_of_silent_none(self):
        errors: list[str] = []
        with patch.dict(os.environ, {"HELIUS_API_KEY": "test-key-not-real"}, clear=False), \
             patch.object(swt.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            out = swt.wallet_get("/v1/wallet/W/identity", errors=errors)
        self.assertIsNone(out)
        self.assertEqual(len(errors), 1)
        self.assertIn("/v1/wallet/W/identity", errors[0])
        self.assertIn("OSError", errors[0])


if __name__ == "__main__":
    unittest.main()
