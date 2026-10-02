from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / "chaos_trader" / "jobs" / "chaos_alpha_elite_ingest.py"


def load_job(env: dict[str, str]):
    with patch.dict(os.environ, env, clear=False):
        if "CHAOS_PYTHON" not in env:
            os.environ.pop("CHAOS_PYTHON", None)
        spec = importlib.util.spec_from_file_location("chaos_alpha_elite_ingest_test", JOB)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class AlphaEliteCronWrapperTests(unittest.TestCase):
    def test_python_defaults_to_current_interpreter_and_profile_from_chaos_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            module = load_job({"CHAOS_HOME": td})
            self.assertEqual(Path(td).resolve(), module.PROFILE)
            self.assertEqual(Path(sys.executable), module.PYTHON)
            self.assertFalse(hasattr(module, "OPERATOR_HOME"))
            self.assertEqual([], [name for name in vars(module) if name.endswith("_ROOT")])

    def test_chaos_python_overrides_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            custom = str(Path(td) / "py")
            module = load_job({"CHAOS_HOME": td, "CHAOS_PYTHON": custom})
            self.assertEqual(Path(custom), module.PYTHON)


if __name__ == "__main__":
    unittest.main()
