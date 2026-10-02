from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "chaos_trader" / "jobs" / "chaos_alpha_elite_paper_cycle.py"
SPEC = importlib.util.spec_from_file_location("chaos_alpha_elite_paper_cycle", SCRIPT)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


class AlphaElitePaperWrapperTests(unittest.TestCase):
    def test_profile_comes_from_chaos_home(self) -> None:
        with patch.dict(os.environ, {"CHAOS_HOME": "/tmp/chaos-test-profile"}, clear=False):
            self.assertEqual(Path("/tmp/chaos-test-profile").resolve(), wrapper.profile_home())

    def test_command_is_bounded_and_targets_the_package_consumer(self) -> None:
        cmd = wrapper.command(limit=5, from_event_id=42, raw=True)
        self.assertEqual(sys.executable, cmd[0])
        self.assertEqual(str(ROOT / "chaos_trader" / "trading" / "scripts" / "elite_paper_cohort.py"), cmd[1])
        self.assertTrue(Path(cmd[1]).is_file())
        self.assertEqual("cycle", cmd[2])
        self.assertIn("--limit", cmd)
        self.assertIn("5", cmd)
        self.assertIn("--from-event-id", cmd)
        self.assertIn("42", cmd)
        self.assertEqual("--raw", cmd[-1])

    def test_burn_in_is_bounded_and_initial_cursor_override_is_used_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            argv = ["wrapper", "--max-cycles", "3", "--interval-seconds", "1", "--from-event-id", "42"]
            with patch.dict(os.environ, {"CHAOS_HOME": td}, clear=False), patch.object(sys, "argv", argv), patch.object(
                wrapper.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run, patch.object(wrapper.time, "sleep") as sleep:
                self.assertEqual(0, wrapper.main())
            self.assertEqual(3, run.call_count)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn("--from-event-id", commands[0])
            self.assertNotIn("--from-event-id", commands[1])
            self.assertNotIn("--from-event-id", commands[2])
            self.assertEqual(2, sleep.call_count)


if __name__ == "__main__":
    unittest.main()
