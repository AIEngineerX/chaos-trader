"""Entry scripts print emoji. On a Windows cp1252 console or pipe that used to crash them."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from chaos_trader.onboard import onboard

# A child that writes non-ASCII JSON and does not import chaos_home, so its stdout encoding
# comes only from the environment its parent hands it.
CHILD = "import json; print(json.dumps({'t': '\\u2604\\ufe0f'}, ensure_ascii=False))"
# chaos_cmd.run_raw runs [python, *args, '--raw'], so '-c CHILD' is a real network-free child.
ROUND_TRIP = f"import chaos_cmd; p = chaos_cmd.run_raw(['-c', {CHILD!r}]); print('ok' if p['t'] == '\\u2604\\ufe0f' else 'mismatch')"


class Utf8StdioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = onboard(Path(self.tmp.name) / "home", rpc_url="https://api.mainnet-beta.solana.com", helius_key=None)
        self.scripts = Path(__file__).resolve().parents[1] / "chaos_trader" / "trading" / "scripts"

    def tearDown(self):
        self.tmp.cleanup()

    def run_in_home(self, args, io_encoding):
        """Run a package script against the home. io_encoding=None leaves PYTHONIOENCODING unset, so a
        Windows parent's pipes use the cp1252 locale codec."""
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["CHAOS_HOME"] = str(self.home)
        if io_encoding:
            env["PYTHONIOENCODING"] = io_encoding
        proc = subprocess.run([sys.executable, *args], cwd=self.scripts, env=env, capture_output=True, timeout=120)
        return proc, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")

    def test_help_runs_under_cp1252_stdio(self):
        proc, out, err = self.run_in_home(["chaos_cmd.py", "help"], "cp1252")
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertIn("sweep", out)

    def test_help_and_fast_token_run_with_pythonioencoding_unset(self):
        proc, out, err = self.run_in_home(["chaos_cmd.py", "help"], None)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertIn("sweep", out)
        # --fast reads only the local cache, in-process: no network and no child.
        proc, out, err = self.run_in_home(["chaos_cmd.py", "token", "So11111111111111111111111111111111111111112", "--fast"], None)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertIn("☄", out)

    def test_child_output_round_trips_through_run_raw_with_pythonioencoding_unset(self):
        proc, out, err = self.run_in_home(["-c", ROUND_TRIP], None)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertEqual(out.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
