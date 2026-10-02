import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from chaos_trader.cli import NEEDS_TERMINAL, main

NUDGE = "The public RPC does not serve the largest-holder read at all; a Helius or other provider key is needed to check the largest holders for watch wallets."


class CliTests(unittest.TestCase):
    def run_cli(self, *args, home: Path, extra_env: dict | None = None):
        env = dict(os.environ, CHAOS_HOME=str(home))
        env.update(extra_env or {})
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], capture_output=True, text=True, encoding="utf-8", env=env)

    def test_onboard_then_help_runs_out_of_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            p = self.run_cli("onboard", "--rpc-url", "https://api.mainnet-beta.solana.com", "--yes", home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn(str(home), p.stdout)
            p = self.run_cli("help", home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("sweep", p.stdout)

    def test_verb_before_onboard_tells_you_to_onboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.run_cli("token", "So11111111111111111111111111111111111111112", home=Path(tmp) / "nope")
            self.assertEqual(p.returncode, 2)
            self.assertIn(f"No chaos-trader home at {(Path(tmp) / 'nope').resolve()}. Run `chaos onboard` first", p.stderr)

    def test_onboard_yes_with_key_only_keeps_helius_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            p = self.run_cli("onboard", "--yes", "--helius-key", "K", home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            env_text = (home / ".env").read_text(encoding="utf-8")
            self.assertIn("HELIUS_API_KEY=K", env_text)
            self.assertNotIn("SOLANA_RPC_URL", env_text)

    def test_interactive_onboard_with_key_and_blank_url_keeps_helius_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            env = dict(os.environ, CHAOS_HOME=str(home))
            p = subprocess.run([sys.executable, "-m", "chaos_trader.cli", "onboard"], input="K\n\n",
                               capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            env_text = (home / ".env").read_text(encoding="utf-8")
            self.assertIn("HELIUS_API_KEY=K", env_text)
            self.assertNotIn("SOLANA_RPC_URL", env_text)

    def run_onboard_closed_stdin(self, *args, home: Path):
        env = dict(os.environ, CHAOS_HOME=str(home))
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", "onboard", *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_onboard_on_a_set_up_home_refuses_before_prompting(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            self.assertEqual(self.run_cli("onboard", "--yes", home=home).returncode, 0)
            p = self.run_onboard_closed_stdin(home=home)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn(f"{home.resolve()} is already set up.", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertNotIn("Helius API key", p.stdout)

    def test_onboard_with_closed_stdin_and_no_yes_names_the_flag_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            p = self.run_onboard_closed_stdin(home=home)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("chaos onboard needs a terminal for its two questions; pass --yes to use the defaults.", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertFalse(home.exists())

    def test_onboard_with_fd_0_closed_and_no_yes_names_the_flag_and_creates_nothing(self):
        # A closed fd 0 (`chaos onboard 0<&-`) starts Python with sys.stdin set to None; set that state here.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            out, err = io.StringIO(), io.StringIO()
            saved = sys.stdin
            sys.stdin = None
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["onboard", "--home", str(home)])
            finally:
                sys.stdin = saved
            self.assertEqual(code, 2, out.getvalue() + err.getvalue())
            self.assertEqual(err.getvalue(), NEEDS_TERMINAL + "\n")
            self.assertNotIn("Helius API key", out.getvalue())
            self.assertFalse(home.exists())

    def test_onboard_yes_with_closed_stdin_sets_up_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            p = self.run_onboard_closed_stdin("--yes", home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue((home / "trading" / "config" / "roster.json").exists())

    def test_onboard_yes_without_rpc_url_writes_the_default_and_names_the_home_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            p = self.run_cli("onboard", "--yes", home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("chosen by CHAOS_HOME", p.stdout)
            self.assertIn(str(home.resolve()), p.stdout)
            env_text = (home / ".env").read_text(encoding="utf-8")
            self.assertIn("SOLANA_RPC_URL=https://api.mainnet-beta.solana.com", env_text)
            if os.name != "nt":
                self.assertEqual((home / ".env").stat().st_mode & 0o777, 0o600)

    def test_onboard_nudges_toward_a_key_only_on_the_public_rpc(self):
        cases = (((), True), (("--rpc-url", "https://example.invalid/"), False), (("--helius-key", "K"), False))
        for args, nudged in cases:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                p = self.run_cli("onboard", "--yes", *args, home=Path(tmp) / "h")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(NUDGE in p.stdout.splitlines(), nudged, p.stdout)

    def test_token_on_the_public_rpc_exits_0_with_a_live_cached_or_unavailable_holder_read(self):
        # Real network. A live read prints no HOLDERS line; a cached or unavailable one prints exactly one.
        with tempfile.TemporaryDirectory() as tmp:
            home, user = Path(tmp) / "h", Path(tmp) / "user"
            user.mkdir()
            drop = ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME", "XAI_API_KEY", "X_SEARCH_PROVIDER", "HERMES_AGENT_SRC")
            env = {k: v for k, v in os.environ.items() if k not in drop}
            env.update(CHAOS_HOME=str(home), PYTHONIOENCODING="utf-8", HOME=str(user), USERPROFILE=str(user))
            cli = [sys.executable, "-m", "chaos_trader.cli"]
            p = subprocess.run([*cli, "onboard", "--yes"], capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            p = subprocess.run([*cli, "token", "So11111111111111111111111111111111111111112", "--no-x"],
                               capture_output=True, text=True, encoding="utf-8", env=env, timeout=480)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            holders = [line for line in p.stdout.splitlines() if line.startswith("HOLDERS:")]
            self.assertLessEqual(len(holders), 1, p.stdout)
            for line in holders:
                self.assertRegex(line, r"^HOLDERS: (cached \d+m|unavailable \((rate limited|not served by this RPC|rpc error)\))$")

    def test_onboard_home_flag_is_named_as_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "flagged"
            p = self.run_cli("onboard", "--home", str(home), "--yes", home=Path(tmp) / "unused")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("chosen by --home", p.stdout)
            self.assertTrue((home / "trading" / "config" / "roster.json").exists())

    def test_update_on_a_home_with_no_install_refuses_and_names_onboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "empty"
            home.mkdir()
            p = self.run_cli("update", "--home", str(home), home=home)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(f"{home.resolve()} is not set up. Run `chaos onboard` first.", p.stdout + p.stderr)
            self.assertFalse((home / "trading").exists())

    def test_update_prints_the_pip_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "h"
            p = self.run_cli("onboard", "--yes", home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            p = self.run_cli("update", "--home", str(home), home=home)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(p.stdout.splitlines(), [f"defaults refreshed in {home.resolve()}", "code is updated with: pip install -U chaos-trader"])

    def test_version(self):
        p = subprocess.run([sys.executable, "-m", "chaos_trader.cli", "--version"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertRegex(p.stdout.strip(), r"^chaos-trader \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
