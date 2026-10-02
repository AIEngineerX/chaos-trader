#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import chaos_paper_autopilot
from chaos_paper_autopilot import PaperAutopilotRunner, RunnerConfig, coerce_flag, today_utc
from paper_autopilot_config_check import load_config, validate
from alpha_tape import EXCLUDED_SWEEP_MINTS

MINT = "8wxkvAfEns76yBzu4MnbV7VnXWjg3iDPA9uwAQ6cpump"


def config_for(root: Path) -> RunnerConfig:
    return RunnerConfig(raw={
        "mode": "paper_only",
        "boundary": {
            "no_execution": True,
            "no_wallet": True,
            "no_signing": True,
            "no_order_routing": True,
            "no_webhooks": True,
        },
        "paths": {"sqlite": str(root / "paper.sqlite")},
        "loop": {"enabled": False, "discovery_interval_sec": 1},
        "budgets": {"max_candidates": 5, "max_open_positions": 3, "max_new_entries_per_hour": 5, "max_new_entries_per_day": 25, "max_same_narrative_positions": 2, "max_same_source_positions": 2},
        "deep_analyze": {"max_per_day": 100, "max_per_hour": 10, "tx_limit": 1},
        "market_discovery": {"enabled": False, "limit": 5, "min_liquidity_usd": 25_000, "min_market_cap_usd": 25_000, "max_market_cap_usd": 750_000, "min_h1_price_change_pct": 10, "max_h1_price_change_pct": 180, "min_volume_liquidity_ratio": 1.5, "max_volume_liquidity_ratio": 25, "repeat_sweep_hits_required": 2, "probe_notional_usd": 25},
        "x_research": {"enabled": True, "max_per_day": 25, "max_per_hour": 5, "lookback_days": 1},
        "paper_size": {"base_risk_usd": 100, "max_notional_usd": 250, "liquidity_bps": 50, "min_simulated_size_usd": 25},
        "entry": {"allowed_decisions": ["paper_enter"], "allowed_entry_gates": ["micro-watch", "watch", "deep-check", "manual-review"], "blocked_entry_gates": ["avoid-entry", "exit-liquidity-watch"], "min_liquidity_usd": 25_000, "max_adjusted_holder_concentration_pct": 45, "require_wallet_or_catalyst": True, "wallet_signal_lane_enabled": True, "min_independent_elite_wallets": 2, "min_elite_weighted_score": 4, "min_wallet_clean_closed_positions": 3, "min_wallet_realized_pnl_sol": 0.0, "min_wallet_win_rate": 0.4, "single_wallet_a_probe_enabled": True, "single_wallet_a_probe_notional_usd": 25, "blocked_social_catalysts": ["spam-raid"]},
        "exit": {"liquidity_break_pct": -25, "tp1_trim_pct": 50, "tp2_trim_pct": 25, "trailing_stop_after_tp1_pct": -35, "stop_pct_conviction": -25, "low_cap_time_stop_min": 15},
        "risk": {"max_daily_loss_r": -3, "max_consecutive_losses": 3, "pause_after_consecutive_losses_min": 60, "pause_on_deep_analyze_errors_per_hour": 10, "pause_on_dex_errors_per_hour": 25, "disable_x_on_x_errors_per_hour": 5},
        "paper_fee_model": {"estimated": True, "provenance": "unit-test fixed bps estimate", "entry_bps": 100, "exit_bps": 100},
        "llm": {"fast_loop": False},
    }, db_path=root / "paper.sqlite")


def sweep_payload_fixture(limit: int = 5) -> dict:
    return {
        "ok": True,
        "candidates": [{
            "mint": MINT,
            "market": {"symbol": "GOOD", "market_cap": 180_000, "liquidity_usd": 60_000},
            "signal": {"token_symbol": "GOOD"},
            "gate": {"verdict": "watch"},
            "candidate_score": 99,
        }],
        "freshness": {"status": "test"},
    }


def analyze_payload_fixture() -> dict:
    return {
        "mint": MINT,
        "x_enabled": False,
        "market": {"symbol": "GOOD", "price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000},
        "classification": {"verdict": "watch", "attention_phase": "live", "x_risk": {"confidence": "low"}},
        "entry_gate": {"action": "watch"},
        "mode_context": {"mode": "conviction trench"},
        "social_catalyst": {"catalyst_type": "dev-stream", "source_quality": "medium"},
        "flow_conversion": {"conversion_status": "clean-flow", "fake_flow_severity": "none", "volume_liquidity_ratio": 2.0},
        "wallet_timing": {"watch_wallet_hit_count": 1},
        "token_scan": {"holder_resolution": {"adjusted_discretionary_pct": 22.0}},
        "x_attention": None,
    }


def source_gap_payload_fixture() -> dict:
    payload = analyze_payload_fixture()
    payload["entry_gate"] = {"action": "study"}
    payload["gate"] = {
        "gate": "study",
        "counts": {
            "hidden": 0,
            "early_hidden": 0,
            "scout": 0,
            "early_scout": 0,
            "tracked_buyers": 0,
            "tracked_sellers": 0,
            "cluster_edges": 0,
            "tg_channels": 0,
            "holders": 0,
        },
    }
    payload["wallet_timing"] = {
        "watch_wallet_hit_count": 0,
        "quality_wallet_hit_count": 0,
        "wallet_timing": [],
    }
    payload["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
    return payload


def seed_wallet_event_db(
    path: Path,
    *,
    run_id: str,
    observed_at: datetime,
    wallets: int = 1,
    event_type: str = "buy",
    side: str = "buy",
    source_id: str = "helius_rpc",
    confidence: str = "high",
    mint: str = MINT,
    run_status: str = "completed",
    source_commit: str = "a" * 64,
    roster_version: str = "elite-test-v1",
    wallet_tier: str | None = "B",
    notes_sha256: str | None = None,
    duplicate_wallet: bool = False,
    block_time_text: str | None = None,
    completed_at_text: str | None = None,
) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE wallet_token_events(id INTEGER PRIMARY KEY, run_id TEXT, wallet TEXT, mint TEXT, signature TEXT, side TEXT, event_type TEXT, block_time_utc TEXT, source_id TEXT, confidence TEXT)"
        )
        con.execute(
            "CREATE TABLE ingestion_runs(run_id TEXT PRIMARY KEY, source_id TEXT, source_commit TEXT, completed_at TEXT, status TEXT, notes TEXT)"
        )
        con.execute(
            "CREATE TABLE positions(wallet TEXT, status TEXT, transfer_contaminated INTEGER, realized_pnl_sol REAL)"
        )
        con.execute(
            "INSERT INTO ingestion_runs(run_id,source_id,source_commit,completed_at,status,notes) VALUES(?,?,?,?,?,?)",
            (
                run_id,
                source_id,
                source_commit,
                completed_at_text if completed_at_text is not None else (observed_at.isoformat(timespec="seconds") if run_status == "completed" else None),
                run_status,
                json.dumps({"roster_version": roster_version, "roster_sha256": notes_sha256 if notes_sha256 is not None else source_commit, "wallet_tier": wallet_tier}, sort_keys=True),
            ),
        )
        for idx in range(wallets):
            wallet = "wallet-0" if duplicate_wallet else f"wallet-{idx}"
            con.execute(
                "INSERT INTO wallet_token_events(run_id,wallet,mint,signature,side,event_type,block_time_utc,source_id,confidence) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, wallet, mint, f"sig-{idx}", side, event_type, block_time_text or observed_at.isoformat(timespec="seconds"), source_id, confidence),
            )
            for pnl in (0.5, 0.25, -0.1):
                con.execute("INSERT INTO positions(wallet,status,transfer_contaminated,realized_pnl_sol) VALUES(?,?,?,?)", (wallet, "closed", 0, pnl))
        con.commit()
    finally:
        con.close()


def insert_candidate(con: sqlite3.Connection, mint: str = MINT) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,updated_at_utc) VALUES(?,?,?,?,?,?)",
        (mint, "TEST", "DISCOVERED", stamp, stamp, stamp),
    )


