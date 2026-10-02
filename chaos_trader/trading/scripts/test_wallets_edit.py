#!/usr/bin/env python3
"""`chaos wallets --add/--remove` against a temp CHAOS_HOME holding a copy of the package seed roster.

Each test runs chaos_cmd.py as a subprocess and reads the roster file it leaves behind.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from elite_wallet_pipeline import load_roster

SCRIPT_DIR = Path(__file__).resolve().parent
SEED = SCRIPT_DIR.parent.parent / "seed" / "roster.json"
NEW = "EditAddwa11et1111111111111111111111111111111"
ADDED = f"added {NEW} as tier B; run chaos run chaos_alpha_elite_ingest to read it"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WalletsEditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.roster = self.home / "trading" / "config" / "roster.json"
        self.roster.parent.mkdir(parents=True)
        shutil.copyfile(SEED, self.roster)
        self.seed = json.loads(SEED.read_text(encoding="utf-8"))
        self.env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME")}
        self.env.update(CHAOS_HOME=str(self.home), PYTHONIOENCODING="utf-8")

    def wallets(self, *extra: str, code: int = 0) -> subprocess.CompletedProcess:
        p = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "chaos_cmd.py"), "wallets", *extra],
            capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120, check=False,
        )
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return p

    def refused(self, *extra: str, code: int) -> str:
        before = self.roster.read_bytes()
        p = self.wallets(*extra, code=code)
        self.assertEqual(p.stdout, "")
        self.assertEqual(self.roster.read_bytes(), before)
        return p.stderr.strip()

    def test_add_appends_the_wallet_and_keeps_the_file_shape(self):
        self.assertEqual(self.wallets("--add", NEW, "--tier", "B").stdout.strip(), ADDED)
        roster = json.loads(self.roster.read_text(encoding="utf-8"))
        self.assertEqual(list(roster), ["version", "captured_at", "source", "wallets"])
        self.assertEqual((roster["version"], roster["captured_at"], roster["source"]),
                         (self.seed["version"], self.seed["captured_at"], "user-edited"))
        self.assertEqual(roster["wallets"], [*self.seed["wallets"], {"address": NEW, "tier": "B"}])
        self.assertTrue(self.roster.read_text(encoding="utf-8").endswith("}\n"))
        # The ingest's strict loader reads the edited file.
        loaded = load_roster(self.roster)
        self.assertEqual(loaded["wallet_count"], len(self.seed["wallets"]) + 1)
        self.assertEqual(loaded["records"][-1]["tier"], "B")

    def test_remove_drops_only_that_wallet(self):
        gone = self.seed["wallets"][1]["address"]
        self.assertEqual(self.wallets("--remove", gone).stdout.strip(), f"removed {gone} from the roster")
        roster = json.loads(self.roster.read_text(encoding="utf-8"))
        self.assertEqual(roster["source"], "user-edited")
        self.assertEqual(roster["wallets"], [w for w in self.seed["wallets"] if w["address"] != gone])

    def test_a_duplicate_address_is_refused(self):
        first = self.seed["wallets"][0]["address"]
        self.assertEqual(self.refused("--add", first, "--tier", "C", code=1), f"{first} is already in the roster.")

    def test_an_unknown_address_cannot_be_removed(self):
        self.assertEqual(self.refused("--remove", NEW, code=1), f"{NEW} is not in the roster.")

    def test_a_malformed_address_is_refused(self):
        self.assertEqual(self.refused("--add", "not-an-address", "--tier", "A", code=1),
                         "not-an-address is not a Solana address (base58, 32 to 44 characters).")

    def test_add_without_a_tier_prints_the_usage_and_exits_2(self):
        err = self.refused("--add", NEW, code=2)
        self.assertTrue(err.startswith("usage: "), err)
        self.assertTrue(err.endswith("error: --add needs --tier A, B, or C"), err)
        self.assertIn("--tier goes with --add", self.refused("--remove", NEW, "--tier", "A", code=2))

    def test_add_and_remove_do_not_combine_with_review_or_discover(self):
        self.assertIn("not allowed with argument", self.refused("--add", NEW, "--tier", "A", "--review", code=2))
        self.assertIn("not allowed with argument", self.refused("--remove", NEW, "--discover", "3", code=2))
        self.assertIn("not allowed with argument", self.refused("--add", NEW, "--tier", "A", "--remove", NEW, code=2))

    def test_the_last_wallet_cannot_be_removed(self):
        self.roster.write_text(json.dumps({"version": "test-v1", "wallets": [{"address": NEW, "tier": "A"}]}), encoding="utf-8")
        self.assertIn("is the last wallet in the roster", self.refused("--remove", NEW, code=1))

    def test_the_seed_and_the_rest_of_the_home_are_untouched(self):
        seed_hash = sha256(SEED)
        self.wallets("--add", NEW, "--tier", "B")
        self.wallets("--remove", NEW)
        self.assertEqual(sha256(SEED), seed_hash)
        self.assertEqual(sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*")),
                         ["trading", "trading/config", "trading/config/roster.json"])
        self.assertEqual(json.loads(self.roster.read_text(encoding="utf-8"))["wallets"], self.seed["wallets"])

    def test_a_home_without_a_roster_names_onboard(self):
        self.roster.unlink()
        p = self.wallets("--add", NEW, "--tier", "A", code=1)
        self.assertEqual(p.stderr.strip(), f"No roster at {self.roster}. Run chaos onboard first.")
        self.assertFalse(self.roster.exists())


if __name__ == "__main__":
    unittest.main()
