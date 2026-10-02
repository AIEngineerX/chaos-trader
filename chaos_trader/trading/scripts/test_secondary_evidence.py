import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SecondaryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CHAOS_HOME": str(self.home)})
        self.env.start()
        import importlib
        import secondary_evidence
        importlib.reload(secondary_evidence)
        self.mod = secondary_evidence

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_paths_live_under_secondary_dir(self):
        self.assertEqual(self.mod.SUMMARY_PATH.resolve(), (self.home / "trading" / "alpha" / "secondary" / "mint_score_summary.json").resolve())
        self.assertEqual(self.mod.MINT_SCORE_DIR.resolve(), (self.home / "trading" / "alpha" / "mint_scores").resolve())

    def test_absent_mint_is_reported_without_vendor_words(self):
        out = self.mod.compact_secondary_evidence("So11111111111111111111111111111111111111112")
        self.assertFalse(out["present"])
        self.assertEqual(out["verdict"], "no-secondary-evidence")
        self.assertEqual(out["source"], "local_secondary_export")
        self.assertNotIn("ocu" + "la", json.dumps(out).lower())

    def test_present_mint_from_summary(self):
        self.mod.SUMMARY_PATH.parent.mkdir(parents=True)
        self.mod.SUMMARY_PATH.write_text(json.dumps({"rows": [{"mint": "M1", "verdict": "deep-check", "score": 71.5, "symbol": "X", "metrics": {"tracked_buyer_count": 4}}]}))
        out = self.mod.compact_secondary_evidence("M1")
        self.assertTrue(out["present"])
        self.assertEqual(out["verdict"], "deep-check")
        self.assertEqual(out["metrics"]["tracked_buyer_count"], 4)


if __name__ == "__main__":
    unittest.main()
