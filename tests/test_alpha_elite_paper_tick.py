from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from chaos_trader.onboard import onboard

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "chaos_trader" / "trading" / "scripts"
if str(SCRIPTS / "fixtures") not in sys.path:
    sys.path.insert(0, str(SCRIPTS / "fixtures"))
from damaged_sqlite import damage_table, junk_with_header  # noqa: E402

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
        """Run one cycle and keep a spy on load_roster in self.load_roster.

        The cohort's default roster comes from elite_wallet_pipeline, which reads CHAOS_HOME once, at import.
        An earlier import in this process may have used another home, so the module is imported fresh under
        this one; patch.dict puts sys.modules back afterwards."""
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        out = io.StringIO()
        with patch.dict(os.environ, {"CHAOS_HOME": str(self.home)}, clear=False), patch.dict(sys.modules):
            for name in ("chaos_home", "elite_wallet_pipeline"):
                sys.modules.pop(name, None)
            pipeline = importlib.import_module("elite_wallet_pipeline")
            with patch.object(pipeline, "load_roster", wraps=pipeline.load_roster) as self.load_roster, redirect_stdout(out):
                code = wrapper.run_cycle_once(self.home)
        return code, out.getvalue()

    def test_fresh_home_prints_no_ingest_and_creates_nothing(self) -> None:
        self.assertFalse((self.db_dir / "smart_wallets.sqlite").exists())
        code, out = self.run_cycle()
        self.assertEqual(0, code)
        self.assertEqual(NO_INGEST, out.strip())
        self.load_roster.assert_not_called()
        self.assertFalse((self.db_dir / "alpha_elite_paper.sqlite").exists())
        self.assertFalse((self.home / "trading" / "reports" / "alpha_elite_paper").exists())

    def test_corrupt_evidence_database_exits_1_with_one_line(self) -> None:
        evidence = self.db_dir / "smart_wallets.sqlite"

        def damaged_events() -> None:
            with closing(sqlite3.connect(evidence)) as con:
                con.executescript((self.db_dir / "smart_wallets_schema.sql").read_text(encoding="utf-8"))
            damage_table(evidence, "wallet_token_events")

        # Plain junk, junk behind a valid header, and an intact first page over a damaged events table,
        # which opens on every platform and fails only when the cycle reads buys.
        shapes = {
            "junk": lambda: evidence.write_bytes(bytes(range(256)) * 24),
            "junk behind a header": lambda: junk_with_header(evidence),
            "damaged events table": damaged_events,
        }
        for shape, make in shapes.items():
            with self.subTest(shape=shape):
                evidence.unlink(missing_ok=True)
                make()
                with self.assertRaises(SystemExit) as raised:
                    self.run_cycle()
                self.assertEqual(
                    f"{evidence} is not a readable SQLite database. Move it aside and run the ingest again.",
                    raised.exception.code,
                )
                self.assertFalse((self.home / "trading" / "reports" / "alpha_elite_paper").exists())

    def test_cycle_on_a_schema_built_database_writes_a_receipt(self) -> None:
        with closing(sqlite3.connect(self.db_dir / "smart_wallets.sqlite")) as con:
            con.executescript((self.db_dir / "smart_wallets_schema.sql").read_text(encoding="utf-8"))
        code, out = self.run_cycle()
        self.assertEqual(0, code, out)
        # The home roster onboard wrote, through the cohort's own default; no second roster constant.
        self.load_roster.assert_called_once_with(self.home / "trading" / "config" / "roster.json")
        self.assertTrue(out.startswith("☄️ ALPHA ELITE PAPER · CYCLE"), out)
        self.assertIn("Observed events 0 · opened 0 · rejected 0", out)
        receipts = list((self.home / "trading" / "reports" / "alpha_elite_paper").glob("elite_paper_*.json"))
        self.assertEqual(1, len(receipts))
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual("cycle", receipt["command"])
        self.assertTrue(receipt["payload"]["ok"])


if __name__ == "__main__":
    unittest.main()
