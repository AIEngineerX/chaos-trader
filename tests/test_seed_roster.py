import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "chaos_trader" / "seed" / "roster.json"
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class SeedRosterTests(unittest.TestCase):
    def test_shape(self):
        data = json.loads(SEED.read_text(encoding="utf-8"))
        self.assertEqual(data["source"], "public")
        self.assertRegex(data["captured_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(len(data["wallets"]), 9)
        addresses = [row["address"] for row in data["wallets"]]
        self.assertEqual(len(set(addresses)), len(addresses))
        self.assertEqual(sorted(row["tier"] for row in data["wallets"]), ["A"] * 3 + ["B"] * 3 + ["C"] * 3)
        for row in data["wallets"]:
            self.assertEqual(set(row), {"address", "tier"}, row)
            self.assertRegex(row["address"], B58)
            self.assertIn(row["tier"], ("A", "B", "C"))

    def test_no_scoring_text_or_handles(self):
        text = SEED.read_text(encoding="utf-8")
        for word in ("handle", "realized_x", "bags_at_zero", "pnl", "score_wallets", "$"):
            self.assertNotIn(word, text)

    def test_pipeline_loads_seed_and_defaults_missing_tier(self):
        import sys
        sys.path.insert(0, str(ROOT / "chaos_trader" / "trading" / "scripts"))
        import elite_wallet_pipeline as ewp
        roster = ewp.load_roster(SEED)
        self.assertEqual(len(roster["wallets"]), len(json.loads(SEED.read_text())["wallets"]))
        self.assertEqual(roster["version"], "seed-v2")
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"wallets": [{"address": "So11111111111111111111111111111111111111112"}]}, fh)
        self.addCleanup(os.unlink, fh.name)
        roster = ewp.load_roster(Path(fh.name))
        self.assertEqual(roster["records"][0]["tier"], "C")
        self.assertEqual(roster["version"], "unpinned")


if __name__ == "__main__":
    unittest.main()
