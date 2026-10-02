import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
NOT_BASE58 = "0x0000000000000000000000000000000000000000"


class ValidateWalletImportTests(unittest.TestCase):
    """`chaos run validate_wallet_import` checks a bare-array wallet import. Real subprocesses, real files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {k: v for k, v in os.environ.items() if k not in ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME")}
        self.env.update(CHAOS_HOME=str(self.tmp / "home"), PYTHONIOENCODING="utf-8")
        p = self.run_chaos("onboard", "--rpc-url", PUBLIC_RPC, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def run_chaos(self, *args):
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120)

    def validate(self, rows) -> subprocess.CompletedProcess:
        path = self.tmp / "import.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return self.run_chaos("run", "validate_wallet_import", str(path))

    def test_a_clean_import_passes(self):
        p = self.validate([
            {"trackedWalletAddress": WSOL, "name": "first", "emoji": "A", "alertsOn": True},
            {"trackedWalletAddress": SYSTEM_PROGRAM, "name": "second", "emoji": "B", "alertsOn": False},
        ])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        report = json.loads(p.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["errors"], [])

    def test_a_duplicate_and_a_non_base58_address_fail(self):
        p = self.validate([
            {"trackedWalletAddress": WSOL, "name": "first", "emoji": "A", "alertsOn": True},
            {"trackedWalletAddress": WSOL, "name": "second", "emoji": "B", "alertsOn": True},
            {"trackedWalletAddress": NOT_BASE58, "name": "third", "emoji": "C", "alertsOn": True},
        ])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        report = json.loads(p.stdout)
        self.assertFalse(report["ok"])
        self.assertIn(f"duplicate trackedWalletAddress: ['{WSOL}']", report["errors"])
        self.assertIn("row 2: invalid address: non-Base58 character: '0'", report["errors"])
        self.assertEqual(len(report["errors"]), 2, report["errors"])


if __name__ == "__main__":
    unittest.main()
