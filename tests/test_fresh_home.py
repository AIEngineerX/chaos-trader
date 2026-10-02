import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_no_vendor_names import banned_kinds  # noqa: E402

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

# Every verb chaos_cmd.py accepts, plus the two that cli.py handles itself.
VERBS = ("sweep", "token", "analyze", "strategy-paper", "smart-signals", "paper-report", "wallets", "outcomes", "help", "onboard", "update", "skills", "run", "--version")

PAPER_REPORT = 'No paper activity yet. Run the ingest job and the paper tick first (see "Running it on a schedule" in the README).'
SWEEP_FAST = "The roster tape is empty until the ingest job has run. Run the ingest job, or use chaos sweep without --fast for the trending sweep."
WALLETS_DISCOVER = 'Wallet discovery needs transfer edges from the Helius wallet API. With a Helius RPC and HELIUS_API_KEY set, run: chaos run smart_wallet_tracker <wallet address> — then try again.'
PAPER_RETIRED = "chaos paper was retired; use chaos paper-report for the paper book and chaos strategy-paper <mint> for one mint."


class FreshHomeTests(unittest.TestCase):
    """A stranger onboards on the public RPC and runs the README commands. Real subprocesses, real files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.env = {k: v for k, v in os.environ.items() if k not in ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME")}
        self.env.update(CHAOS_HOME=str(self.home), SOLANA_RPC_URL=PUBLIC_RPC, PYTHONIOENCODING="utf-8")
        p = self.run_chaos("onboard", "--rpc-url", PUBLIC_RPC, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def run_chaos(self, *args):
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120)

    def zero_byte_files(self):
        return [p.name for p in (self.home / "trading" / "db").rglob("*") if p.is_file() and p.stat().st_size == 0]

    def assert_ok_with(self, p, sentence):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(sentence, p.stdout)
        self.assertNotIn("Traceback", p.stdout + p.stderr)

    def test_paper_report_is_an_empty_report_not_a_crash(self):
        self.assert_ok_with(self.run_chaos("paper-report"), PAPER_REPORT)
        self.assertEqual(self.zero_byte_files(), [])
        self.assertFalse((self.home / "trading" / "db" / "paper_autopilot.sqlite").exists())

    def test_paper_verb_is_retired_with_a_pointer(self):
        p = self.run_chaos("paper")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(PAPER_RETIRED, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        self.assertEqual(self.zero_byte_files(), [])

    def test_sweep_fast_says_the_tape_is_empty(self):
        self.assert_ok_with(self.run_chaos("sweep", "--fast"), SWEEP_FAST)
        self.assertEqual(self.zero_byte_files(), [])

    def test_wallets_discover_says_why_it_does_nothing(self):
        self.assert_ok_with(self.run_chaos("wallets", "--discover", "3"), WALLETS_DISCOVER)
        self.assertEqual(self.zero_byte_files(), [])

    def test_help_lists_every_verb_and_no_chat_bot_wording(self):
        p = self.run_chaos("help")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for verb in VERBS:
            self.assertRegex(p.stdout, r"(?m)^- " + verb + r"\b", verb)
        self.assertEqual(banned_kinds(p.stdout), set())

    def test_outcomes_is_an_empty_card_with_no_rates(self):
        p = self.run_chaos("outcomes")
        self.assert_ok_with(p, "NO SCORES YET")
        self.assertNotIn("%", p.stdout)
        self.assertIn("WATCH · 0 of 20 reads, not scored yet", p.stdout)
        self.assertIn("Outcome tick has not run", p.stdout)
        self.assertNotIn("edge", p.stdout.lower())
        self.assertFalse((self.home / "trading" / "db" / "signal_ledger.sqlite").exists())
        self.assertEqual(self.zero_byte_files(), [])

    def test_outcomes_json_label_and_floor(self):
        p = self.run_chaos("outcomes", "--json", "--window", "1h")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        envelope = json.loads(p.stdout)
        self.assertEqual(("1", "outcomes"), (envelope["schema_version"], envelope["command"]))
        self.assertEqual(("1h", 20, None), (envelope["data"]["window"], envelope["data"]["min_n"], envelope["data"]["tick_last_run"]))
        self.assertEqual("below_n", envelope["data"]["labels"]["avoid-entry"]["status"])
        p = self.run_chaos("outcomes", "watch")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("WATCH · 0 of 20 reads, not scored yet", p.stdout)
        self.assertNotIn("STUDY", p.stdout)
        p = self.run_chaos("outcomes", "--min-n", "5")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--min-n cannot go below 20", p.stderr)

    def test_no_zero_byte_database_after_the_whole_path(self):
        for args in (("paper-report",), ("sweep", "--fast"), ("wallets", "--discover", "3"), ("wallets",), ("outcomes",)):
            self.run_chaos(*args)
        self.assertEqual(self.zero_byte_files(), [])


if __name__ == "__main__":
    unittest.main()
