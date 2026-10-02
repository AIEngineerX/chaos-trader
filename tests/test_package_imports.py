import importlib
import unittest
from pathlib import Path


class PackageImportTests(unittest.TestCase):
    def test_version_is_a_string(self):
        mod = importlib.import_module("chaos_trader")
        self.assertIsInstance(mod.__version__, str)
        self.assertRegex(mod.__version__, r"^\d+\.\d+\.\d+$")

    def test_tree_ships_inside_package(self):
        root = Path(importlib.import_module("chaos_trader").__file__).resolve().parent
        for rel in ("trading/scripts/chaos_cmd.py", "trading/schemas/smart_wallets_schema.sql",
                    "trading/config/paper_autopilot.yaml", "jobs/chaos_paper_autopilot_tick.py", "seed/roster.json"):
            self.assertTrue((root / rel).exists(), rel)
        self.assertFalse((root / "deploy").exists())

    def test_skills_live_at_the_repo_root(self):
        root = Path(importlib.import_module("chaos_trader").__file__).resolve().parents[1]
        self.assertTrue((root / "skills/blockchain/solana/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
