"""Readiness exercises real persisted files and the installed CLI path, never fake DBs."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "chaos_trader/trading/scripts"
sys.path.insert(0, str(SCRIPTS))
import operational_readiness as readiness
import elite_paper_cohort
import chaos_paper_autopilot


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.now = datetime.now(timezone.utc)
        self.at = self.now.isoformat()
        self.guard = self.root / "guard"
        self.guard.mkdir()
        # Python audit hooks guard the external network/filesystem boundary in both
        # real CLI processes. SQLite's C-level writes are checked by file snapshots.
        (self.guard / "sitecustomize.py").write_text('''import os, sys
sys.dont_write_bytecode = True
def guard(event, args):
    if event.startswith("socket."):
        raise RuntimeError("network forbidden in readiness")
    if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod", "os.truncate"}:
        raise RuntimeError("filesystem mutation forbidden in readiness")
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        raise RuntimeError("write forbidden in readiness")
sys.addaudithook(guard)
''', encoding="utf-8")

    def write(self, relative, content):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mtime_ns,
                os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                for p in self.root.rglob("*")}

    def cli(self, *args):
        before = self.snapshot()
        env = dict(os.environ, CHAOS_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=os.pathsep.join([str(self.guard), str(ROOT)]))
        result = subprocess.run([sys.executable, "-B", "-W", "error::ResourceWarning", "-m", "chaos_trader.cli", "readiness", *args],
                                env=env, cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(before, self.snapshot(), "readiness changed persisted files")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn(str(self.root), result.stdout)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["command"], "readiness")
        self.assertEqual(envelope["schema_version"], "1")
        return envelope["data"]

    def ingest(self, **updates):
        data = dict(schema="chaos.alpha_elite_ingest.v1", completed_at_utc=self.at,
                    attempted=2, succeeded=2, failed=0, events_inserted=0)
        data.update(updates)
        self.write("trading/reports/elite_wallets/elite_ingest_20260101T000000Z.json", json.dumps(data))

    def config(self):
        return self.write("trading/config/paper_autopilot.yaml", (ROOT / "chaos_trader/trading/config/paper_autopilot.yaml").read_text())

    def db(self, filename, schema):
        path = self.home / "trading/db" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(schema)
        con.commit()
        self.addCleanup(con.close)
        return con

    def cycle(self, *, at=None, ok=True, opened=0, marks=0, command="cycle", **extra):
        con = self.db("alpha_elite_paper.sqlite", elite_paper_cohort.SCHEMA)
        receipt = dict(schema="chaos.alpha_elite_paper.v1", command=command,
                       completed_at_utc=at or self.at,
                       payload=dict(ok=ok, observations=dict(opened=opened), marking=dict(marks_written=marks)), **extra)
        con.execute("INSERT OR REPLACE INTO cycle_receipts VALUES (?,?,?,?,?)", ("cycle-1", at or self.at, at or self.at, command, json.dumps(receipt)))
        con.commit()
        con.close()

    def test_missing_home_does_not_onboard(self):
        data = self.cli()
        self.assertFalse(self.home.exists())
        self.assertEqual(data["producer"]["elite_wallet_ingest"]["state"], "never_produced")
        self.assertEqual(data["consumers"]["autopilot_book"]["state"], "unconfigured")
        self.assertEqual(data["consumers"]["elite_paper_book"]["state"], "never_produced")

    def test_idle_ingest_is_fresh_despite_old_trade_time_and_private_metadata(self):
        self.ingest(metrics={"latest_event_utc": "2000-01-01T00:00:00Z"},
                    roster={"source_path": str(self.root), "version": "operator@example.invalid"},
                    wallet_results=[{"error": "private-token-sentinel"}])
        data = self.cli()
        producer = data["producer"]["elite_wallet_ingest"]
        self.assertEqual(producer["state"], "healthy_idle")
        self.assertEqual(producer["timestamp_basis"], "ingest_receipt.completed_at_utc")
        self.assertNotIn("private-token-sentinel", json.dumps(data))
        self.assertNotIn("operator@example.invalid", json.dumps(data))

    def test_ingest_activity_failure_staleness_and_no_attempt(self):
        for updates, expected in [({"events_inserted": 1}, "healthy"),
                                  ({"failed": 1, "succeeded": 1}, "failed_refresh"),
                                  ({"completed_at_utc": (self.now - timedelta(days=1)).isoformat()}, "stale"),
                                  ({"attempted": 0, "succeeded": 0}, "unsupported")]:
            with self.subTest(expected=expected):
                self.ingest(**updates)
                self.assertEqual(self.cli()["producer"]["elite_wallet_ingest"]["state"], expected)

    def test_malformed_receipts_never_fall_back_to_old_success(self):
        self.ingest()
        for content in ["{", "[]", "null", json.dumps({"schema": "unknown"})]:
            with self.subTest(content=content):
                self.write("trading/reports/elite_wallets/elite_ingest_99999999T000000Z.json", content)
                self.assertIn(self.cli()["producer"]["elite_wallet_ingest"]["state"], ("invalid_data", "unsupported"))

    def test_bad_counts_and_timestamps(self):
        for updates in [{"succeeded": True}, {"failed": -1}, {"attempted": 8},
                        {"completed_at_utc": "2026-01-01"}, {"completed_at_utc": "invalid"},
                        {"completed_at_utc": (self.now + timedelta(days=1)).isoformat()}]:
            with self.subTest(updates=updates):
                self.ingest(**updates)
                self.assertEqual(self.cli()["producer"]["elite_wallet_ingest"]["state"], "invalid_data")

    def test_freshness_boundary_and_failed_attempt_age(self):
        self.ingest(completed_at_utc=(self.now - timedelta(seconds=3600)).isoformat())
        data = readiness.report(self.home, now=self.now)
        self.assertEqual(data["producer"]["elite_wallet_ingest"]["state"], "healthy_idle")
        self.ingest(completed_at_utc=(self.now - timedelta(seconds=3601)).isoformat(), succeeded=1, failed=1)
        data = readiness.report(self.home, now=self.now)
        self.assertEqual(data["producer"]["elite_wallet_ingest"]["state"], "failed_refresh")
        self.assertFalse(data["producer"]["elite_wallet_ingest"]["fresh"])

    def test_config_present_but_database_absent(self):
        self.config()
        self.assertEqual(self.cli()["consumers"]["autopilot_book"]["state"], "never_produced")
        self.assertFalse((self.home / "trading/db").exists())

    def test_config_malformed_or_invalid(self):
        for text in ["[", "[]", "mode: live", "loop: []"]:
            with self.subTest(text=text):
                self.write("trading/config/paper_autopilot.yaml", text)
                self.assertIn(self.cli()["consumers"]["autopilot_book"]["state"], ("unconfigured", "invalid_data"))

    def test_autopilot_persisted_linkage_is_not_complete_tick_health(self):
        self.config()
        con = self.db("paper_autopilot.sqlite", chaos_paper_autopilot.SCHEMA)
        con.execute("INSERT INTO paper_positions(id,mint,state,opened_at_utc,decision_json,policy_version,updated_at_utc) VALUES(?,?,?,?,?,?,?)",
                    ("p1", "fixture-mint", "PAPER_OPEN", self.at, '{"decision":"enter"}', "policy-1", self.at))
        con.execute("INSERT INTO events(timestamp_utc,event_type,position_id) VALUES(?,?,?)", (self.at, "paper_position_open", "p1"))
        con.execute("INSERT INTO events(timestamp_utc,event_type,message) VALUES(?,?,?)", (self.at, "position_monitor_error", "operator@example.invalid private-token-sentinel"))
        con.commit()
        con.close()
        data = self.cli()["consumers"]["autopilot_book"]
        self.assertEqual(data["state"], "unsupported")
        self.assertEqual(data["latest_error"]["state"], "failed_refresh")
        self.assertEqual(data["linkage"]["positions_with_open_event_link"], 1)
        self.assertEqual(data["linkage"]["positions_with_decision_snapshot"], 1)
        self.assertEqual(data["linkage"]["positions_with_policy_version"], 1)
        self.assertEqual(data["linkage"]["config_version_link"], "not_persisted")
        self.assertNotIn("private-token-sentinel", json.dumps(data))

    def test_absent_schema_and_corrupt_database(self):
        self.config()
        for filename, key in [("paper_autopilot.sqlite", "autopilot_book"), ("alpha_elite_paper.sqlite", "elite_paper_book")]:
            con = self.db(filename, "CREATE TABLE unrelated(x)")
            con.close()
            self.assertEqual(self.cli()["consumers"][key]["state"], "unsupported")
            (self.home / "trading/db" / filename).write_bytes(b"not a sqlite database")
            self.assertEqual(self.cli()["consumers"][key]["state"], "invalid_data")

    def test_elite_complete_cycle_states(self):
        for options, state in [({}, "healthy_idle"), ({"opened": 1}, "healthy"),
                               ({"marks": 1}, "healthy"), ({"ok": False}, "failed_refresh"),
                               ({"at": (self.now - timedelta(days=1)).isoformat()}, "stale")]:
            with self.subTest(state=state):
                self.cycle(**options)
                data = self.cli()["consumers"]["elite_paper_book"]
                self.assertEqual(data["state"], state)
                self.assertEqual(data["timestamp_basis"], "cycle_receipts.completed_at_utc")

    def test_observe_only_is_not_a_complete_cycle(self):
        self.cycle(command="observe")
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "never_produced")

    def test_elite_invalid_cycle_payload(self):
        self.cycle(ok="yes")
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "invalid_data")

    def test_active_sqlite_journals_are_not_ignored_or_modified(self):
        self.cycle()
        for suffix in ("-wal", "-journal"):
            sidecar = self.write("trading/db/alpha_elite_paper.sqlite" + suffix, "fixture-active-journal")
            self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "unsupported")
            sidecar.unlink()

    def test_real_wal_uncheckpointed_commit_is_not_read_as_old_healthy(self):
        self.cycle()
        path = self.home / "trading/db/alpha_elite_paper.sqlite"
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("UPDATE cycle_receipts SET payload_json='invalid uncheckpointed receipt'")
        con.commit()
        self.assertTrue(Path(str(path) + "-wal").exists())
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "unsupported")
        con.close()

    def test_elite_stored_episode_links_and_malformed_linkage(self):
        self.cycle()
        con = sqlite3.connect(self.home / "trading/db/alpha_elite_paper.sqlite")
        self.addCleanup(con.close)
        con.execute("""INSERT INTO episodes(episode_id,mint,state,gate_reason,source_event_id,source_wallet,
                    source_event_at_utc,observed_at_utc,baseline_age_seconds,source_event_ids_json,
                    source_wallet_count,roster_version,roster_sha256,policy_version,raw_baseline_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("e1", "fixture-mint", "OPEN", "fixture", 7, "fixture-wallet", self.at,
                     self.at, 0, "[7,8]", 1, "roster-1", "fixture-hash", "policy-1", "{}"))
        con.commit()
        data = self.cli()["consumers"]["elite_paper_book"]
        self.assertEqual(data["linkage"]["episodes_with_source_event_ids"], 1)
        self.assertEqual(data["linkage"]["episodes_with_policy_and_roster_version"], 1)
        con.execute("UPDATE episodes SET source_event_ids_json='not-json'")
        con.commit()
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "invalid_data")
        con.close()

    def test_config_outside_home_is_not_opened(self):
        import yaml
        path = self.config()
        config = yaml.safe_load(path.read_text())
        config["paths"]["sqlite"] = str(self.root / "not-a-book")
        path.write_text(yaml.safe_dump(config))
        data = self.cli()["consumers"]["autopilot_book"]
        self.assertEqual(data["reason"], "database_outside_selected_home")

    def test_unknown_user_database_path_is_private_structured_error(self):
        import yaml
        path = self.config()
        config = yaml.safe_load(path.read_text())
        config["paths"]["sqlite"] = "~readiness-private-sentinel-no-such-user/book.sqlite"
        self.assertTrue(readiness.validate(config)["ok"])
        path.write_text(yaml.safe_dump(config))
        self.ingest()
        self.cycle()
        data = self.cli("--json")
        self.assertEqual(data["consumers"]["autopilot_book"],
                         {"state": "invalid_data", "reason": "database_path_unexpandable"})
        self.assertNotIn("readiness-private-sentinel", json.dumps(data))
        self.assertEqual(data["producer"]["elite_wallet_ingest"]["state"], "healthy_idle")
        self.assertEqual(data["consumers"]["elite_paper_book"]["state"], "healthy_idle")

    def test_elite_self_symlink_is_private_structured_error(self):
        self.config()
        self.ingest()
        path = self.home / "trading/db/alpha_elite_paper.sqlite"
        path.parent.mkdir(parents=True)
        path.symlink_to(path.name)
        data = self.cli("--json")
        self.assertEqual(data["consumers"]["elite_paper_book"],
                         {"state": "unreadable", "reason": "database_path_unresolvable"})
        self.assertEqual(data["producer"]["elite_wallet_ingest"]["state"], "healthy_idle")
        self.assertEqual(data["consumers"]["autopilot_book"]["state"], "never_produced")

    def test_autopilot_self_symlink_is_private_structured_error(self):
        self.config()
        self.ingest()
        self.cycle()
        path = self.home / "trading/db/paper_autopilot.sqlite"
        path.symlink_to(path.name)
        data = self.cli("--json")
        self.assertEqual(data["consumers"]["autopilot_book"],
                         {"state": "unreadable", "reason": "database_path_unresolvable"})
        self.assertEqual(data["producer"]["elite_wallet_ingest"]["state"], "healthy_idle")
        self.assertEqual(data["consumers"]["elite_paper_book"]["state"], "healthy_idle")

    def test_guarded_does_not_swallow_unrelated_runtime_errors(self):
        def broken_reader():
            raise RuntimeError("programming bug")
        with self.assertRaisesRegex(RuntimeError, "programming bug"):
            readiness.guarded(broken_reader)

    def test_real_receipt_writer_is_compatible(self):
        con = self.db("alpha_elite_paper.sqlite", elite_paper_cohort.SCHEMA)
        elite_paper_cohort.save_receipt(con, self.home / "trading/reports/alpha_elite_paper", "cycle", self.now,
                                      {"ok": True, "observations": {"opened": 0}, "marking": {"marks_written": 0}})
        con.close()
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "healthy_idle")

    def test_wal_behind_database_symlink_is_not_ignored(self):
        self.cycle()
        path = self.home / "trading/db/alpha_elite_paper.sqlite"
        target = path.with_name("actual.sqlite")
        path.rename(target)
        path.symlink_to(target.name)
        con = sqlite3.connect(target)
        self.addCleanup(con.close)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("UPDATE cycle_receipts SET payload_json='uncheckpointed'")
        con.commit()
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "unsupported")
        con.close()

    def test_nonboolean_config_flag_cannot_leak_private_values(self):
        import yaml
        path = self.config()
        config = yaml.safe_load(path.read_text())
        config["loop"]["enabled"] = "operator@example.invalid private-token-sentinel"
        path.write_text(yaml.safe_dump(config))
        data = self.cli()["consumers"]["autopilot_book"]
        self.assertEqual(data["state"], "invalid_data")
        self.assertNotIn("private-token-sentinel", json.dumps(data))

    def test_unreadable_receipt_is_structured(self):
        path = self.home / "trading/reports/elite_wallets/elite_ingest_99999999.json"
        path.mkdir(parents=True)
        self.assertEqual(self.cli()["producer"]["elite_wallet_ingest"]["state"], "unreadable")

    def test_latest_malformed_elite_cycle_does_not_reuse_healthy_receipt(self):
        self.cycle()
        con = sqlite3.connect(self.home / "trading/db/alpha_elite_paper.sqlite")
        self.addCleanup(con.close)
        con.execute("INSERT INTO cycle_receipts VALUES(?,?,?,?,?)", ("cycle-2", self.at, "invalid", "cycle", "{"))
        con.commit()
        con.close()
        self.assertEqual(self.cli()["consumers"]["elite_paper_book"]["state"], "invalid_data")

    def test_json_flag_and_freshness_override(self):
        self.ingest(completed_at_utc=(self.now - timedelta(seconds=100)).isoformat())
        result = self.cli("--json", "--max-age-seconds", "60")
        self.assertEqual(result["max_age_seconds"], 60)
        self.assertEqual(result["producer"]["elite_wallet_ingest"]["state"], "stale")

    def test_invalid_age_argument_is_usage_error(self):
        result = subprocess.run([sys.executable, "-B", "-m", "chaos_trader.cli", "readiness", "--max-age-seconds", "0"],
                                env=dict(os.environ, CHAOS_HOME=str(self.home)), cwd=ROOT,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
