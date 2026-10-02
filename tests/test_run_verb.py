import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"


class RunVerbTests(unittest.TestCase):
    """`chaos run <script>` runs package code by file name. Real subprocesses, real files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.env = {k: v for k, v in os.environ.items() if k not in ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME")}
        self.env.update(CHAOS_HOME=str(self.home), PYTHONIOENCODING="utf-8")

    def run_chaos(self, *args):
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120)

    def listing(self, p) -> dict[str, list[str]]:
        """The names `chaos run` prints, keyed by the header they sit under."""
        lines = p.stdout.splitlines()
        self.assertEqual(lines[:2], ["chaos run <script> [args]", "available:"], p.stdout + p.stderr)
        groups: dict[str, list[str]] = {}
        current = None
        for line in lines[2:]:
            if line.endswith(":") and not line.startswith(" "):
                current = line[:-1]
                groups[current] = []
            else:
                groups[current].append(line.strip())
        return groups

    def assert_full_listing(self, p):
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        groups = self.listing(p)
        self.assertEqual(list(groups), ["pipeline", "jobs", "skills"])
        self.assertIn("chaos_cmd", groups["pipeline"])
        self.assertIn("chaos_paper_autopilot_tick", groups["jobs"])
        self.assertIn("solana_client", groups["skills"])
        for names in groups.values():
            self.assertEqual(names, sorted(names))
            self.assertFalse([n for n in names if n.startswith("test_") or n == "__init__"], names)

    def test_run_alone_lists_every_script_by_source(self):
        self.assert_full_listing(self.run_chaos("run"))

    def test_unknown_name_prints_the_same_listing(self):
        self.assert_full_listing(self.run_chaos("run", "nope"))

    def onboard(self):
        p = self.run_chaos("onboard", "--rpc-url", PUBLIC_RPC, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_script_help_runs_and_exits_zero(self):
        self.onboard()
        p = self.run_chaos("run", "strategy_paper_engine", "--help")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("json_file", p.stdout)

    def test_a_relative_path_resolves_from_the_callers_working_directory(self):
        self.onboard()
        schema = (ROOT / "chaos_trader" / "trading" / "schemas" / "smart_wallets_schema.sql").read_text(encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.home / "trading" / "db" / "smart_wallets.sqlite")) as con:
            con.executescript(schema)
        with tempfile.TemporaryDirectory() as cwd:
            shutil.copyfile(ROOT / "chaos_trader" / "seed" / "roster.json", Path(cwd) / "r.json")
            p = subprocess.run([sys.executable, "-m", "chaos_trader.cli", "run", "elite_wallet_pipeline", "status", "--roster", "r.json"],
                               cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("No roster found", p.stdout + p.stderr)

    def test_wallet_deep_without_a_key_fails_and_names_the_key(self):
        self.onboard()
        p = self.run_chaos("run", "wallet_deep", WSOL)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("HELIUS_API_KEY", p.stdout + p.stderr)

    def test_test_modules_are_not_runnable(self):
        self.onboard()
        p = self.run_chaos("run", "test_chaos_cmd")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("available:", p.stdout)
        self.assertNotIn("Ran ", p.stdout + p.stderr)

    def test_a_script_on_a_home_that_is_not_set_up_names_onboard_without_a_traceback(self):
        self.assertFalse(self.home.exists())
        p = self.run_chaos("run", "chaos_paper_learning_tick")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("chaos onboard", p.stderr)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        self.assertFalse(self.home.exists())

    def test_the_listing_needs_no_home(self):
        self.assertFalse(self.home.exists())
        self.assert_full_listing(self.run_chaos("run"))
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
