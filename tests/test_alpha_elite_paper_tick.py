from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from chaos_trader.onboard import onboard

ROOT = Path(__file__).resolve().parents[1]
NO_INGEST = "No ingest yet. Run chaos run chaos_alpha_elite_ingest first."
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


class RealCycleTests(unittest.TestCase):
    """run_cycle_once against the real cohort on an onboarded home. An empty evidence database needs no network."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = onboard(Path(tmp.name) / "home", rpc_url=None, helius_key=None)
        self.db_dir = self.home / "trading" / "db"

    def run_cycle(self) -> tuple[int, str]:
        out = io.StringIO()
        with patch.dict(os.environ, {"CHAOS_HOME": str(self.home)}, clear=False), redirect_stdout(out):
            code = wrapper.run_cycle_once(self.home)
        return code, out.getvalue()

    def test_fresh_home_prints_no_ingest_and_creates_nothing(self) -> None:
        self.assertFalse((self.db_dir / "smart_wallets.sqlite").exists())
        code, out = self.run_cycle()
        self.assertEqual(0, code)
        self.assertEqual(NO_INGEST, out.strip())
        self.assertFalse((self.db_dir / "alpha_elite_paper.sqlite").exists())
        self.assertFalse((self.home / "trading" / "reports" / "alpha_elite_paper").exists())

    def test_cycle_on_a_schema_built_database_writes_a_receipt(self) -> None:
        with closing(sqlite3.connect(self.db_dir / "smart_wallets.sqlite")) as con:
            con.executescript((self.db_dir / "smart_wallets_schema.sql").read_text(encoding="utf-8"))
        code, out = self.run_cycle()
        self.assertEqual(0, code, out)
        self.assertTrue(out.startswith("☄️ ALPHA ELITE PAPER · CYCLE"), out)
        self.assertIn("Observed events 0 · opened 0 · rejected 0", out)
        receipts = list((self.home / "trading" / "reports" / "alpha_elite_paper").glob("elite_paper_*.json"))
        self.assertEqual(1, len(receipts))
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual("cycle", receipt["command"])
        self.assertTrue(receipt["payload"]["ok"])


if __name__ == "__main__":
    unittest.main()
