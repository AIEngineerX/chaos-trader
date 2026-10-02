"""`chaos run chaos_outcome_tick`: one locked pass of the outcome tracker on the home, a stamp on success.

Real subprocesses on an onboarded home. The failing case is a real network failure: HTTPS_PROXY points at a
local port nothing listens on, so every DexScreener request the tracker makes is refused.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chaos_trader.onboard import onboard

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "chaos_trader" / "trading" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from outcomes_report import TICK_STAMP  # noqa: E402
from signal_ledger import record_signal  # noqa: E402

SPEC = importlib.util.spec_from_file_location("chaos_outcome_tick", ROOT / "chaos_trader" / "jobs" / "chaos_outcome_tick.py")
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


def closed_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OutcomeTickTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = onboard(Path(tmp.name) / "home", rpc_url=None, helius_key=None)
        drop = ("CHAOS_PROFILE_HOME", "HERMES_HOME", "NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy")
        self.env = {k: v for k, v in os.environ.items() if k not in drop}
        self.env.update(CHAOS_HOME=str(self.home), PYTHONIOENCODING="utf-8")
        self.stamp = self.home / TICK_STAMP
        self.lock = self.home / "trading" / "db" / "outcome_tick.lock"

    def tick(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", "run", "chaos_outcome_tick"], capture_output=True,
                              text=True, encoding="utf-8", env=self.env, timeout=300, check=False)

    def assert_lock_released(self):
        with self.lock.open("a+") as handle:
            self.assertTrue(wrapper.acquire_lock(handle))

    def test_fresh_home_has_nothing_due_and_stamps_the_run(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        p = self.tick()
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertIn("0 due", p.stdout)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        stamped = datetime.fromisoformat(self.stamp.read_text(encoding="utf-8").strip())
        self.assertEqual(timedelta(0), stamped.utcoffset())
        self.assertGreaterEqual(stamped, before)
        self.assert_lock_released()

    def test_a_failing_tracker_exits_2_without_a_stamp_and_releases_the_lock(self):
        due = datetime.now(timezone.utc) - timedelta(minutes=15, seconds=20)
        db = self.home / "trading" / "db" / "signal_ledger.sqlite"
        record_signal({"generated_at": due.isoformat(), "mint": "9" * 32,
                       "market": {"symbol": "DUE", "price_usd": 1.0, "liquidity_usd": 10_000},
                       "classification": {"verdict": "watch", "flow": {}}}, source_command="token", db_path=db)
        self.env["HTTPS_PROXY"] = f"http://127.0.0.1:{closed_port()}"
        p = self.tick()
        self.assertEqual(2, p.returncode, p.stdout + p.stderr)
        self.assertIn("DexScreener did not answer for 1 read", p.stdout)
        self.assertFalse(self.stamp.exists())
        self.assert_lock_released()
        with closing(sqlite3.connect(db)) as con:
            status, eligible = con.execute("SELECT outcome_status, primary_eligible FROM outcomes WHERE window_label='15m'").fetchone()
        self.assertEqual(("missing", 0), (status, eligible))

    def test_a_tick_skips_while_another_holds_the_lock(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a+") as held:
            self.assertTrue(wrapper.acquire_lock(held))
            p = self.tick()
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertIn("skipped; prior tick active", p.stdout)
        self.assertFalse(self.stamp.exists())


if __name__ == "__main__":
    unittest.main()
