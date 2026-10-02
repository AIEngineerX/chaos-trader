"""What works and what refuses when no Helius key is configured. Real subprocesses, no stubs."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ADDRESS = "So11111111111111111111111111111111111111112"


def run_script(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME")}
    env.update({"CHAOS_HOME": str(home), "PYTHONIOENCODING": "utf-8"})
    return subprocess.run([sys.executable, *args], cwd=SCRIPT_DIR, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)


class NoHeliusKeyTests(unittest.TestCase):
    def test_wallet_deep_with_only_rpc_url_names_the_key(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".env").write_text("SOLANA_RPC_URL=https://api.mainnet-beta.solana.com\n", encoding="utf-8")
            proc = run_script(home, "wallet_deep.py", ADDRESS)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("HELIUS_API_KEY", proc.stdout + proc.stderr)

    def test_help_runs_with_no_key(self):
        with tempfile.TemporaryDirectory() as td:
            proc = run_script(Path(td), "chaos_cmd.py", "help")
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("sweep", proc.stdout)


if __name__ == "__main__":
    unittest.main()
