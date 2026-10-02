from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from wallet_live_paper_hunter import (
    score_mints,
    seed_wallets,
    wallet_candidates_for_analysis,
    wallet_weight,
)


class LegacyWalletPaperHunterTests(unittest.TestCase):
    def test_external_signal_paper_sources_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "retired"):
            seed_wallets(2, source="external-signal-elite")
        with self.assertRaisesRegex(ValueError, "retired"):
            seed_wallets(2, source="external-signal-live")

    def test_legacy_weight_uses_strength_and_contamination_only(self) -> None:
        seed = {"strength_score": 75, "insider_score": 50, "transfer_contamination": 0.1}
        weight = wallet_weight({"WalletA": seed}, "WalletA")
        self.assertGreater(weight, 2.0)
        contaminated = dict(seed, transfer_contamination=0.95)
        self.assertLess(wallet_weight({"WalletA": contaminated}, "WalletA"), weight)

    def test_transfer_only_activity_never_creates_candidate(self) -> None:
        seed_by = {"WalletA": {"strength_score": 50, "transfer_contamination": 0.1}}
        events = [{"wallet": "WalletA", "mint": "MintA", "event_type": "token_transfer_in"}]
        self.assertEqual([], score_mints(events, seed_by))

    def test_decoded_buy_can_create_legacy_paper_candidate(self) -> None:
        seed_by = {"WalletA": {"strength_score": 50, "transfer_contamination": 0.1}}
        events = [{"wallet": "WalletA", "mint": "MintA", "event_type": "buy"}]
        scored = score_mints(events, seed_by)
        self.assertEqual(1, len(scored))
        self.assertGreater(scored[0]["score"], 1.0)

    def test_wallet_analysis_selection_does_not_consume_global_backlog(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("""CREATE TABLE candidates(
            mint TEXT PRIMARY KEY, state TEXT, last_deep_analyze_utc TEXT, x_checked INTEGER
        )""")
        con.executemany(
            "INSERT INTO candidates VALUES(?,?,?,?)",
            [("backlog", "DISCOVERED", None, 0), ("walletA", "DISCOVERED", None, 0), ("walletB", "AVOIDED", None, 0)],
        )
        rows = wallet_candidates_for_analysis(
            con,
            [{"mint": "walletA"}, {"mint": "walletB"}],
            limit=5,
            cooldown_min=10,
            with_x=False,
        )
        self.assertEqual(["walletA"], [row["mint"] for row in rows])
        con.close()


if __name__ == "__main__":
    unittest.main()
