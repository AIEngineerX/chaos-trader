#!/usr/bin/env python3
"""The sweep fills token_signals and token_concentration_snapshots, so the fast lanes stop reading a stale tape.

Real SQLite from the shipped schema, a real sweep run, and real holder-cache files. Only the DexScreener reads
(discovery and the per-mint market fetch) are replaced: they are the network boundary.
"""
from __future__ import annotations

import functools
import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import alpha_tape  # noqa: E402
import fast_lane_writers  # noqa: E402
import holder_resolver  # noqa: E402
import signal_ledger  # noqa: E402
import trending_token_sweep as sweep  # noqa: E402
from elite_wallet_pipeline import connect_db  # noqa: E402
from smart_wallet_tracker import insert_event, upsert_wallet  # noqa: E402

MINT_A = "5hiLgyybrAYPpUwNFa38agfZ8iEtnahWKAPixcfspump"
MINT_B = "AXLmMWkRmSPdPxkuMqAD4nzYBK7QRssNkYZ6RXzLpump"
MINT_C = "7z8xH9Uv3sUpTaBR1WUkjggKGAMWFQpScKE4mEAwpump"
WSOL = "So11111111111111111111111111111111111111112"
WALLETS = ["Wallet1111111111111111111111111111111111111", "Wallet2222222222222222222222222222222222222"]


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def sample(mint: str, discretionary_pct: float) -> dict:
    return {
        "ok": True, "mode": "holder_resolver", "mint": mint, "limit": 20, "supply": 1_000_000_000.0,
        "raw_top_pct": 41.0, "lp_pool_pct": 22.0, "program_or_burn_pct": 4.0, "custody_pct": 0.0,
        "adjusted_discretionary_pct": discretionary_pct, "unknown_pct": 3.0, "largest_discretionary": None, "holders": [],
    }


class Home:
    """A temp home: the smart-wallet DB from the shipped schema, a holder cache, and the alpha-tape paths."""

    def __init__(self, root: Path):
        self.root = root
        self.db = root / "smart_wallets.sqlite"
        self.cache = root / "holders"
        with closing(connect_db(self.db)):
            pass

    def buy(self, wallet: str, mint: str, at: datetime, n: int) -> None:
        run_id = f"elite-test-{n}"
        with closing(connect_db(self.db)) as con:
            con.execute("INSERT INTO ingestion_runs(run_id,source_id,started_at,status) VALUES(?,?,?,?)", (run_id, "solana_rpc", iso(at), "completed"))
            upsert_wallet(con, wallet)
            insert_event(con, run_id, {"wallet": wallet, "mint": mint, "signature": f"sig{n}", "block_time_utc": iso(at), "event_type": "buy", "sol_delta": -1.5}, source_id="solana_rpc")
            con.commit()

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute(sql, params).fetchall()

    def patches(self) -> list:
        return [
            mock.patch.object(fast_lane_writers, "DEFAULT_DB", self.db),
            mock.patch.object(holder_resolver, "HOLDER_CACHE", self.cache),
            mock.patch.object(alpha_tape, "DB_PATH", self.db),
            mock.patch.object(alpha_tape, "SIGNAL_LEDGER_PATH", self.root / "missing-ledger.sqlite"),
            mock.patch.object(alpha_tape, "SECONDARY_SUMMARY_PATH", self.root / "missing-summary.json"),
            mock.patch.object(alpha_tape, "SECONDARY_SCORE_DIR", self.root / "missing-scores"),
        ]


def ranked(*mints: str) -> list[dict]:
    return [{"mint": m, "sources": ["boosts_top"], "summary": {"marketCap": 90_000.0 + i}, "candidate_score": 100.0 - i} for i, m in enumerate(mints)]


class FastLaneWriterTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Home(Path(self.td.name))
        for p in self.home.patches():
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.td.cleanup()

    def signal_rows(self) -> list[tuple]:
        return self.home.rows("SELECT mint,wallet_count,tg_channel_count,signal_type FROM token_signals WHERE source_id='chaos_sweep' ORDER BY mint")

    def test_one_row_per_ranked_mint_per_bucket(self):
        now = datetime.now(timezone.utc)
        self.home.buy(WALLETS[0], MINT_A, now - timedelta(minutes=5), 1)
        self.home.buy(WALLETS[1], MINT_A, now - timedelta(minutes=4), 2)
        self.home.buy(WALLETS[0], MINT_C, now - timedelta(minutes=3), 3)
        for m in (MINT_A, MINT_B):
            holder_resolver.write_cached_holders(sample(m, 12.5))
        start = datetime.fromisoformat(fast_lane_writers.bucket_start(now))

        fast_lane_writers.write_ranked(ranked(MINT_A, MINT_B), now=start + timedelta(minutes=1))
        self.assertEqual(self.signal_rows(), [(MINT_A, 2, 0, "sweep-rank"), (MINT_B, 0, 0, "sweep-rank")])
        self.assertEqual(self.home.rows("SELECT COUNT(*) FROM token_signals WHERE mint=?", (MINT_C,)), [(0,)])

        # Same bucket: rows are updated, not added.
        moved = ranked(MINT_A, MINT_B)
        moved[0]["summary"]["marketCap"] = 250_000.0
        fast_lane_writers.write_ranked(moved, now=start + timedelta(minutes=9))
        self.assertEqual(len(self.signal_rows()), 2)
        self.assertEqual(self.home.rows("SELECT COUNT(*) FROM token_concentration_snapshots"), [(2,)])
        self.assertEqual(self.home.rows("SELECT current_market_cap_usd FROM token_signals WHERE mint=?", (MINT_A,)), [(250_000.0,)])
        self.assertEqual(self.home.rows("SELECT market_cap_usd FROM token_concentration_snapshots WHERE mint=?", (MINT_A,)), [(250_000.0,)])

        # Next bucket: a new row per ranked mint.
        fast_lane_writers.write_ranked(ranked(MINT_A, MINT_B), now=start + timedelta(minutes=16))
        self.assertEqual(len(self.signal_rows()), 4)
        self.assertEqual(self.home.rows("SELECT COUNT(*) FROM token_concentration_snapshots"), [(4,)])

    def test_snapshot_fields_come_from_the_holder_sample(self):
        holder_resolver.write_cached_holders(sample(MINT_A, 18.25))
        result = fast_lane_writers.write_ranked(ranked(MINT_A))
        self.assertEqual(result["holder_data"], {MINT_A: "cached 0m"})
        (supply_pct, holder_count, market_cap, meta), = self.home.rows(
            "SELECT supply_pct,holder_count,market_cap_usd,metadata_json FROM token_concentration_snapshots WHERE mint=?", (MINT_A,))
        meta = json.loads(meta)
        self.assertEqual(supply_pct, 18.25)
        self.assertIsNone(holder_count)  # no holder read gives a real holder count; the sample size is not one
        self.assertEqual(market_cap, 90_000.0)
        self.assertEqual((meta["raw_top_pct"], meta["lp_pool_pct"], meta["holder_data"], meta["writer"]), (41.0, 22.0, "cached 0m", "sweep"))

    def test_an_unserved_holder_read_writes_no_snapshot(self):
        unserved = {"mint": MINT_A, "holder_data": "unavailable (not served by this RPC)"}
        result = fast_lane_writers.write_ranked(ranked(MINT_A), holder_func=lambda mint: unserved)
        self.assertEqual((result["token_signals"], result["concentration_snapshots"]), (1, 0))
        self.assertEqual(self.home.rows("SELECT COUNT(*) FROM token_concentration_snapshots"), [(0,)])
        payload = alpha_tape.token_payload(MINT_A, dex=False)
        self.assertIn("concentration stale: unknown", payload["freshness"]["stale_reasons"])

    def test_old_rows_still_read_as_stale_tape(self):
        then = datetime.now(timezone.utc) - timedelta(hours=2)
        self.home.buy(WALLETS[0], MINT_A, then - timedelta(minutes=5), 1)
        self.home.buy(WALLETS[1], MINT_A, then - timedelta(minutes=4), 2)
        fast_lane_writers.write_ranked(ranked(MINT_A), now=then, holder_func=lambda mint: sample(mint, 9.0))
        payload = alpha_tape.token_payload(MINT_A, dex=False)
        self.assertEqual(payload["freshness"]["status"], "stale")
        self.assertEqual(payload["gate"]["verdict"], "stale-tape")

    def test_writer_adds_under_two_seconds_per_mint(self):
        mints = [f"{i}{MINT_A[1:]}" for i in range(1, 10)] + [MINT_B]
        for m in mints:
            holder_resolver.write_cached_holders(sample(m, 10.0))
        started = time.perf_counter()
        result = fast_lane_writers.write_ranked(ranked(*mints))
        per_mint = (time.perf_counter() - started) / len(mints)
        self.assertEqual((result["token_signals"], result["concentration_snapshots"]), (10, 10))
        self.assertLess(per_mint, 2.0)


