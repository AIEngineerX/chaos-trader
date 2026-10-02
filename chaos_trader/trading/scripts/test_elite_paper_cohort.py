from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import elite_paper_cohort as paper  # noqa: E402

WALLET_A = "A" * 31 + "B"
WALLET_B = "A" * 31 + "C"
MINT_A = "B" * 32
MINT_B = "C" * 32
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def roster() -> dict:
    return {
        "version": "elite-test-v1",
        "source_sha256": "f" * 64,
        "wallets": [WALLET_A, WALLET_B],
    }


_OPEN: list[sqlite3.Connection] = []  # closed in tearDownModule, so no connection is left to the garbage collector


def evidence_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    _OPEN.append(con)
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE wallet_token_events(
        id INTEGER PRIMARY KEY,wallet TEXT,mint TEXT,signature TEXT,block_time_utc TEXT,
        event_type TEXT,confidence TEXT,token_delta REAL,sol_delta REAL,fee_sol REAL,source_id TEXT,run_id TEXT
        )"""
    )
    return con


def add_event(con: sqlite3.Connection, event_id: int, *, mint: str = MINT_A, event_type: str = "buy", wallet: str = WALLET_A, at: datetime = T0, confidence: str = "medium", run_id: str | None = None) -> None:
    con.execute(
        "INSERT INTO wallet_token_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, wallet, mint, f"sig-{event_id}", paper.iso(at), event_type, confidence, 1000.0, -0.5, 0.000005, "helius_rpc", run_id or f"elite-test-{event_id}"),
    )
    con.commit()


class MappingFetcher:
    def __init__(self, mapping: dict[str, dict]):
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, mint: str) -> dict:
        self.calls.append(mint)
        return deepcopy(self.mapping[mint])


def market(price: float, liquidity: float = 100_000.0, *, symbol: str = "TOK") -> dict:
    return {
        "price_usd": price,
        "liquidity_usd": liquidity,
        "market_cap": 1_000_000.0,
        "best_pair": "pair",
        "dex_id": "pumpswap",
        "symbol": symbol,
    }


class ElitePaperCohortTests(unittest.TestCase):
    def connect(self, root: str) -> sqlite3.Connection:
        con = paper.connect(Path(root) / "paper.sqlite")
        self.addCleanup(con.close)
        return con

    def policy(self) -> dict:
        return deepcopy(paper.POLICY)

    def test_default_initialization_is_prospective_and_skips_backlog(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 7, at=T0 - timedelta(minutes=1))
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            fetcher = MappingFetcher({})
            result = paper.observe(con, evidence, roster(), checked_at=T0, fetcher=fetcher)
            self.assertTrue(result["initialized"])
            self.assertEqual(7, result["cursor_after"])
            self.assertEqual(0, result["events_scanned"])
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            self.assertEqual([], fetcher.calls)
            con.close()

    def test_only_fresh_elite_buys_open_and_transfer_quote_noise_is_excluded(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, mint=USDT, at=T0 - timedelta(minutes=2))
        add_event(evidence, 2, mint=MINT_B, event_type="token_transfer_in", at=T0 - timedelta(minutes=2))
        add_event(evidence, 3, mint=MINT_A, at=T0 - timedelta(minutes=2))
        add_event(evidence, 4, mint=MINT_A, wallet=WALLET_B, at=T0 - timedelta(minutes=1))
        fetcher = MappingFetcher({MINT_A: market(0.001), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            result = paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=fetcher)
            self.assertEqual(2, result["events_scanned"])
            self.assertEqual(1, result["mints_seen"])
            self.assertEqual(1, result["opened"])
            row = con.execute("SELECT * FROM episodes").fetchone()
            self.assertEqual("OPEN", row["state"])
            self.assertEqual(2, row["source_wallet_count"])
            self.assertEqual(1.0, row["entry_notional_sol"])
            self.assertAlmostEqual(0.02, row["entry_cost_sol"])
            cursor = paper.get_meta(con, "cursor_event_id")
            self.assertIsNotNone(cursor)
            self.assertEqual(4, int(cursor or 0))
            con.close()

    def test_shadow_lineage_cannot_open_or_cluster_elite_episode(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, wallet=WALLET_A, at=T0 - timedelta(minutes=2), run_id="shadow-overlap")
        add_event(evidence, 2, wallet=WALLET_B, at=T0 - timedelta(minutes=1), run_id="elite-authoritative")

        eligible = paper.eligible_buy_rows(evidence, roster()["wallets"], 0, 2)
        self.assertEqual([2], [row["id"] for row in eligible])
        source_ids, wallet_count = paper.cluster_evidence(evidence, roster()["wallets"], MINT_A, T0, 2)
        self.assertEqual([2], source_ids)
        self.assertEqual(1, wallet_count)

    def test_late_baseline_is_rejected_without_market_call(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0 - timedelta(hours=2))
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            fetcher = MappingFetcher({})
            result = paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=fetcher)
            self.assertEqual(1, result["rejected"])
            self.assertEqual(1, result["late"])
            self.assertEqual("late_baseline", con.execute("SELECT gate_reason FROM episodes").fetchone()[0])
            self.assertEqual([], fetcher.calls)
            con.close()

    def test_liquidity_cap_is_enforced_not_decorative(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0 - timedelta(minutes=1))
        fetcher = MappingFetcher({MINT_A: market(0.001, 50_000), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            result = paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=fetcher)
            self.assertEqual(1, result["rejected"])
            self.assertEqual("notional_exceeds_liquidity_cap", con.execute("SELECT gate_reason FROM episodes").fetchone()[0])
            con.close()

    def test_fixed_horizon_marks_use_sol_benchmark_costs_and_late_validity(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0)
        baseline = MappingFetcher({MINT_A: market(0.001), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            paper.observe(con, evidence, roster(), checked_at=T0 + timedelta(minutes=1), from_event_id=0, fetcher=baseline)
            mark_fetcher = MappingFetcher({MINT_A: market(0.0012), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
            one_h = paper.collect_marks(con, checked_at=T0 + timedelta(hours=1, minutes=5), fetcher=mark_fetcher)
            self.assertEqual(1, one_h["valid_marks"])
            row = con.execute("SELECT * FROM marks WHERE window_label='1h'").fetchone()
            self.assertAlmostEqual(20.0, row["token_vs_sol_return_pct"], places=6)
            self.assertAlmostEqual(0.15248, row["net_pnl_sol"], places=6)

            final_fetcher = MappingFetcher({MINT_A: market(0.0008), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
            final = paper.collect_marks(con, checked_at=T0 + timedelta(hours=24, minutes=5), fetcher=final_fetcher)
            self.assertEqual(2, final["marks_written"])
            six_h = con.execute("SELECT * FROM marks WHERE window_label='6h'").fetchone()
            day = con.execute("SELECT * FROM marks WHERE window_label='24h'").fetchone()
            self.assertEqual(("late_mark", 0), (six_h["mark_status"], six_h["calibration_valid"]))
            self.assertEqual(("ok", 1), (day["mark_status"], day["calibration_valid"]))
            episode = con.execute("SELECT * FROM episodes").fetchone()
            self.assertEqual("CLOSED", episode["state"])
            self.assertAlmostEqual(day["net_pnl_sol"], episode["final_net_pnl_sol"])
            con.close()

    def test_transient_market_failure_defers_marks_and_24h_close_until_retry(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0)
        baseline = MappingFetcher({MINT_A: market(0.001), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=baseline)
            paper.collect_marks(con, checked_at=T0 + timedelta(hours=1, minutes=5), fetcher=baseline)
            paper.collect_marks(con, checked_at=T0 + timedelta(hours=6, minutes=5), fetcher=baseline)

            def unavailable(_mint: str) -> dict:
                raise RuntimeError("market unavailable")

            failed_cycle = paper.run_cycle(
                con,
                evidence_db(),
                roster(),
                checked_at=T0 + timedelta(hours=24, minutes=5),
                from_event_id=0,
                limit=1,
                fetcher=unavailable,
            )
            self.assertFalse(failed_cycle["ok"])
            failed = failed_cycle["marking"]
            self.assertEqual((1, 1, 0), (failed["market_errors"], failed["marks_deferred"], failed["marks_written"]))
            self.assertEqual(2, con.execute("SELECT COUNT(*) FROM marks").fetchone()[0])
            self.assertEqual("OPEN", con.execute("SELECT state FROM episodes").fetchone()[0])

            recovered = MappingFetcher({MINT_A: market(0.0009), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
            retry = paper.collect_marks(con, checked_at=T0 + timedelta(hours=24, minutes=10), fetcher=recovered)
            self.assertEqual((1, 0, 1), (retry["marks_written"], retry["marks_deferred"], retry["closed"]))
            self.assertEqual("CLOSED", con.execute("SELECT state FROM episodes").fetchone()[0])
            self.assertEqual("ok", con.execute("SELECT final_status FROM episodes").fetchone()[0])
            con.close()

    def test_market_failure_after_grace_records_terminal_missing_24h_mark(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0)
        baseline = MappingFetcher({MINT_A: market(0.001), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=baseline)
            paper.collect_marks(con, checked_at=T0 + timedelta(hours=1, minutes=5), fetcher=baseline)
            paper.collect_marks(con, checked_at=T0 + timedelta(hours=6, minutes=5), fetcher=baseline)

            def unavailable(_mint: str) -> dict:
                raise RuntimeError("market unavailable")

            result = paper.collect_marks(con, checked_at=T0 + timedelta(hours=24, minutes=40), fetcher=unavailable)
            self.assertEqual((1, 0, 1, 1), (result["market_errors"], result["marks_deferred"], result["marks_written"], result["closed"]))
            final = con.execute("SELECT state,final_status FROM episodes").fetchone()
            self.assertEqual(("CLOSED", "missing_market"), (final["state"], final["final_status"]))
            con.close()

    def test_due_filter_runs_before_the_200_episode_network_bound(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0)
        baseline = MappingFetcher({MINT_A: market(0.001), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        checked_at = T0 + timedelta(hours=7)
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=baseline)
            template = dict(con.execute("SELECT * FROM episodes").fetchone())
            blocker_ids = [template["episode_id"]]
            for index in range(1, 200):
                row = dict(template)
                row["episode_id"] = f"blocker-{index:03d}"
                row["mint"] = f"B{index:031d}"
                columns = list(row)
                con.execute(
                    f"INSERT INTO episodes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
                blocker_ids.append(row["episode_id"])
            target = dict(template)
            target["episode_id"] = "target-due-1h"
            target["mint"] = MINT_B
            target["source_event_at_utc"] = paper.iso(T0 + timedelta(hours=5, minutes=50))
            target["opened_at_utc"] = target["source_event_at_utc"]
            columns = list(target)
            con.execute(
                f"INSERT INTO episodes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                [target[column] for column in columns],
            )
            for episode_id in blocker_ids:
                for label, horizon in (("1h", 3600), ("6h", 21600)):
                    con.execute(
                        "INSERT INTO marks(episode_id,window_label,horizon_seconds,checked_at_utc,age_seconds,lateness_seconds,calibration_valid,mark_status,raw_market_json) VALUES(?,?,?,?,?,?,?,?,?)",
                        (episode_id, label, horizon, paper.iso(T0 + timedelta(seconds=horizon)), horizon, 0, 1, "ok", "{}"),
                    )
            con.commit()

            fetcher = MappingFetcher({MINT_B: market(0.0012), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
            result = paper.collect_marks(con, checked_at=checked_at, fetcher=fetcher, limit=200)
            self.assertEqual((201, 1, 1), (result["open_seen"], result["due_seen"], result["due_selected"]))
            self.assertEqual(1, result["marks_written"])
            self.assertIsNotNone(
                con.execute("SELECT 1 FROM marks WHERE episode_id='target-due-1h' AND window_label='1h'").fetchone()
            )
            con.close()

    def test_due_capacity_backlog_is_visible_and_makes_cycle_unhealthy(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0)
        calls: list[str] = []

        def constant_market(mint: str) -> dict:
            calls.append(mint)
            return market(200.0, 5_000_000, symbol="SOL") if mint == paper.SOL_MINT else market(0.001)

        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=constant_market)
            template = dict(con.execute("SELECT * FROM episodes").fetchone())
            for index in range(1, 201):
                row = dict(template)
                row["episode_id"] = f"due-{index:03d}"
                row["mint"] = f"D{index:031d}"
                row["source_event_at_utc"] = paper.iso(T0 + timedelta(seconds=index))
                row["opened_at_utc"] = row["source_event_at_utc"]
                columns = list(row)
                con.execute(
                    f"INSERT INTO episodes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
            con.commit()
            calls.clear()

            result = paper.run_cycle(
                con,
                evidence_db(),
                roster(),
                checked_at=T0 + timedelta(hours=1, minutes=5),
                from_event_id=0,
                limit=1,
                fetcher=constant_market,
            )
            self.assertFalse(result["ok"])
            self.assertEqual((201, 199, 2, 200, 200), (
                result["marking"]["due_seen"],
                result["marking"]["due_selected"],
                result["marking"]["due_deferred_limit"],
                result["marking"]["network_calls_planned"],
                len(calls),
            ))
            self.assertEqual(199, result["marking"]["marks_written"])
            self.assertIsNotNone(con.execute("SELECT 1 FROM marks WHERE episode_id='due-198' AND window_label='1h'").fetchone())
            self.assertIsNone(con.execute("SELECT 1 FROM marks WHERE episode_id='due-199' AND window_label='1h'").fetchone())
            self.assertIsNone(con.execute("SELECT 1 FROM marks WHERE episode_id='due-200' AND window_label='1h'").fetchone())
            con.close()

    def test_restart_is_idempotent_by_cursor_and_unique_mint(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, at=T0 - timedelta(minutes=1))
        fetcher = MappingFetcher({MINT_A: market(0.001), paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL")})
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            first = paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=fetcher)
            second = paper.observe(con, evidence, roster(), checked_at=T0 + timedelta(minutes=1), from_event_id=0, fetcher=fetcher)
            self.assertEqual(1, first["opened"])
            self.assertEqual(0, second["events_scanned"])
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            con.close()

    def test_cap_does_not_advance_cursor_past_unprocessed_mint(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, mint=MINT_A, at=T0 - timedelta(minutes=1))
        add_event(evidence, 2, mint=MINT_B, at=T0 - timedelta(minutes=1))
        fetcher = MappingFetcher({
            MINT_A: market(0.001), MINT_B: market(0.002),
            paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL"),
        })
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            first = paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, limit=1, fetcher=fetcher)
            self.assertEqual(1, first["cursor_after"])
            self.assertEqual(1, first["opened"])
            second = paper.observe(con, evidence, roster(), checked_at=T0 + timedelta(minutes=1), limit=1, fetcher=fetcher)
            self.assertEqual(2, second["cursor_after"])
            self.assertEqual(1, second["opened"])
            self.assertEqual(2, con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
            con.close()

    def test_cycle_observation_cap_does_not_starve_mark_coverage(self) -> None:
        evidence = evidence_db()
        add_event(evidence, 1, mint=MINT_A, at=T0)
        fetcher = MappingFetcher({
            MINT_A: market(0.001),
            paper.SOL_MINT: market(200.0, 5_000_000, symbol="SOL"),
        })
        with tempfile.TemporaryDirectory() as td:
            con = self.connect(td)
            paper.observe(con, evidence, roster(), checked_at=T0, from_event_id=0, fetcher=fetcher)
            template = dict(con.execute("SELECT * FROM episodes WHERE mint=?", (MINT_A,)).fetchone())
            for index in range(5):
                row = dict(template)
                row["episode_id"] = f"newer-not-due-{index}"
                row["mint"] = f"N{index}" * 16
                fetcher.mapping[row["mint"]] = market(0.001)
                row["source_event_at_utc"] = paper.iso(T0 + timedelta(minutes=index + 1))
                row["opened_at_utc"] = paper.iso(T0 + timedelta(minutes=index + 1))
                columns = list(row)
                con.execute(
                    f"INSERT INTO episodes({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
            con.commit()

            # Simulate five older opens whose 1h marks are already complete while
            # the sixth open still needs its 1h mark. A mark scan capped by the
            # observation limit would inspect only the first row and miss it.
            paper.collect_marks(
                con,
                checked_at=T0 + timedelta(hours=1, minutes=6),
                fetcher=fetcher,
            )
            target_episode_id = "newer-not-due-4"
            con.execute("DELETE FROM marks WHERE episode_id=?", (target_episode_id,))
            con.commit()

            empty_evidence = evidence_db()
            result = paper.run_cycle(
                con,
                empty_evidence,
                roster(),
                checked_at=T0 + timedelta(hours=1, minutes=10),
                from_event_id=0,
                limit=1,
                fetcher=fetcher,
            )
            self.assertEqual(6, result["marking"]["open_seen"])
            self.assertEqual(1, result["marking"]["marks_written"])
            self.assertIsNotNone(
                con.execute("SELECT 1 FROM marks WHERE episode_id=? AND window_label='1h'", (target_episode_id,)).fetchone()
            )
            con.close()

    def test_module_does_not_depend_on_legacy_paper_or_execution(self) -> None:
        text = (SCRIPT_DIR / "elite_paper_cohort.py").read_text(encoding="utf-8")
        for forbidden in ("chaos_paper_autopilot", "paper_autopilot.sqlite", "signal_ledger.sqlite", "wallet_adapter", "sign_transaction", "execution_router", "x_search"):
            self.assertNotIn(forbidden, text)

    def test_cycle_and_observe_before_the_first_ingest_print_one_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME")}
            env.update(CHAOS_HOME=str(home), PYTHONIOENCODING="utf-8")
            paper_db = home / "paper.sqlite"
            for command in ("cycle", "observe"):
                with self.subTest(command=command):
                    p = subprocess.run(
                        [sys.executable, str(SCRIPT_DIR / "elite_paper_cohort.py"), command,
                         "--db", str(paper_db), "--evidence-db", str(home / "smart_wallets.sqlite")],
                        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, check=False,
                    )
                    self.assertEqual(0, p.returncode, p.stdout + p.stderr)
                    self.assertEqual(paper.NO_INGEST, p.stdout.strip())
                    self.assertEqual("", p.stderr)
                    self.assertFalse(paper_db.exists())


def tearDownModule() -> None:
    for con in _OPEN:
        con.close()


if __name__ == "__main__":
    unittest.main()