class PaperAutopilotRunnerTests(unittest.TestCase):
    def test_run_once_discovers_but_source_first_gate_prevents_unconfirmed_analysis(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            with patch("chaos_paper_autopilot.sweep_payload", side_effect=lambda limit, **kw: sweep_payload_fixture(limit)), \
                 patch.object(runner, "run_analyze", return_value=analyze_payload_fixture()), \
                 patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}):
                out = runner.run_once(limit=1, analyze_top=1, with_x=False)
            self.assertTrue(out["ok"])
            self.assertEqual(out["discovered"], 1)
            self.assertEqual(out["decisions"][0]["decision"], "paper_wait")
            self.assertIn("no wallet", out["boundary"])
            con = sqlite3.connect(Path(td) / "paper.sqlite")
            try:
                pos_count = con.execute("SELECT COUNT(*) FROM paper_positions WHERE state='PAPER_OPEN'").fetchone()[0]
                event_types = {r[0] for r in con.execute("SELECT event_type FROM events").fetchall()}
            finally:
                con.close()
            self.assertEqual(pos_count, 0)
            self.assertNotIn("paper_position_open", event_types)
            self.assertNotIn("deep_analyze", event_types)
            self.assertNotIn("buy", event_types)
            self.assertNotIn("sell", event_types)

    def test_repeated_live_market_structure_can_open_tiny_probe_without_wallet_buy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config_for(root)
            cfg.raw["market_discovery"]["enabled"] = True
            runner = PaperAutopilotRunner(cfg)
            payload = analyze_payload_fixture()
            payload["wallet_timing"] = {"watch_wallet_hit_count": 0, "quality_wallet_hit_count": 0, "wallet_timing": []}
            payload["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
            market_source = {
                "mint": MINT,
                "source": "dexscreener_trending",
                "sources": ["boosts_top", "profiles_latest"],
                "market": {
                    "market_cap": 180_000,
                    "liquidity_usd": 60_000,
                    "volume_h1": 180_000,
                    "price_change_h1": 45,
                },
            }
            with patch.object(runner, "run_analyze", return_value=payload):
                with runner.connect() as con:
                    insert_candidate(con)
                    con.execute("UPDATE candidates SET market_sweep_hits=2, source_json=? WHERE mint=?", (json.dumps(market_source), MINT))
                    decision = runner.decide_candidate(con, MINT)
                    position = con.execute("SELECT simulated_notional_usd,policy_version FROM paper_positions WHERE mint=?", (MINT,)).fetchone()
            self.assertEqual("paper_enter", decision["decision"])
            self.assertEqual("market_structure_probe", decision["paper_lane"])
            self.assertEqual(25, decision["paper_plan"]["simulated_notional_usd"])
            self.assertIsNotNone(position)
            self.assertEqual(25, position[0])
            self.assertEqual("paper_policy_p0_v4_multilane", position[1])

    def test_budget_blocks_deep_analyze(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw["deep_analyze"]["max_per_day"] = 0
            runner = PaperAutopilotRunner(cfg)
            qualifying = {"eligible": True, "mint": MINT, "wallet_count": 2, "weighted_score": 4}
            with patch.object(runner, "elite_source_confirmation", return_value=qualifying):
                with runner.connect() as con:
                    decision = runner.decide_candidate(con, MINT)
            self.assertEqual(decision["decision"], "budget_exhausted")

    def test_fresh_elite_source_confirmation_upgrades_conviction_study_to_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            with patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()):
                with runner.connect() as con:
                    decision = runner.decide_candidate(con, MINT)
            self.assertEqual(decision["decision"], "paper_enter")
            self.assertEqual(decision["entry_action"], "watch")
            self.assertEqual(decision["source_confirmation"]["lane"], "elite")
            self.assertEqual(decision["source_confirmation"]["mint"], MINT)
            self.assertEqual(decision["source_confirmation"]["bridge_policy_version"], "elite_provenance_bridge_v1")
            self.assertEqual(decision["source_confirmation"]["weighted_score"], 4)
            self.assertEqual(decision["source_confirmation"]["tier_breakdown"]["B"], 2)
            self.assertEqual(len(decision["source_confirmation"]["lineage"]["event_ids"]), 2)
            self.assertEqual(decision["source_confirmation"]["lineage"]["runs"][0]["run_id"], "elite-proof")
            self.assertEqual(decision["source_confirmation"]["lineage"]["runs"][0]["roster_version"], "elite-test-v1")
            event_receipt = decision["source_confirmation"]["lineage"]["events"][0]
            self.assertEqual(event_receipt["event_type"], "buy")
            self.assertEqual(event_receipt["source_id"], "helius_rpc")
            self.assertEqual(event_receipt["confidence"], "high")
            self.assertEqual(event_receipt["signature"], "sig-0")

    def test_elite_rows_from_standard_rpc_confirm_like_helius_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-standard",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
                source_id="solana_rpc",
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            with patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()):
                with runner.connect() as con:
                    decision = runner.decide_candidate(con, MINT)
            self.assertEqual(decision["decision"], "paper_enter")
            self.assertEqual(decision["source_confirmation"]["lane"], "elite")
            self.assertEqual(len(decision["source_confirmation"]["lineage"]["event_ids"]), 2)
            self.assertEqual(decision["source_confirmation"]["lineage"]["events"][0]["source_id"], "solana_rpc")

    def test_single_elite_wallet_cannot_open_paper_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-single",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=1,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            with patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()):
                with runner.connect() as con:
                    insert_candidate(con)
                    decision = runner.decide_candidate(con, MINT)
                    position_count = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
            self.assertEqual("paper_wait", decision["decision"])
            self.assertEqual(0, position_count)
            self.assertIn("1/2 independent wallets", " ".join(decision["blockers"]))

    def test_two_tier_c_elite_wallets_cannot_open_paper_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-tier-c",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
                wallet_tier="C",
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            with patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()):
                with runner.connect() as con:
                    insert_candidate(con)
                    decision = runner.decide_candidate(con, MINT)
                    position_count = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
            self.assertEqual("paper_wait", decision["decision"])
            self.assertEqual(0, position_count)
            self.assertIn("tier weight below minimum: 2/4", " ".join(decision["blockers"]))

    def test_mint_identity_mismatch_fails_closed_at_every_handoff(self):
        wrong_mint = "Mint-B-Not-The-Queried-Candidate"
        for stage in ("analyzer", "confirmation", "decision"):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    runner = PaperAutopilotRunner(config_for(root))
                    payload = source_gap_payload_fixture()
                    analyzer_patch = patch.object(runner, "run_analyze", return_value=payload)
                    confirmation_patch = patch.object(
                        runner,
                        "elite_source_confirmation",
                        return_value={"eligible": True, "lane": "elite", "mint": MINT, "wallet_count": 2, "weighted_score": 4},
                    )
                    strategy_patch = patch(
                        "chaos_paper_autopilot.strategy_decide",
                        return_value={"mint": wrong_mint, "decision": "paper_enter"},
                    )
                    if stage == "analyzer":
                        payload["mint"] = wrong_mint
                    elif stage == "confirmation":
                        confirmation_patch = patch.object(
                            runner,
                            "elite_source_confirmation",
                            return_value={"eligible": True, "lane": "elite", "mint": wrong_mint},
                        )
                    contexts = [analyzer_patch, confirmation_patch]
                    if stage == "decision":
                        contexts.append(strategy_patch)
                    with contexts[0], contexts[1]:
                        if stage == "decision":
                            with contexts[2]:
                                with runner.connect() as con:
                                    insert_candidate(con)
                                    result = runner.decide_candidate(con, MINT)
                                    state = con.execute("SELECT state FROM candidates WHERE mint=?", (MINT,)).fetchone()[0]
                                    wrong_count = con.execute("SELECT COUNT(*) FROM candidates WHERE mint=?", (wrong_mint,)).fetchone()[0]
                                    position_count = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
                        else:
                            with runner.connect() as con:
                                insert_candidate(con)
                                result = runner.decide_candidate(con, MINT)
                                state = con.execute("SELECT state FROM candidates WHERE mint=?", (MINT,)).fetchone()[0]
                                wrong_count = con.execute("SELECT COUNT(*) FROM candidates WHERE mint=?", (wrong_mint,)).fetchone()[0]
                                position_count = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["mint"], MINT)
                    self.assertEqual(result["decision"], "paper_wait")
                    self.assertIn("identity mismatch", result["blockers"][0])
                    self.assertEqual(state, "PAPER_WAIT")
                    self.assertEqual(wrong_count, 0)
                    self.assertEqual(position_count, 0)

    def test_identity_failure_charges_consumed_x_and_deep_analyze_budgets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            payload = source_gap_payload_fixture()
            payload["mint"] = "wrong-mint"
            payload["x_request_made"] = True  # the analyzer reports that its X request went out
            qualifying = {"eligible": True, "lane": "elite", "mint": MINT, "wallet_count": 2, "weighted_score": 4}
            with patch.object(runner, "elite_source_confirmation", return_value=qualifying), \
                 patch.object(runner, "run_analyze", return_value=payload):
                with runner.connect() as con:
                    insert_candidate(con)
                    result = runner.decide_candidate(con, MINT, use_x=True)
                    counters = dict(con.execute("SELECT counter,value FROM budget_counters").fetchall())
                    x_events = con.execute("SELECT COUNT(*) FROM events WHERE event_type='x_search'").fetchone()[0]
                    x_checked = con.execute("SELECT x_checked FROM candidates WHERE mint=?", (MINT,)).fetchone()[0]
                    immediately_active = runner.active_candidates(con, with_x=True)
            self.assertEqual(result["decision"], "paper_wait")
            self.assertEqual(counters["deep_analyze"], 1)
            self.assertEqual(counters["x_search"], 1)
            self.assertEqual(x_events, 1)
            self.assertEqual(x_checked, 1)
            self.assertEqual(immediately_active, [])

    def test_missing_analyzer_mint_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            payload = source_gap_payload_fixture()
            payload.pop("mint")
            with patch.object(runner, "run_analyze", return_value=payload):
                with runner.connect() as con:
                    insert_candidate(con)
                    result = runner.decide_candidate(con, MINT)
                    position_count = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
            self.assertEqual(result["decision"], "paper_wait")
            self.assertEqual(result["mint"], MINT)
            self.assertEqual(position_count, 0)

    def test_persist_decision_rejects_expected_mint_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            with runner.connect() as con:
                with self.assertRaises(ValueError):
                    runner.persist_decision(
                        con,
                        {"mint": "wrong-mint", "decision": "paper_enter"},
                        expected_mint=MINT,
                    )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0], 0)

    def test_elite_source_database_is_opened_mode_ro(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            real_connect = sqlite3.connect
            opened = {}

            def capture_connect(database, *args, **kwargs):
                opened["database"] = str(database)
                opened["uri"] = kwargs.get("uri")
                return real_connect(database, *args, **kwargs)

            with patch("chaos_paper_autopilot.sqlite3.connect", side_effect=capture_connect):
                confirmation = runner.elite_source_confirmation(MINT)
            self.assertTrue(confirmation["eligible"])
            self.assertTrue(opened["uri"])
            self.assertIn("?mode=ro", opened["database"])

    def test_mode_ro_sees_uncheckpointed_wal_and_cannot_be_made_writable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            now = datetime.now(timezone.utc) - timedelta(minutes=5)
            seed_wallet_event_db(smart_db, run_id="elite-main", observed_at=now)
            writer = sqlite3.connect(smart_db)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "INSERT INTO ingestion_runs(run_id,source_id,source_commit,completed_at,status,notes) VALUES(?,?,?,?,?,?)",
                    ("elite-wal", "helius_rpc", "b" * 64, now.isoformat(timespec="seconds"), "completed", json.dumps({"roster_version": "elite-test-v2", "roster_sha256": "b" * 64})),
                )
                writer.execute(
                    "INSERT INTO wallet_token_events(run_id,wallet,mint,side,event_type,block_time_utc,source_id,confidence) VALUES(?,?,?,?,?,?,?,?)",
                    ("elite-wal", "wallet-wal", MINT, "buy", "buy", now.isoformat(timespec="seconds"), "helius_rpc", "high"),
                )
                for pnl in (0.5, 0.25, -0.1):
                    writer.execute("INSERT INTO positions(wallet,status,transfer_contaminated,realized_pnl_sol) VALUES(?,?,?,?)", ("wallet-wal", "closed", 0, pnl))
                writer.commit()
                cfg = config_for(root)
                cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
                confirmation = PaperAutopilotRunner(cfg).elite_source_confirmation(MINT)
                self.assertTrue(confirmation["eligible"])
                self.assertEqual(confirmation["buy_events"], 2)
                ro = sqlite3.connect(f"{smart_db.resolve().as_uri()}?mode=ro", uri=True)
                try:
                    ro.execute("PRAGMA query_only=OFF")
                    with self.assertRaises(sqlite3.OperationalError):
                        ro.execute("INSERT INTO wallet_token_events(event_type) VALUES('buy')")
                finally:
                    ro.close()
            finally:
                writer.close()

    def test_duplicate_events_count_one_distinct_wallet_and_preserve_event_lineage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-duplicate-wallet",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
                duplicate_wallet=True,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            confirmation = PaperAutopilotRunner(cfg).elite_source_confirmation(MINT)
            self.assertTrue(confirmation["eligible"])
            self.assertEqual(confirmation["wallet_count"], 1)
            self.assertEqual(confirmation["buy_events"], 2)
            self.assertEqual(len(confirmation["lineage"]["event_ids"]), 2)

    def test_excluded_sweep_mint_cannot_be_source_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            excluded_mint = next(iter(EXCLUDED_SWEEP_MINTS))
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                mint=excluded_mint,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            confirmation = PaperAutopilotRunner(cfg).elite_source_confirmation(excluded_mint)
            self.assertFalse(confirmation["eligible"])
            self.assertIn("excluded", confirmation["reason"])

    def test_wallet_signal_lane_is_off_by_default(self):
        """With the lane flag absent from config, wallet evidence must not produce an entry."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            del cfg.raw["entry"]["wallet_signal_lane_enabled"]
            runner = PaperAutopilotRunner(cfg)
            with patch("chaos_paper_autopilot.sweep_payload", side_effect=lambda limit, **kw: sweep_payload_fixture(limit)), \
                 patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()), \
                 patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}):
                out = runner.run_once(limit=1, analyze_top=1, with_x=False)
            self.assertEqual(out["decisions"][0]["decision"], "paper_wait")
            con = sqlite3.connect(root / "paper.sqlite")
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0], 0)
            finally:
                con.close()

    def test_trending_candidates_cannot_starve_fresh_wallet_candidates(self):
        """Live defect: dexscreener_trending sorted first unconditionally, and trending rows
        are re-seen by every sweep while wallet rows are not. 10 of 10 analyse slots went to
        trending and 298 wallet candidates aged out unanalysed inside their 45-min window."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc)
            with runner.connect() as con:
                for i in range(12):  # trending: always freshly re-seen
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (f"trend{i}", f"T{i}", "DISCOVERED", now.isoformat(timespec="seconds"),
                         now.isoformat(timespec="seconds"), json.dumps({"source": "dexscreener_trending"}),
                         now.isoformat(timespec="seconds")),
                    )
                for i in range(3):  # wallet: fresh, inside the actionable window
                    seen = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (f"elite{i}", f"E{i}", "DISCOVERED", seen, seen,
                         json.dumps({"source": "elite-wallet-buys", "signal": {"captured_at_utc": seen}}), seen),
                    )
                con.commit()
                picked = runner.active_candidates(con, limit=3)
            sources = [json.loads(r["source_json"])["source"] for r in picked]
            # Non-starvation, not dominance: wallet candidates expire so they lead, but the
            # lanes interleave rather than one taking every slot.
            self.assertEqual(sources[0], "elite-wallet-buys", f"trending starved wallet candidates: {sources}")
            self.assertGreaterEqual(sources.count("elite-wallet-buys"), 2, sources)

    def test_neither_lane_can_starve_the_other(self):
        """Review P2: absolute wallet priority just reversed the original starvation. With 12
        fresh wallet rows and 5 trending rows at limit=2, the tick must still analyse one of
        each — the elite ingest seeds a whole batch at once that stays fresh for 45 minutes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc)
            fresh = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
            with runner.connect() as con:
                for i in range(12):
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (f"elite{i}", f"E{i}", "DISCOVERED", fresh, fresh,
                         json.dumps({"source": "elite-wallet-buys",
                                     "signal": {"captured_at_utc": fresh}}), fresh),
                    )
                for i in range(5):
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (f"trend{i}", f"T{i}", "DISCOVERED", now.isoformat(timespec="seconds"),
                         now.isoformat(timespec="seconds"), json.dumps({"source": "dexscreener_trending"}),
                         now.isoformat(timespec="seconds")),
                    )
                con.commit()
                picked = runner.active_candidates(con, limit=2)
            sources = [json.loads(r["source_json"])["source"] for r in picked]
            self.assertEqual(sources, ["elite-wallet-buys", "dexscreener_trending"], sources)

    def _elite_entry_decision(self, root: Path, *, wallets: int, tier: str) -> dict:
        smart_db = root / "smart.sqlite"
        seed_wallet_event_db(
            smart_db,
            run_id="elite-proof",
            observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            wallets=wallets,
            wallet_tier=tier,
        )
        cfg = config_for(root)
        cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
        cfg.raw["entry"]["min_independent_elite_wallets"] = 1
        cfg.raw["entry"]["min_elite_weighted_score"] = 1
        runner = PaperAutopilotRunner(cfg)
        with patch.object(runner, "run_analyze", return_value=analyze_payload_fixture()), \
             patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}):
            with runner.connect() as con:
                return runner.decide_candidate(con, MINT)

    def test_elite_entries_carry_a_source_key_for_concentration_budgets(self):
        """Review P1: _decision_key derives the source budget key from source_identity, which
        only the market lane set. Elite positions were invisible to
        budgets.max_same_source_positions and persisted with a null source — three opened
        against a configured limit of two."""
        with tempfile.TemporaryDirectory() as td:
            decision = self._elite_entry_decision(Path(td), wallets=1, tier="B")
            self.assertEqual(decision.get("decision"), "paper_enter", decision.get("blockers"))
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self.assertIsNotNone(runner._decision_key(decision, "source"))
            self.assertTrue(decision.get("paper_lane"), "elite entry must be attributable to a lane")

    def test_single_wallet_entries_are_probe_sized_regardless_of_tier(self):
        """Review P1: with the dial at 1/1 a single wallet of any tier clears the standard
        gate, but the probe cap only applied to tier A — so weaker B/C evidence was sized
        4x larger than A. Evidence strength must not be inversely related to size."""
        sizes = {}
        for tier in ("A", "B", "C"):
            with tempfile.TemporaryDirectory() as td:
                decision = self._elite_entry_decision(Path(td), wallets=1, tier=tier)
                self.assertEqual(decision.get("decision"), "paper_enter", decision.get("blockers"))
                sizes[tier] = float((decision.get("paper_plan") or {}).get("simulated_notional_usd") or 0)
        self.assertEqual(len(set(sizes.values())), 1, f"single-wallet size varies by tier: {sizes}")
        self.assertLessEqual(max(sizes.values()), 25.0, sizes)

    def test_pending_x_work_keeps_priority_across_both_lanes(self):
        """Review P2: once the lanes are queried separately the ORDER BY only ranks within a
        lane, so an ordinary wallet row could displace a PAPER_WAIT row still awaiting X.
        Unfinished work must outrank new work in either lane."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc)
            fresh = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
            with runner.connect() as con:
                for i in range(2):  # trending rows already analysed, still awaiting X
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,x_checked,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (f"xwait{i}", f"XW{i}", "PAPER_WAIT", now.isoformat(timespec="seconds"),
                         now.isoformat(timespec="seconds"), json.dumps({"source": "dexscreener_trending"}), 0,
                         now.isoformat(timespec="seconds")),
                    )
                for i in range(3):  # ordinary fresh wallet rows
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,x_checked,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (f"elite{i}", f"E{i}", "DISCOVERED", fresh, fresh,
                         json.dumps({"source": "elite-wallet-buys",
                                     "signal": {"captured_at_utc": fresh}}), 0, fresh),
                    )
                con.commit()
                picked = runner.active_candidates(con, limit=2, with_x=True)
            self.assertEqual([r["mint"] for r in picked], ["xwait0", "xwait1"], [r["mint"] for r in picked])

    def test_one_lane_still_fills_when_the_other_is_empty(self):
        """Interleaving must not cap throughput when only one lane has candidates."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with runner.connect() as con:
                for i in range(6):
                    con.execute(
                        "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (f"trend{i}", f"T{i}", "DISCOVERED", now, now,
                         json.dumps({"source": "dexscreener_trending"}), now),
                    )
                con.commit()
                picked = runner.active_candidates(con, limit=4)
            self.assertEqual(len(picked), 4)

    def test_queue_priority_uses_buy_time_not_discovery_time(self):
        """Review P2: discovery can lag the buy by half the window. A candidate discovered
        just now whose BUY is already expired must not hold analyse priority — live case
        was buy 12:49:38, discovered 13:15:57, selected 13:46:02, buy long dead."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc)
            expired_buy = (now - timedelta(minutes=70)).isoformat(timespec="seconds")
            with runner.connect() as con:
                con.execute(
                    "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("lateseed", "LS", "DISCOVERED", now.isoformat(timespec="seconds"),
                     now.isoformat(timespec="seconds"),
                     json.dumps({"source": "elite-wallet-buys",
                                 "signal": {"captured_at_utc": expired_buy}}),
                     now.isoformat(timespec="seconds")),
                )
                con.execute(
                    "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("trendfresh", "TF", "DISCOVERED", now.isoformat(timespec="seconds"),
                     now.isoformat(timespec="seconds"), json.dumps({"source": "dexscreener_trending"}),
                     now.isoformat(timespec="seconds")),
                )
                con.commit()
                picked = runner.active_candidates(con, limit=1)
            self.assertEqual(json.loads(picked[0]["source_json"])["source"], "dexscreener_trending")

    def test_malformed_source_json_does_not_abort_the_selector(self):
        """Review P2 latent: json_extract raises on malformed JSON, and active_candidates()
        runs outside the per-candidate handler, so one bad row would kill the whole tick."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with runner.connect() as con:
                con.execute(
                    "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("badjson", "BJ", "DISCOVERED", now, now, "{not valid json", now),
                )
                con.execute(
                    "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("goodrow", "GR", "DISCOVERED", now, now, json.dumps({"source": "dexscreener_trending"}), now),
                )
                con.commit()
                picked = runner.active_candidates(con, limit=5)
            self.assertIn("goodrow", [r["mint"] for r in picked])

    def test_elite_confirmation_survives_missing_wal_sidecars(self):
        """Review P1: strict mode=ro cannot create -shm/-wal, so a checkpointed WAL database
        failed to open and silently rejected every wallet candidate before analysis."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
            )
            # Reproducing the real condition needs a checkpointed WAL database, and whether
            # a strict mode=ro open then fails is platform-dependent — it does on the live
            # macOS host, not on Windows. So drive the failure directly: make every strict
            # mode=ro open raise, exactly as it did in production, and assert the
            # query_only fallback still answers.
            real_connect = sqlite3.connect

            def only_ro_fails(target, *args, **kwargs):
                if isinstance(target, str) and target.startswith("file:") and "mode=ro" in target:
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(target, *args, **kwargs)

            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            with patch("alpha_tape.sqlite3.connect", side_effect=only_ro_fails):
                confirmation = PaperAutopilotRunner(cfg).elite_source_confirmation(MINT)
            self.assertNotIn("unavailable", str(confirmation.get("reason") or ""))
            self.assertTrue(confirmation["eligible"], confirmation)

    def test_stale_wallet_candidates_do_not_jump_the_queue(self):
        """Only candidates still inside the confirmation window earn priority — a stale one
        cannot be source-confirmed, so it must not displace an analysable trending row."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner = PaperAutopilotRunner(config_for(root))
            now = datetime.now(timezone.utc)
            stale = (now - timedelta(hours=6)).isoformat(timespec="seconds")
            with runner.connect() as con:
                con.execute(
                    "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("elitestale", "ES", "DISCOVERED", stale, now.isoformat(timespec="seconds"),
                     json.dumps({"source": "elite-wallet-buys", "signal": {"captured_at_utc": stale}}), stale),
                )
                con.execute(
                    "INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,source_json,updated_at_utc)"
                    " VALUES(?,?,?,?,?,?,?)",
                    ("trendfresh", "TF", "DISCOVERED", now.isoformat(timespec="seconds"),
                     now.isoformat(timespec="seconds"), json.dumps({"source": "dexscreener_trending"}),
                     now.isoformat(timespec="seconds")),
                )
                con.commit()
                picked = runner.active_candidates(con, limit=1)
            self.assertEqual(json.loads(picked[0]["source_json"])["source"], "dexscreener_trending")

    def test_wallet_evidence_cannot_enter_via_market_probe_when_lane_off(self):
        """Review P1: wallet evidence must not ride an admitted market probe into an entry."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            cfg.raw["entry"]["wallet_signal_lane_enabled"] = False
            runner = PaperAutopilotRunner(cfg)

            def _boom(*a, **kw):
                raise AssertionError("elite_source_confirmation must not be consulted when the lane is off")

            # An admitted market probe is the failing path: it skips the early return,
            # so the wallet evidence must be absent from the pipeline, not merely ungated.
            eligible_market = {"eligible": True, "lane": "market_structure_probe", "sweep_hits": 2}
            with patch.object(runner, "elite_source_confirmation", side_effect=_boom), \
                 patch.object(runner, "market_probe_confirmation", return_value=eligible_market), \
                 patch.object(runner, "run_analyze", return_value=analyze_payload_fixture()), \
                 patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}):
                with runner.connect() as con:
                    decision = runner.decide_candidate(con, MINT)
            self.assertIsNone(decision.get("source_confirmation"))
            if decision.get("decision") == "paper_enter":
                self.assertEqual(decision.get("paper_lane"), "market_structure_probe")

    def test_analyzer_wallet_timing_cannot_carry_an_entry_when_lane_off(self):
        """Re-review P1: watch_hits alone is sufficient for paper_enter, so the
        analyzer's wallet_timing must be suppressed too, not just source_confirmation."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config_for(root)
            cfg.raw["entry"]["wallet_signal_lane_enabled"] = False
            runner = PaperAutopilotRunner(cfg)
            # Wallet evidence present, and deliberately no catalyst and no X, so wallet
            # timing is the ONLY thing that could justify an entry.
            payload = analyze_payload_fixture()
            payload["wallet_timing"] = {"watch_wallet_hit_count": 3, "quality_wallet_hit_count": 3}
            payload["social_catalyst"] = {"catalyst_type": "none", "source_quality": "none"}
            eligible_market = {"eligible": True, "lane": "market_structure_probe", "sweep_hits": 2}
            with patch.object(runner, "market_probe_confirmation", return_value=eligible_market), \
                 patch.object(runner, "run_analyze", return_value=payload), \
                 patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}):
                with runner.connect() as con:
                    decision = runner.decide_candidate(con, MINT)
            self.assertEqual(decision.get("watch_wallet_hits"), 0)
            self.assertNotIn("wallet timing/watch evidence: 3", decision.get("reasons") or [])
            # A market_structure_probe entry is permitted here by design — that lane is a
            # separate, untested hypothesis. What must not happen is an entry attributed to
            # wallet evidence.
            if decision.get("decision") == "paper_enter":
                self.assertEqual(decision.get("paper_lane"), "market_structure_probe")
                self.assertIsNone(decision.get("source_confirmation"))

    def test_config_check_rejects_malformed_entry_container(self):
        """Re-review P2: a non-mapping entry must fail validation, not raise."""
        base = load_config(SCRIPT_DIR.parent / "config" / "paper_autopilot.yaml")
        for bad in (True, [1, 2], "entry"):
            broken = json.loads(json.dumps(base))
            broken["entry"] = bad
            result = validate(broken)
            self.assertFalse(result["ok"], f"entry={bad!r} must fail validation")

    def test_coerce_flag_rejects_falsy_looking_and_malformed_values(self):
        """Review P1: bool('false') is True, so a quoted no must not enable the lane."""
        for value in ("false", "False", "FALSE", "no", "off", "0", "", "  ", {"bad": 1}, [1], None, 0, 0.0, False):
            self.assertFalse(coerce_flag(value), f"{value!r} must not enable a safety flag")
        for value in (True, "true", "TRUE", " yes ", "on", "1", 1):
            self.assertTrue(coerce_flag(value), f"{value!r} should enable")

    def test_config_check_rejects_missing_and_non_boolean_lane_flag(self):
        """Review P2: a typo'd or quoted flag must fail validation, not pass silently."""
        base = load_config(SCRIPT_DIR.parent / "config" / "paper_autopilot.yaml")

        missing = json.loads(json.dumps(base))
        del missing["entry"]["wallet_signal_lane_enabled"]
        self.assertFalse(validate(missing)["ok"])

        typo = json.loads(json.dumps(base))
        del typo["entry"]["wallet_signal_lane_enabled"]
        typo["entry"]["wallet_signals_lane_enabled"] = False
        self.assertFalse(validate(typo)["ok"])

        quoted = json.loads(json.dumps(base))
        quoted["entry"]["wallet_signal_lane_enabled"] = "false"
        self.assertFalse(validate(quoted)["ok"])

        self.assertTrue(validate(base)["ok"])

    def test_config_check_separates_trading_language_from_live_execution(self):
        base = load_config(SCRIPT_DIR.parent / "config" / "paper_autopilot.yaml")
        self.assertTrue(validate(base)["ok"])

        for paper_output in ("paper_enter", "paper_wait", "paper_avoid", "paper_trim", "paper_exit", "paper_expire"):
            with self.subTest(paper_output=paper_output):
                paper = json.loads(json.dumps(base))
                paper["boundary"]["allowed_outputs"] = [paper_output]
                self.assertTrue(validate(paper)["ok"])

        for forbidden in (
            "buy",
            "sell",
            "wallet_connect",
            "swap_link",
            "swap",
            "snipe",
            "execute",
            "route",
            "route_order",
            "sign",
            "sign_transaction",
            "submit",
            "submit_order",
            "live_order",
            "live-execution",
        ):
            with self.subTest(forbidden=forbidden):
                live = json.loads(json.dumps(base))
                live["boundary"]["allowed_outputs"] = [forbidden]
                result = validate(live)
                self.assertFalse(result["ok"])

        advisory = json.loads(json.dumps(base))
        advisory["boundary"]["advisory_verdicts"] = ["buy", "sell", "hold", "wait", "avoid", "trim", "exit"]
        self.assertTrue(validate(advisory)["ok"])

        configured = json.loads(json.dumps(base))
        configured["boundary"]["forbidden_words"].append("bridge")
        configured["boundary"]["allowed_outputs"] = ["bridge_request"]
        self.assertFalse(validate(configured)["ok"])

    def test_shipped_config_states_the_wallet_lane_decision_explicitly(self):
        """The lane's value is the operator's call; what must never drift is that the
        decision is stated explicitly and as a real boolean, so nobody inherits it by
        accident in either direction."""
        shipped = SCRIPT_DIR.parent / "config" / "paper_autopilot.yaml"
        self.assertTrue(shipped.is_file(), f"missing shipped config: {shipped}")
        config = load_config(shipped)
        entry = (config.get("entry") or {})
        self.assertIn("wallet_signal_lane_enabled", entry)
        self.assertIsInstance(entry["wallet_signal_lane_enabled"], bool)
        # Paper stays simulated regardless of the lane: these are the real safety bounds.
        self.assertEqual(config.get("mode"), "paper_only")
        for key in (
            "no_execution",
            "no_wallet",
            "no_signing",
            "no_order_routing",
            "no_swap_links",
            "no_webhooks",
            "no_live_alerts",
        ):
            self.assertIs((config.get("boundary") or {}).get(key), True, f"boundary.{key} must stay true")

        for key in ("no_swap_links", "no_live_alerts"):
            weakened = json.loads(json.dumps(config))
            weakened["boundary"][key] = False
            self.assertFalse(validate(weakened)["ok"])

    def test_run_once_fresh_elite_source_opens_auditable_paper_position_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(
                smart_db,
                run_id="elite-proof",
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                wallets=2,
            )
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            with patch("chaos_paper_autopilot.sweep_payload", side_effect=lambda limit, **kw: sweep_payload_fixture(limit)), \
                 patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()), \
                 patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}):
                out = runner.run_once(limit=1, analyze_top=1, with_x=False)
            self.assertEqual(out["decisions"][0]["decision"], "paper_enter")
            con = sqlite3.connect(root / "paper.sqlite")
            try:
                position = con.execute("SELECT state,decision_json FROM paper_positions").fetchone()
                event_types = {row[0] for row in con.execute("SELECT event_type FROM events")}
            finally:
                con.close()
            self.assertEqual(position[0], "PAPER_OPEN")
            persisted = json.loads(position[1])
            self.assertEqual(persisted["source_confirmation"]["run_id_prefix"], "elite-")
            receipt = persisted["source_confirmation"]["lineage"]["events"][0]
            source = sqlite3.connect(smart_db)
            try:
                source.execute("DELETE FROM wallet_token_events WHERE source_id='helius_rpc'")
                source.commit()
            finally:
                source.close()
            self.assertEqual(receipt["event_type"], "buy")
            self.assertEqual(receipt["wallet"], "wallet-0")
            self.assertEqual(receipt["signature"], "sig-0")
            self.assertNotIn("buy", event_types)
            self.assertNotIn("sell", event_types)

    def test_ordinary_or_stale_source_cannot_upgrade_conviction_study(self):
        now = datetime.now(timezone.utc)
        cases = (
            ("ordinary-lane", {"run_id": "helius-proof", "observed_at": now - timedelta(minutes=5)}),
            ("case-spoof", {"run_id": "Elite-spoof", "observed_at": now - timedelta(minutes=5)}),
            ("stale", {"run_id": "elite-stale", "observed_at": now - timedelta(minutes=46)}),
            ("future", {"run_id": "elite-future", "observed_at": now + timedelta(minutes=5)}),
            ("wrong-source", {"run_id": "elite-wrong-source", "observed_at": now - timedelta(minutes=5), "source_id": "secondary"}),
            ("low-confidence", {"run_id": "elite-low-confidence", "observed_at": now - timedelta(minutes=5), "confidence": "low"}),
            ("case-spoof-confidence", {"run_id": "elite-case-confidence", "observed_at": now - timedelta(minutes=5), "confidence": "HIGH"}),
            ("side-only-buy", {"run_id": "elite-side-only", "observed_at": now - timedelta(minutes=5), "event_type": "swap", "side": "buy"}),
            ("malformed-time", {"run_id": "elite-malformed-time", "observed_at": now - timedelta(minutes=5), "block_time_text": "not-a-time"}),
            ("incomplete-run", {"run_id": "elite-incomplete", "observed_at": now - timedelta(minutes=5), "run_status": "running"}),
            ("invalid-roster-hash", {"run_id": "elite-invalid-hash", "observed_at": now - timedelta(minutes=5), "source_commit": "not-a-sha256"}),
            ("mismatched-roster-hash", {"run_id": "elite-mismatched-hash", "observed_at": now - timedelta(minutes=5), "notes_sha256": "b" * 64}),
            ("empty-completed-at", {"run_id": "elite-empty-completion", "observed_at": now - timedelta(minutes=5), "completed_at_text": ""}),
            ("malformed-completed-at", {"run_id": "elite-malformed-completion", "observed_at": now - timedelta(minutes=5), "completed_at_text": "not-a-time"}),
            ("future-completed-at", {"run_id": "elite-future-completion", "observed_at": now - timedelta(minutes=5), "completed_at_text": (now + timedelta(minutes=5)).isoformat(timespec="seconds")}),
        )
        for label, event in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    smart_db = root / "smart.sqlite"
                    seed_wallet_event_db(smart_db, **event)
                    cfg = config_for(root)
                    cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
                    runner = PaperAutopilotRunner(cfg)
                    payload = source_gap_payload_fixture()
                    payload["source_confirmation"] = {
                        "eligible": True,
                        "lane": "elite",
                        "verified_by": "paper_autopilot.elite_source_confirmation_v1",
                        "wallet_count": 99,
                        "first_buy_utc": event["observed_at"].isoformat(timespec="seconds"),
                        "latest_buy_utc": event["observed_at"].isoformat(timespec="seconds"),
                    }
                    with patch.object(runner, "run_analyze", return_value=payload):
                        with runner.connect() as con:
                            decision = runner.decide_candidate(con, MINT)
                    self.assertEqual(decision["decision"], "paper_wait")
                    self.assertEqual(decision["entry_action"], "study")

    def test_missing_or_malformed_elite_database_fails_closed(self):
        for case in ("missing", "missing-table"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    smart_db = root / "smart.sqlite"
                    if case == "missing-table":
                        con = sqlite3.connect(smart_db)
                        con.execute("CREATE TABLE unrelated(x)")
                        con.close()
                    cfg = config_for(root)
                    cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
                    runner = PaperAutopilotRunner(cfg)
                    with patch.object(runner, "run_analyze", return_value=source_gap_payload_fixture()):
                        with runner.connect() as con:
                            decision = runner.decide_candidate(con, MINT)
                    self.assertEqual(decision["decision"], "paper_wait")
                    self.assertEqual(decision["entry_action"], "study")
                    if case == "missing":
                        self.assertFalse(smart_db.exists())

    def test_elite_source_does_not_bypass_holder_or_flow_blockers(self):
        for blocker in ("holder", "flow"):
            with self.subTest(blocker=blocker):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    smart_db = root / "smart.sqlite"
                    seed_wallet_event_db(
                        smart_db,
                        run_id="elite-proof",
                        observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                        wallets=2,
                    )
                    payload = source_gap_payload_fixture()
                    if blocker == "holder":
                        payload["token_scan"]["holder_resolution"]["adjusted_discretionary_pct"] = 50.0
                    else:
                        payload["flow_conversion"] = {"conversion_status": "dead-churn", "fake_flow_severity": "high"}
                    cfg = config_for(root)
                    cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
                    runner = PaperAutopilotRunner(cfg)
                    with patch.object(runner, "run_analyze", return_value=payload):
                        with runner.connect() as con:
                            decision = runner.decide_candidate(con, MINT)
                    self.assertEqual(decision["decision"], "paper_avoid")

    def test_elite_bridge_does_not_change_low_cap_policy(self):
        for wallets in (1, 2):
            with self.subTest(wallets=wallets):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    smart_db = root / "smart.sqlite"
                    seed_wallet_event_db(
                        smart_db,
                        run_id="elite-proof",
                        observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                        wallets=wallets,
                    )
                    payload = source_gap_payload_fixture()
                    payload["mode_context"] = {"mode": "low-cap trench"}
                    cfg = config_for(root)
                    cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
                    runner = PaperAutopilotRunner(cfg)
                    with patch.object(runner, "run_analyze", return_value=payload):
                        with runner.connect() as con:
                            decision = runner.decide_candidate(con, MINT)
                    self.assertEqual(decision["decision"], "paper_wait")
                    self.assertEqual(decision["entry_action"], "study")


    def test_entry_caps_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw["budgets"]["max_new_entries_per_day"] = 1
            runner = PaperAutopilotRunner(cfg)
            with runner.connect() as con:
                runner.log_event(con, "paper_position_open", mint="OLD", message="seed", payload={})
                con.commit()
            restarted = PaperAutopilotRunner(cfg)
            with restarted.connect() as con:
                decision = analyze_payload_fixture()
                decision.update({"decision": "paper_enter", "paper_plan": {"simulated_notional_usd": 100, "stop_pct": -25, "tp1_pct": 75, "tp2_pct": 200, "time_stop_minutes": 25}})
                blocked = restarted.apply_runtime_policy(con, decision)
            self.assertEqual(blocked["decision"], "paper_wait")
            self.assertTrue(any("max_new_entries_per_day" in b for b in blocked["blockers"]))

    def test_same_source_and_same_narrative_caps_block(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw["budgets"]["max_same_source_positions"] = 1
            cfg.raw["budgets"]["max_same_narrative_positions"] = 1
            runner = PaperAutopilotRunner(cfg)
            with runner.connect() as con:
                seed_decision = {"source_key": "alpha", "narrative_key": "cat", "versioning": {}}
                con.execute("INSERT INTO paper_positions(id,mint,symbol,state,opened_at_utc,decision_json,updated_at_utc) VALUES(?,?,?,?,?,?,?)", ("seed", "OLD", "OLD", "PAPER_OPEN", "2026-07-25T00:00:00+00:00", json.dumps(seed_decision), "2026-07-25T00:00:00+00:00"))
                decision = analyze_payload_fixture()
                decision.update({"decision": "paper_enter", "source_key": "alpha", "narrative_key": "cat", "paper_plan": {"simulated_notional_usd": 100, "stop_pct": -25, "tp1_pct": 75, "tp2_pct": 200, "time_stop_minutes": 25}})
                blocked = runner.apply_runtime_policy(con, decision)
            self.assertEqual(blocked["decision"], "paper_wait")
            self.assertTrue(any("max_same_source_positions" in b for b in blocked["blockers"]))
            self.assertTrue(any("max_same_narrative_positions" in b for b in blocked["blockers"]))

    def test_daily_loss_and_consecutive_loss_pause(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw["risk"]["max_daily_loss_r"] = -2
            cfg.raw["risk"]["max_consecutive_losses"] = 2
            cfg.raw["risk"]["pause_after_consecutive_losses_min"] = 60
            runner = PaperAutopilotRunner(cfg)
            with runner.connect() as con:
                recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for idx in range(2):
                    con.execute("INSERT INTO paper_positions(id,mint,symbol,state,opened_at_utc,closed_at_utc,realized_r,decision_json,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?)", (f"loss{idx}", f"LOSS{idx}", "LOSS", "PAPER_CLOSED", recent, recent, -1.1, "{}", recent))
                decision = analyze_payload_fixture()
                decision.update({"decision": "paper_enter", "paper_plan": {"simulated_notional_usd": 100, "stop_pct": -25, "tp1_pct": 75, "tp2_pct": 200, "time_stop_minutes": 25}})
                blocked = runner.apply_runtime_policy(con, decision)
            self.assertEqual(blocked["decision"], "paper_wait")
            self.assertTrue(any("max_daily_loss_r" in b for b in blocked["blockers"]))
            self.assertTrue(any("max_consecutive_losses" in b for b in blocked["blockers"]))

    def test_consecutive_loss_pause_expires_after_configured_minutes(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw["risk"]["max_daily_loss_r"] = -999
            cfg.raw["risk"]["max_consecutive_losses"] = 2
            cfg.raw["risk"]["pause_after_consecutive_losses_min"] = 60
            runner = PaperAutopilotRunner(cfg)
            with runner.connect() as con:
                stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
                for idx in range(2):
                    con.execute("INSERT INTO paper_positions(id,mint,symbol,state,opened_at_utc,closed_at_utc,realized_r,decision_json,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?)", (f"loss{idx}", f"LOSS{idx}", "LOSS", "PAPER_CLOSED", stale, stale, -1.1, "{}", stale))
                decision = analyze_payload_fixture()
                decision.update({"decision": "paper_enter", "entry_action": "watch", "watch_wallet_hits": 1, "social_catalyst": {"type": "dev-stream", "quality": "medium"}, "symbol": "GOOD", "paper_plan": {"simulated_notional_usd": 100, "stop_pct": -25, "tp1_pct": 75, "tp2_pct": 200, "time_stop_minutes": 25}})
                out = runner.apply_runtime_policy(con, decision)
                self.assertFalse(any("max_consecutive_losses" in b for b in out.get("blockers") or []))
                # End to end: the expired pause must actually let the entry open.
                self.assertEqual(out["decision"], "paper_enter")
                runner.persist_decision(con, out)
                con.commit()
                open_count = con.execute("SELECT COUNT(*) FROM paper_positions WHERE state='PAPER_OPEN'").fetchone()[0]
            self.assertEqual(open_count, 1)

    def test_versions_persist_on_paper_position(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            with runner.connect() as con:
                row = con.execute("SELECT strategy_version,policy_version,analyzer_version,source_roster_version,fee_model_version,decision_json FROM paper_positions").fetchone()
            self.assertTrue(all(row[idx] for idx in range(5)))
            payload = json.loads(row[5])
            self.assertEqual(row[0], payload["versioning"]["strategy_version"])

    def test_config_runtime_contract_covers_material_controls(self):
        expected = {
            "entry.allowed_decisions", "entry.allowed_entry_gates", "entry.blocked_entry_gates", "entry.require_wallet_or_catalyst",
            "budgets.max_new_entries_per_hour", "budgets.max_new_entries_per_day", "budgets.max_same_source_positions", "budgets.max_same_narrative_positions",
            "risk.max_daily_loss_r", "risk.max_consecutive_losses", "risk.pause_on_deep_analyze_errors_per_hour", "risk.pause_on_dex_errors_per_hour",
            "deep_analyze.max_per_hour", "x_research.max_per_hour", "exit.stop_pct_conviction", "exit.low_cap_time_stop_min",
        }
        self.assertTrue(expected.issubset(PaperAutopilotRunner.runtime_contract_keys()))


    def test_status_does_not_enable_loop(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            status = runner.status()
            self.assertFalse(status["loop_enabled_config"])
            self.assertEqual(status["position_states"], {})

    def _open_position(self, runner: PaperAutopilotRunner) -> None:
        with runner.connect() as con:
            con.execute("INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,updated_at_utc) VALUES(?,?,?,?,?,?)", (MINT, "GOOD", "DISCOVERED", "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00"))
            runner.persist_decision(con, {
                "mint": MINT,
                "symbol": "GOOD",
                "decision": "paper_enter",
                "entry_action": "watch",
                "watch_wallet_hits": 1,
                "social_catalyst": {"type": "dev-stream", "quality": "medium"},
                "market": {"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000},
                "paper_plan": {"simulated_notional_usd": 100, "stop_pct": -25, "tp1_pct": 75, "tp2_pct": 200, "time_stop_minutes": 25},
            })
            con.commit()

    def test_monitor_closes_stop_loss(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.0007, "market_cap": 126_000, "liquidity_usd": 60_000}):
                rows = runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT state, exit_reason, realized_r FROM paper_positions").fetchone()
            self.assertEqual(len(rows), 1)
            self.assertEqual(pos[0], "PAPER_CLOSED")
            self.assertEqual(pos[1], "stop_loss")
            self.assertLess(pos[2], 0)

    def test_stop_loss_books_observed_gap_with_fees(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            # Chaos polls marks; there is no resting order. A -80% observed gap
            # books -80%, and realized R is net of fees:
            # gross -80, entry fee 1.0, exit fee 20*1% = 0.2 -> net -81.2 -> R -3.248
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.0002, "market_cap": 36_000, "liquidity_usd": 60_000}):
                runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT state, exit_reason, realized_r FROM paper_positions").fetchone()
                fill = con.execute("SELECT execution_price_usd, payload_json FROM paper_fills WHERE side='exit'").fetchone()
            self.assertEqual(pos[0], "PAPER_CLOSED")
            self.assertEqual(pos[1], "stop_loss")
            self.assertAlmostEqual(pos[2], -3.248)
            self.assertAlmostEqual(fill[0], 0.0002)
            self.assertAlmostEqual(json.loads(fill[1])["return_pct"], -80.0)

    def test_slippage_adjusts_entry_price_and_exit_return(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config_for(Path(td))
            cfg.raw["paper_fee_model"]["entry_slippage_bps"] = 150
            cfg.raw["paper_fee_model"]["exit_slippage_bps"] = 150
            runner = PaperAutopilotRunner(cfg)
            self._open_position(runner)
            with runner.connect() as con:
                entry_price = con.execute("SELECT entry_price FROM paper_positions").fetchone()[0]
            self.assertAlmostEqual(entry_price, 0.001 * 1.015)
            # Observed gap vs slipped entry: 0.0002/0.001015 - 1 = -80.295567%.
            # Exit slippage 150bps -> net fill -80.591133%; proceeds 19.408867;
            # net of fees (entry 1.0, exit 0.194089) -> -81.785222 -> R -3.271409.
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.0002, "market_cap": 36_000, "liquidity_usd": 60_000}):
                runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT realized_r FROM paper_positions").fetchone()
                fill = con.execute("SELECT payload_json FROM paper_fills WHERE side='exit'").fetchone()
            self.assertAlmostEqual(pos[0], -3.271409, places=5)
            self.assertAlmostEqual(json.loads(fill[0])["return_pct"], -80.591133, places=4)

    def test_stale_candidates_are_not_active(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            stale = "2026-07-01T00:00:00+00:00"
            fresh = f"{today_utc()}T00:00:00+00:00"
            with runner.connect() as con:
                con.execute("INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,sweep_hits,updated_at_utc) VALUES(?,?,?,?,?,?,?)", ("OLDMINT", "OLD", "DISCOVERED", stale, stale, 99, stale))
                con.execute("INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,sweep_hits,updated_at_utc) VALUES(?,?,?,?,?,?,?)", (MINT, "GOOD", "DISCOVERED", fresh, fresh, 1, fresh))
                con.commit()
                active = [r["mint"] for r in runner.active_candidates(con, limit=10)]
            self.assertEqual(active, [MINT])

    def test_avoided_candidate_reconsidered_after_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            stale = "2026-07-09T00:00:00+00:00"
            with runner.connect() as con:
                con.execute("INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,updated_at_utc) VALUES(?,?,?,?,?,?)", (MINT, "GOOD", "AVOIDED", stale, stale, stale))
                con.commit()
            with patch("chaos_paper_autopilot.sweep_payload", side_effect=lambda limit, **kw: sweep_payload_fixture(limit)):
                with runner.connect() as con:
                    runner.discover(con, limit=1)
                    con.commit()
                    state = con.execute("SELECT state FROM candidates WHERE mint=?", (MINT,)).fetchone()[0]
            self.assertEqual(state, "DISCOVERED")
            # A freshly avoided candidate stays parked until the cooldown passes.
            with runner.connect() as con:
                con.execute("UPDATE candidates SET state='AVOIDED', updated_at_utc=? WHERE mint=?", (f"{today_utc()}T23:59:59+00:00", MINT))
                con.commit()
            with patch("chaos_paper_autopilot.sweep_payload", side_effect=lambda limit, **kw: sweep_payload_fixture(limit)):
                with runner.connect() as con:
                    runner.discover(con, limit=1)
                    con.commit()
                    state = con.execute("SELECT state FROM candidates WHERE mint=?", (MINT,)).fetchone()[0]
            self.assertEqual(state, "AVOIDED")

    def test_monitor_trims_tp1_and_records_remaining(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.0018, "market_cap": 324_000, "liquidity_usd": 70_000}):
                runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT state, exit_reason, remaining_pct, tp1_done, realized_r FROM paper_positions").fetchone()
            self.assertEqual(pos[0], "PAPER_TRIMMED")
            self.assertEqual(pos[1], "tp1")
            self.assertEqual(pos[2], 50)
            self.assertEqual(pos[3], 1)
            self.assertGreater(pos[4], 0)

    def test_trailing_stop_books_observed_mark_not_trail_level(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            # Tick 1: +80% takes TP1 at its +75% level and arms the trail (high 0.0018).
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.0018, "market_cap": 324_000, "liquidity_usd": 70_000}):
                runner.monitor_positions_once(con)
                con.commit()
            # Tick 2: price back at entry. Trail level would be 0.00117 (+17%), but a
            # polled system books the observed 0.0% mark on the remaining half:
            # TP1 net 36.125 -> 1.445R; exit gross 0 minus fees (0.5 + 0.5) -> -0.04R.
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 70_000}):
                runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT state, exit_reason, realized_r FROM paper_positions").fetchone()
                fill = con.execute("SELECT payload_json FROM paper_fills WHERE side='exit' AND reason='trailing_stop'").fetchone()
            self.assertEqual(pos[0], "PAPER_CLOSED")
            self.assertEqual(pos[1], "trailing_stop")
            self.assertAlmostEqual(pos[2], 1.405)
            self.assertAlmostEqual(json.loads(fill[0])["return_pct"], 0.0)

    def test_monitor_closes_liquidity_break(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 40_000}):
                runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT state, exit_reason FROM paper_positions").fetchone()
            self.assertEqual(pos[0], "PAPER_CLOSED")
            self.assertEqual(pos[1], "liquidity_break")

    def test_monitor_can_take_tp1_and_tp2_same_fast_tick(self):
        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            self._open_position(runner)
            with runner.connect() as con, patch.object(runner, "fetch_market", return_value={"price_usd": 0.0032, "market_cap": 576_000, "liquidity_usd": 90_000}):
                runner.monitor_positions_once(con)
                con.commit()
                pos = con.execute("SELECT state, remaining_pct, tp1_done, tp2_done, realized_r FROM paper_positions").fetchone()
                events = [r[0] for r in con.execute("SELECT message FROM events WHERE event_type='paper_trim' ORDER BY id").fetchall()]
                fills = con.execute("""
                    SELECT side, reason, original_fraction, gross_proceeds_usd, gross_realized_pnl_usd, remaining_fraction, payload_json
                    FROM paper_fills
                    WHERE side='trim'
                    ORDER BY timestamp_utc, fill_id
                """).fetchall()
            self.assertEqual(pos[0], "PAPER_TRIMMED")
            self.assertAlmostEqual(pos[1], 37.5)
            self.assertEqual(pos[2], 1)
            self.assertEqual(pos[3], 1)
            # TPs fill at their limit levels (tp1 +75%, tp2 +200%), not the observed +220% spike,
            # and realized R is net of fees: 36.125/25 + 24.5/25 = 1.445 + 0.98
            self.assertAlmostEqual(pos[4], 2.425)
            self.assertEqual(events[-2:], ["tp1", "tp2"])
            self.assertEqual([(row[0], row[1]) for row in fills], [("trim", "tp1"), ("trim", "tp2")])
            self.assertAlmostEqual(fills[0][2], 0.5)
            self.assertAlmostEqual(fills[1][2], 0.125)
            self.assertAlmostEqual(fills[0][3], 87.5)
            self.assertAlmostEqual(fills[1][3], 37.5)
            self.assertAlmostEqual(sum(row[4] for row in fills), 62.5)
            self.assertAlmostEqual(fills[1][5], 0.375)
            payloads = [json.loads(row[6]) for row in fills]
            fees = sum((p["allocated_entry_fee_usd"] or 0) + (p["exit_fee_usd"] or 0) for p in payloads)
            net = sum(p["net_realized_pnl_usd"] for p in payloads)
            self.assertAlmostEqual(fees, 1.875)
            self.assertAlmostEqual(net, 60.625)


