from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos_trader.onboard import onboard

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "chaos_trader" / "jobs" / "chaos_paper_autopilot_tick.py"
SPEC = importlib.util.spec_from_file_location("chaos_paper_autopilot_tick", SCRIPT)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


class PaperAutopilotTickTests(unittest.TestCase):
    def test_profile_comes_from_chaos_home(self) -> None:
        with patch.dict(os.environ, {"CHAOS_HOME": "/tmp/chaos-tick-profile"}, clear=False):
            self.assertEqual(Path("/tmp/chaos-tick-profile").resolve(), wrapper.profile_home())

    def test_command_is_bounded_and_targets_the_package_autopilot(self) -> None:
        cmd = wrapper.command(limit=10, analyze_top=2, with_x=False)
        self.assertEqual(sys.executable, cmd[0])
        self.assertEqual(str(ROOT / "chaos_trader" / "trading" / "scripts" / "chaos_paper_autopilot.py"), cmd[1])
        self.assertTrue(Path(cmd[1]).is_file())
        self.assertEqual("--once", cmd[2])
        self.assertIn("--limit", cmd)
        self.assertIn("10", cmd)
        self.assertIn("--analyze-top", cmd)
        self.assertIn("2", cmd)
        self.assertNotIn("--with-x", cmd)
        self.assertNotIn("--max-cycles", cmd)

    def test_with_x_flag_is_forwarded(self) -> None:
        cmd = wrapper.command(limit=5, analyze_top=1, with_x=True)
        self.assertEqual("--with-x", cmd[-1])

    def test_main_runs_one_locked_bounded_pass(self) -> None:
        argv = ["wrapper", "--limit", "99", "--analyze-top", "99"]
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"CHAOS_HOME": td}, clear=False
        ), patch.object(sys, "argv", argv), patch.object(
            wrapper.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="ok", stderr="")
        ) as run:
            self.assertEqual(0, wrapper.main())
        self.assertEqual(1, run.call_count)
        cmd = run.call_args.args[0]
        # Caps hold even when asked for more.
        self.assertIn("--limit", cmd)
        self.assertEqual("50", cmd[cmd.index("--limit") + 1])
        self.assertEqual("10", cmd[cmd.index("--analyze-top") + 1])
        self.assertEqual(str(Path(td)), run.call_args.kwargs["env"]["HERMES_HOME"])

    def test_corrupt_wallet_database_fails_the_real_tick_with_one_line(self) -> None:
        # The real job and the real autopilot child on an onboarded home. The child reads the wallet tape
        # before any market read, so the junk file stops it before the network is touched.
        with tempfile.TemporaryDirectory() as td:
            home = onboard(Path(td) / "home", rpc_url=None, helius_key=None)
            db = home / "trading" / "db" / "smart_wallets.sqlite"
            db.write_bytes(bytes(range(256)) * 24)
            env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME")}
            env.update(CHAOS_HOME=str(home), PYTHONIOENCODING="utf-8")
            p = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8",
                               env=env, timeout=300, check=False)
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        self.assertEqual(f"{db} is not a readable SQLite database. Move it aside and run the ingest again.", p.stdout.strip())
        self.assertNotIn("Traceback", p.stdout + p.stderr)

    def test_second_tick_skips_while_lock_held(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CHAOS_HOME": td}, clear=False):
            lock_path = Path(td) / "trading" / "db" / "paper_autopilot.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as held:
                self.assertTrue(wrapper.acquire_lock(held))
                with lock_path.open("a+") as second:
                    self.assertFalse(wrapper.acquire_lock(second))


if __name__ == "__main__":
    unittest.main()