class SweepFillsTheFastLanesTests(unittest.TestCase):
    """One real `trending_token_sweep` run over a synthetic DexScreener payload, after one seeded ingest."""

    def discovery(self, path: str, timeout: int = 20):
        rows = {
            "/token-boosts/top/v1": [
                {"chainId": "solana", "tokenAddress": MINT_A, "amount": 50, "totalAmount": 500},
                {"chainId": "solana", "tokenAddress": MINT_B, "amount": 40, "totalAmount": 400},
            ],
            "/token-boosts/latest/v1": [{"chainId": "solana", "tokenAddress": MINT_C, "amount": 1, "totalAmount": 1}],
            "/token-profiles/latest/v1": [],
        }
        return rows[path], None

    @staticmethod
    def market(chain: str, mint: str, cache: bool = True):
        liquidity = {MINT_A: 80_000.0, MINT_B: 60_000.0, MINT_C: 1_000.0}[mint]
        symbol = {MINT_A: "ALPHA", MINT_B: "BETA", MINT_C: "GAMMA"}[mint]
        # MINT_B sits on the quote side of its pair, as a token paired against itself sometimes does on DexScreener.
        sides = {"baseToken": {"address": mint, "symbol": symbol}, "quoteToken": {"address": WSOL, "symbol": "SOL"}}
        if mint == MINT_B:
            sides = {"baseToken": {"address": WSOL, "symbol": "SOL"}, "quoteToken": {"address": mint, "symbol": symbol}}
        return {"pair_count": 1, "pairs": [sides],
                "summary": {"marketCap": liquidity * 3, "liquidity_usd": liquidity, "volume_h1": liquidity / 2, "txns_h1": {"buys": 40, "sells": 20}}}

    def test_after_one_ingest_and_one_sweep_the_fast_lanes_are_not_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = Home(root)
            for p in home.patches():
                p.start()
                self.addCleanup(p.stop)
            now = datetime.now(timezone.utc)
            home.buy(WALLETS[0], MINT_A, now - timedelta(minutes=6), 1)
            home.buy(WALLETS[1], MINT_A, now - timedelta(minutes=5), 2)
            for m in (MINT_A, MINT_B):
                holder_resolver.write_cached_holders(sample(m, 11.0))

            before = alpha_tape.token_payload(MINT_A, dex=False)
            self.assertEqual(before["gate"]["verdict"], "stale-tape")
            self.assertEqual(alpha_tape.sweep_payload(limit=5)["freshness"]["status"], "stale")

            argv = ["trending_token_sweep.py", "--limit", "2", "--deep", "0", "--out-dir", str(root / "sweeps"), "--raw"]
            out = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(sweep, "fetch_json", self.discovery), \
                 mock.patch.object(sweep, "fetch_token", self.market), \
                 mock.patch.object(sweep, "record_signal", functools.partial(signal_ledger.record_signal, db_path=root / "signal_ledger.sqlite")), \
                 redirect_stdout(out):
                sweep.main()
            payload = json.loads(out.getvalue())
            self.assertEqual([c["mint"] for c in payload["ranked_candidates"]], [MINT_A, MINT_B])
            self.assertEqual((payload["fast_lane"]["token_signals"], payload["fast_lane"]["concentration_snapshots"]), (2, 2))
            self.assertEqual(home.rows("SELECT mint,wallet_count FROM token_signals ORDER BY mint"), [(MINT_A, 2), (MINT_B, 0)])

            token = alpha_tape.token_payload(MINT_A, dex=False)
            self.assertEqual(token["freshness"]["status"], "fresh")
            self.assertNotEqual(token["gate"]["verdict"], "stale-tape")
            fast_sweep = alpha_tape.sweep_payload(limit=5)
            self.assertEqual(fast_sweep["freshness"]["status"], "fresh")
            verdicts = {c["mint"]: c["gate"]["verdict"] for c in fast_sweep["candidates"]}
            self.assertEqual(set(verdicts), {MINT_A, MINT_B})
            self.assertNotIn("stale-tape", verdicts.values())
            self.assertEqual(home.rows("SELECT mint,symbol FROM tokens WHERE mint IN (?,?) ORDER BY mint", (MINT_A, MINT_B)), [(MINT_A, "ALPHA"), (MINT_B, "BETA")])
            card = alpha_tape.render_sweep(fast_sweep)
            self.assertIn("**$ALPHA**", card)
            self.assertIn("**$BETA**", card)
            self.assertNotIn("$UNKNOWN", card)


if __name__ == "__main__":
    unittest.main()
