"""The four job wrappers that capture a child keep its stderr when it crashes after printing.

Each wrapper runs a real child process: a small script that prints a line, then exits 0 or crashes.
Only the path or command that names the child is swapped, so the wrapper's capture and print run for real.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "chaos_trader" / "jobs"
CRASH = "import sys\nprint('partial card')\nsys.stdout.flush()\nraise RuntimeError('child crashed after output')\n"
CLEAN = "import sys\nprint('full card')\nprint('a warning the card does not need', file=sys.stderr)\n"


def load(name: str, home: Path):
    # The ingest wrapper reads CHAOS_HOME at import, so every wrapper loads under the test home.
    with patch.dict(os.environ, {"CHAOS_HOME": str(home)}, clear=False):
        os.environ.pop("CHAOS_PYTHON", None)
        spec = importlib.util.spec_from_file_location(f"{name}_stderr_test", JOBS / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class JobWrapperStderrTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / "trading" / "db").mkdir(parents=True)
        self.children = Path(tmp.name) / "children"
        self.children.mkdir()

    def child(self, body: str) -> Path:
        # Named chaos_cmd.py so the wrappers that build SCRIPTS / "chaos_cmd.py" find it.
        path = self.children / "chaos_cmd.py"
        path.write_text(body, encoding="utf-8")
        return path

    def run_wrapper(self, name: str, body: str) -> tuple[int, str, str]:
        child = self.child(body)
        module = load(name, self.home)
        if name == "chaos_paper_autopilot_tick":
            swap = patch.object(module, "command", return_value=[sys.executable, str(child)])
        elif name == "chaos_alpha_elite_ingest":
            swap = patch.multiple(module, PIPELINE=child, PYTHON=Path(sys.executable))
        else:
            swap = patch.object(module, "SCRIPTS", self.children)
        # The wrappers reconfigure stdout and stderr, so capture through real text wrappers.
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with swap, patch.dict(os.environ, {"CHAOS_HOME": str(self.home)}, clear=False), \
                patch.object(sys, "argv", [name]), redirect_stdout(out), redirect_stderr(err):
            code = module.main()
        out.flush()
        err.flush()
        return code, out.buffer.getvalue().decode("utf-8"), err.buffer.getvalue().decode("utf-8")

    def test_a_crash_after_partial_output_keeps_its_traceback(self) -> None:
        for name in ("chaos_paper_autopilot_tick", "chaos_alpha_elite_ingest", "chaos_paper_learning_tick", "chaos_wallet_discovery_tick"):
            with self.subTest(wrapper=name):
                code, out, err = self.run_wrapper(name, CRASH)
                self.assertEqual(1, code)
                self.assertEqual("partial card", out.strip())
                self.assertIn("Traceback", err)
                self.assertTrue(err.strip().endswith("RuntimeError: child crashed after output"), err)

    def test_a_clean_exit_does_not_echo_stderr(self) -> None:
        for name in ("chaos_paper_autopilot_tick", "chaos_alpha_elite_ingest", "chaos_paper_learning_tick", "chaos_wallet_discovery_tick"):
            with self.subTest(wrapper=name):
                code, out, err = self.run_wrapper(name, CLEAN)
                self.assertEqual(0, code)
                self.assertEqual("full card", out.strip())
                self.assertEqual("", err)


if __name__ == "__main__":
    unittest.main()