class PaperAutopilotErrorSurfaceTests(unittest.TestCase):
    """A tick whose analyses raised must say so: in the discovery event, on the card, and in the exit code.

    sweep_payload, an in-package step, is stubbed because the autopilot calls it with dex=True, which reads
    DexScreener; the stub hands discovery one fixed candidate, and the tests check what happens after it."""

    def test_discovery_event_records_tape_errors(self):
        def unreadable_tape(limit, **kw):
            payload = sweep_payload_fixture(limit)
            payload["ok"] = False
            payload["errors"] = ["smart_wallets.sqlite missing or unreadable"]
            return payload

        with tempfile.TemporaryDirectory() as td:
            runner = PaperAutopilotRunner(config_for(Path(td)))
            with patch("chaos_paper_autopilot.sweep_payload", side_effect=unreadable_tape), runner.connect() as con:
                runner.discover(con, limit=1)
                con.commit()
                payload = json.loads(con.execute("SELECT payload_json FROM events WHERE event_type='discovery'").fetchone()[0])
            self.assertEqual(payload["tape_errors"], ["smart_wallets.sqlite missing or unreadable"])

    def run_main(self, root: Path, analyze: dict) -> tuple[int, str]:
        smart_db = root / "smart.sqlite"
        seed_wallet_event_db(smart_db, run_id="elite-proof", observed_at=datetime.now(timezone.utc) - timedelta(minutes=5), wallets=2)
        cfg = config_for(root)
        cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
        cfg.raw["boundary"].update(no_swap_links=True, no_live_alerts=True)  # main() loads through the validator
        config_path = root / "paper_autopilot.yaml"
        config_path.write_text(json.dumps(cfg.raw), encoding="utf-8")
        argv = ["chaos_paper_autopilot.py", "--config", str(config_path), "--once", "--limit", "1", "--analyze-top", "1"]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), \
             patch("chaos_paper_autopilot.sweep_payload", side_effect=lambda limit, **kw: sweep_payload_fixture(limit)), \
             patch.object(PaperAutopilotRunner, "run_analyze", **analyze), \
             patch.object(PaperAutopilotRunner, "fetch_market", return_value={"price_usd": 0.001, "market_cap": 180_000, "liquidity_usd": 60_000}), \
             redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            chaos_paper_autopilot.main()
        return raised.exception.code, out.getvalue()

    def test_analysis_error_is_counted_on_the_card_and_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            code, card = self.run_main(Path(td), {"side_effect": RuntimeError("analysis child died")})
            con = sqlite3.connect(Path(td) / "paper.sqlite")
            try:
                errors = con.execute("SELECT message FROM events WHERE event_type='deep_analyze_error'").fetchall()
            finally:
                con.close()
        self.assertEqual(code, 1)
        self.assertIn("Decisions: 1", card.splitlines())
        self.assertIn("Errors: 1", card.splitlines())
        self.assertEqual(errors, [("analysis child died",)])

    def test_clean_tick_prints_zero_errors_and_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            code, card = self.run_main(Path(td), {"return_value": source_gap_payload_fixture()})
        self.assertEqual(code, 0)
        self.assertIn("Decisions: 1", card.splitlines())
        self.assertIn("Errors: 0", card.splitlines())


if __name__ == "__main__":
    unittest.main()
