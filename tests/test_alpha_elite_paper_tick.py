from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "chaos_trader" / "jobs" / "chaos_alpha_elite_paper_tick.py"
SPEC = importlib.util.spec_from_file_location("chaos_alpha_elite_paper_tick", SCRIPT)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


class AlphaElitePaperTickTests(unittest.TestCase):
    def test_profile_comes_from_chaos_home(self) -> None:
        with patch.dict(os.environ, {"CHAOS_HOME": "/tmp/chaos-paper-profile"}, clear=False):
            self.assertEqual(Path("/tmp/chaos-paper-profile").resolve(), wrapper.profile_home())

    def test_consumer_path_targets_the_package_consumer(self) -> None:
        path = wrapper.consumer_path()
        self.assertEqual(ROOT / "chaos_trader" / "trading" / "scripts" / "elite_paper_cohort.py", path)
        self.assertTrue(path.is_file())

    def test_main_runs_one_in_process_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"CHAOS_HOME": td}, clear=False
        ), patch.object(wrapper, "run_cycle_once", return_value=0) as run:
            self.assertEqual(0, wrapper.main())
            run.assert_called_once_with(Path(td))

    def test_cycle_uses_fail_closed_roster_defaults_without_duplicate_constants(self) -> None:
        class Connection:
            def close(self) -> None:
                pass

        consumer = SimpleNamespace(
            DEFAULT_ROSTER=Path("/tmp/roster.json"),
            now_utc=Mock(return_value="started"),
            connect=Mock(return_value=Connection()),
            load_roster=Mock(return_value={"version": "elite-v3"}),
            connect_evidence=Mock(return_value=Connection()),
            run_cycle=Mock(return_value={"ok": True}),
            save_receipt=Mock(return_value="/tmp/receipt.json"),
            compact=Mock(return_value="ok"),
        )

        class Loader:
            def exec_module(self, module: object) -> None:
                pass

        spec = SimpleNamespace(loader=Loader())
        with patch.object(wrapper.importlib.util, "spec_from_file_location", return_value=spec), patch.object(
            wrapper.importlib.util, "module_from_spec", return_value=consumer
        ), patch("builtins.print"):
            self.assertEqual(0, wrapper.run_cycle_once(Path("/tmp/profile")))

        consumer.load_roster.assert_called_once_with(consumer.DEFAULT_ROSTER)


if __name__ == "__main__":
    unittest.main()
