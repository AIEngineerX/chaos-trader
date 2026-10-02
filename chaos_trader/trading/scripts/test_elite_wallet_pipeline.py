from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import elite_wallet_pipeline as elite
import smart_wallet_tracker as tracker


class EliteWalletPipelineTests(unittest.TestCase):
    def roster_file(self, root: str, *, version: str = "elite-test-v1") -> Path:
        path = Path(root) / "roster.json"
        path.write_text(json.dumps({
            "version": version,
            "data_through": "2026-07-30",
            "wallets": [
                {"address": "A" * 31 + "B", "tier": "A"},
                {"address": "A" * 31 + "C", "tier": "B"},
            ],
        }), encoding="utf-8")
        return path

    def test_roster_derives_version_and_hash_from_the_loaded_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self.roster_file(td)
            roster = elite.load_roster(path)
            self.assertEqual(2, roster["wallet_count"])
            self.assertEqual("A", roster["records"][0]["tier"])
            self.assertEqual("2026-07-30", roster["data_through"])
            self.assertEqual("elite-test-v1", roster["version"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), roster["source_sha256"])
            other = self.roster_file(td, version="elite-test-v2")
            self.assertEqual("elite-test-v2", elite.load_roster(other)["version"])

    def test_roster_hash_follows_the_file_when_addresses_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self.roster_file(td)
            before = elite.load_roster(path)["source_sha256"]
            payload = json.loads(path.read_text())
            payload["wallets"][1]["address"] = "A" * 31 + "D"
            path.write_text(json.dumps(payload), encoding="utf-8")
            after = elite.load_roster(path)
            self.assertNotEqual(before, after["source_sha256"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), after["source_sha256"])

    def test_roster_rejects_duplicates_and_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self.roster_file(td)
            payload = json.loads(path.read_text())
            payload["wallets"][1]["address"] = payload["wallets"][0]["address"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                elite.load_roster(path)
            payload["wallets"][1]["address"] = "not-base58!"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid address"):
                elite.load_roster(path)
            path.write_text(json.dumps({"version": "x"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no wallets"):
                elite.load_roster(path)

    def test_roster_rejects_unknown_tier(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = self.roster_file(td)
            payload = json.loads(path.read_text())
            payload["wallets"][0]["tier"] = "S"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tier must be one of A, B, C"):
                elite.load_roster(path)

    def test_missing_roster_names_chaos_onboard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                elite.load_roster(Path(td) / "trading" / "config" / "roster.json")
            self.assertIn("Run `chaos onboard` to create the home with the seed roster, or pass `--roster <path>`.", str(cm.exception))
            self.assertNotIn("chaos update", str(cm.exception))

    def test_roster_defaults_missing_version_and_tier(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "roster.json"
            path.write_text(json.dumps({"wallets": [{"address": "A" * 31 + "B"}]}), encoding="utf-8")
            roster = elite.load_roster(path)
            self.assertEqual("unpinned", roster["version"])
            self.assertEqual("C", roster["records"][0]["tier"])

    def test_raw_transaction_is_preserved_and_prior_full_evidence_is_not_rewritten(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE transactions(
            signature TEXT PRIMARY KEY,block_time_utc TEXT,fee_lamports INTEGER,
            err_json TEXT,source_id TEXT,raw_json TEXT
        )""")
        tx = {"signature": "SigA", "blockTime": 1785370000, "meta": {"fee": 5000}, "payload": {"proof": 1}}
        self.assertTrue(elite.preserve_transaction(con, tx, source_id="solana_rpc"))
        stored = json.loads(con.execute("SELECT raw_json FROM transactions WHERE signature='SigA'").fetchone()[0])
        self.assertEqual({"proof": 1}, stored["payload"])
        self.assertEqual("solana_rpc", con.execute("SELECT source_id FROM transactions WHERE signature='SigA'").fetchone()[0])
        changed = {**tx, "payload": {"proof": 999}}
        self.assertFalse(elite.preserve_transaction(con, changed, source_id="helius_rpc"))
        stored_again = json.loads(con.execute("SELECT raw_json FROM transactions WHERE signature='SigA'").fetchone()[0])
        self.assertEqual({"proof": 1}, stored_again["payload"])
        con.close()

    def test_event_insert_is_idempotent_across_restart(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE wallet_token_events(
            wallet TEXT,mint TEXT,signature TEXT,event_type TEXT,source_id TEXT,run_id TEXT
        )""")
        event = {"wallet": "WalletA", "mint": "MintA", "signature": "SigA", "event_type": "buy"}

        def insert_func(db: sqlite3.Connection, run_id: str, row: dict) -> None:
            db.execute(
                "INSERT INTO wallet_token_events VALUES(?,?,?,?,?,?)",
                (row["wallet"], row["mint"], row["signature"], row["event_type"], "helius_rpc", run_id),
            )

        self.assertTrue(elite.insert_event_if_new(con, "run-1", event, insert_func=insert_func))
        self.assertFalse(elite.insert_event_if_new(con, "run-2", event, insert_func=insert_func))
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM wallet_token_events").fetchone()[0])
        con.close()

    def test_elite_lineage_can_recover_event_first_seen_by_legacy_shadow_run(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE wallet_token_events(
            wallet TEXT,mint TEXT,signature TEXT,event_type TEXT,source_id TEXT,run_id TEXT
        )""")
        event = {"wallet": "WalletA", "mint": "MintA", "signature": "SigA", "event_type": "buy"}

        def insert_func(db: sqlite3.Connection, run_id: str, row: dict) -> None:
            db.execute(
                "INSERT INTO wallet_token_events VALUES(?,?,?,?,?,?)",
                (row["wallet"], row["mint"], row["signature"], row["event_type"], "helius_rpc", run_id),
            )

        self.assertTrue(elite.insert_event_if_new(con, "shadow-legacy", event, insert_func=insert_func))
        self.assertTrue(elite.insert_event_if_new(con, "elite-recovery-1", event, insert_func=insert_func))
        self.assertFalse(elite.insert_event_if_new(con, "elite-recovery-2", event, insert_func=insert_func))
        self.assertEqual(
            ["elite-recovery-1", "shadow-legacy"],
            [row[0] for row in con.execute("SELECT run_id FROM wallet_token_events ORDER BY run_id")],
        )
        con.close()

    def test_event_seen_via_one_rpc_is_not_reinserted_via_the_other(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("""CREATE TABLE wallet_token_events(
            wallet TEXT,mint TEXT,signature TEXT,event_type TEXT,source_id TEXT,run_id TEXT
        )""")
        event = {"wallet": "WalletA", "mint": "MintA", "signature": "SigA", "event_type": "buy"}

        def insert_as(source_id: str):
            def insert_func(db: sqlite3.Connection, run_id: str, row: dict) -> None:
                db.execute(
                    "INSERT INTO wallet_token_events VALUES(?,?,?,?,?,?)",
                    (row["wallet"], row["mint"], row["signature"], row["event_type"], source_id, run_id),
                )
            return insert_func

        self.assertTrue(elite.insert_event_if_new(con, "elite-standard", event, insert_func=insert_as("solana_rpc")))
        self.assertFalse(elite.insert_event_if_new(con, "elite-helius", event, insert_func=insert_as("helius_rpc")))
        self.assertEqual(1, con.execute("SELECT COUNT(*) FROM wallet_token_events").fetchone()[0])
        con.close()

    def test_partial_wallet_failure_is_visible_and_does_not_abort_successes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            roster_path = self.roster_file(td)
            roster = elite.load_roster(roster_path)

            def fake_ingest(_con, wallet, **_kwargs):
                if wallet.endswith("C"):
                    return {"wallet": wallet, "ok": False, "error": "rpc unavailable"}
                return {"wallet": wallet, "ok": True, "txs_seen": 3, "events_seen": 2, "events_inserted": 1, "duplicate_events": 1}

            result = elite.run_ingest(
                roster,
                db_path=Path(td) / "smart.sqlite",
                report_dir=Path(td) / "reports",
                ingest_func=fake_ingest,
                metrics_func=lambda _con, _wallets, top: {"events": 1, "distinct_mints": 1, "latest_event_utc": "2026-07-30T00:00:00+00:00", "event_types": {"buy": 1}, "quote_events": 0, "top_mints": []},
                ensure_func=lambda _con: None,
            )
            self.assertEqual((2, 1, 1), (result["attempted"], result["succeeded"], result["failed"]))
            self.assertEqual(1, result["events_inserted"])
            self.assertTrue(Path(result["receipt_path"]).exists())

    def test_helius_system_exit_is_a_failed_wallet_receipt_not_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            roster_path = self.roster_file(td)
            roster = elite.load_roster(roster_path)
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute(
                """CREATE TABLE ingestion_runs(
                run_id TEXT PRIMARY KEY,source_id TEXT,source_path TEXT,source_commit TEXT,
                started_at TEXT,completed_at TEXT,status TEXT,notes TEXT,row_counts_json TEXT
                )"""
            )
            try:
                with mock.patch.object(elite, "upsert_wallet"), mock.patch.object(
                    tracker, "is_helius_endpoint", return_value=True
                ), mock.patch.object(
                    tracker, "rpc_request", side_effect=SystemExit("rpc unavailable")
                ):
                    result = elite.ingest_one_wallet(
                        con,
                        roster["wallets"][0],
                        cycle_id="test-cycle",
                        roster=roster,
                        history_limit=50,
                        pages=1,
                    )
                self.assertFalse(result["ok"])
                self.assertIn("Helius transaction fetch failed", result["error"])
                run = con.execute("SELECT status,row_counts_json,source_id FROM ingestion_runs WHERE run_id=?", (result["run_id"],)).fetchone()
                self.assertEqual("failed", run["status"])
                self.assertIn("rpc unavailable", run["row_counts_json"])
                self.assertEqual("helius_rpc", run["source_id"])
            finally:
                con.close()

    def test_standard_rpc_failure_is_a_failed_wallet_receipt_under_solana_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            roster_path = self.roster_file(td)
            roster = elite.load_roster(roster_path)
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute(
                """CREATE TABLE ingestion_runs(
                run_id TEXT PRIMARY KEY,source_id TEXT,source_path TEXT,source_commit TEXT,
                started_at TEXT,completed_at TEXT,status TEXT,notes TEXT,row_counts_json TEXT
                )"""
            )
            try:
                with mock.patch.object(elite, "upsert_wallet"), mock.patch.object(
                    tracker, "is_helius_endpoint", return_value=False
                ), mock.patch.object(
                    tracker, "rpc_request", side_effect=SystemExit("429 rate limited")
                ):
                    result = elite.ingest_one_wallet(
                        con,
                        roster["wallets"][0],
                        cycle_id="test-cycle",
                        roster=roster,
                        history_limit=5,
                        pages=1,
                    )
                self.assertFalse(result["ok"])
                self.assertIn("RPC transaction fetch failed", result["error"])
                run = con.execute("SELECT status,row_counts_json,source_id FROM ingestion_runs WHERE run_id=?", (result["run_id"],)).fetchone()
                self.assertEqual("failed", run["status"])
                self.assertIn("429 rate limited", run["row_counts_json"])
                self.assertEqual("solana_rpc", run["source_id"])
            finally:
                con.close()

    def test_metrics_keep_quote_activity_raw_but_out_of_rankings(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE tokens(mint TEXT PRIMARY KEY,symbol TEXT)")
        con.execute("""CREATE TABLE wallet_token_events(
            wallet TEXT,mint TEXT,signature TEXT,block_time_utc TEXT,event_type TEXT,source_id TEXT
        )""")
        wallet = "A" * 31 + "B"
        usdt = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
        mint = "B" * 32
        con.executemany("INSERT INTO tokens VALUES(?,?)", [(usdt, "USDT"), (mint, "MEME")])
        con.executemany(
            "INSERT INTO wallet_token_events VALUES(?,?,?,?,?,?)",
            [
                (wallet, usdt, "s1", "2026-07-30T00:00:00+00:00", "buy", "helius_rpc"),
                (wallet, mint, "s2", "2026-07-30T00:01:00+00:00", "buy", "helius_rpc"),
                (wallet, mint, "s3", "2026-07-30T00:02:00+00:00", "token_transfer_in", "helius_rpc"),
            ],
        )
        metrics = elite.cohort_metrics(con, [wallet])
        self.assertEqual(3, metrics["events"])
        self.assertEqual(1, metrics["quote_events"])
        self.assertEqual([mint], [row["mint"] for row in metrics["top_mints"]])
        self.assertEqual(1, metrics["top_mints"][0]["transfer_in"])
        con.close()

    def test_pipeline_has_no_paper_signal_dex_or_x_dependencies(self) -> None:
        text = (SCRIPTS / "elite_wallet_pipeline.py").read_text(encoding="utf-8")
        for forbidden in ("chaos_paper_autopilot", "paper_autopilot.sqlite", "signal_ledger.sqlite", "dexscreener_client", "x_search"):
            self.assertNotIn(forbidden, text)

    def test_runtime_cli_cannot_override_reviewed_roster_expectations(self) -> None:
        for script in ("elite_wallet_pipeline.py", "elite_paper_cohort.py"):
            text = (SCRIPTS / script).read_text(encoding="utf-8")
            self.assertNotIn("--expected-version", text)
            self.assertNotIn("--expected-count", text)
            self.assertNotIn("--expected-sha256", text)


if __name__ == "__main__":
    unittest.main()
