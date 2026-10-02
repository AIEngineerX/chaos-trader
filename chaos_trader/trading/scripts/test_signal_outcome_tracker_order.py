#!/usr/bin/env python3
"""One outcome tick on a seeded ledger: the read nearest its 300 s cutoff is marked first, and a mint due twice
costs one set of DexScreener calls.

The real tracker runs end to end through `main`. Replaced: the tracker's clock and per-read sleep, DexScreener's
HTTP call (`dexscreener_client.get_json`), and the DexScreener cache folder (to a temp dir).
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dexscreener_client  # noqa: E402
import signal_outcome_tracker  # noqa: E402
from signal_ledger import record_signal  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
PAIR = {"pairAddress": "Pair1111111111111111111111111111111111111111", "priceUsd": "2.0", "liquidity": {"usd": 50_000},
        "marketCap": 2_000_000, "fdv": 2_000_000, "url": "https://dexscreener.com/solana/pair"}


def listed(path: str):
    return {"schemaVersion": "1.0.0", "pairs": [PAIR]} if path.startswith("/latest/") else [PAIR]


class OutcomeTickOrderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "signal_ledger.sqlite"
        patcher = patch.object(dexscreener_client, "CACHE_DIR", Path(tmp.name) / "dexscreener")
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, mint: str, seconds_past_15m: int) -> None:
        at = NOW - timedelta(minutes=15, seconds=seconds_past_15m)
        record_signal({"generated_at": at.isoformat(), "mint": mint, "market": {"symbol": "T", "price_usd": 1.0, "liquidity_usd": 40_000},
                       "classification": {"verdict": "watch", "flow": {}}}, source_command="token", db_path=self.db)

    def tick(self) -> tuple[dict, list[str]]:
        calls: list[str] = []

        def answer(path: str):
            calls.append(path)
            return listed(path)

        out = io.StringIO()
        argv = ["signal_outcome_tracker.py", "--db", str(self.db), "--raw"]
        with patch.object(sys, "argv", argv), patch("signal_outcome_tracker.now", return_value=NOW), \
             patch("signal_outcome_tracker.time.sleep"), patch("dexscreener_client.get_json", side_effect=answer), redirect_stdout(out):
            signal_outcome_tracker.main()
        return json.loads(out.getvalue()), calls

    def test_the_read_closest_to_its_cutoff_is_marked_first(self):
        lags = {"A" * 32: 200, "B" * 32: 296, "C" * 32: 250, "D" * 32: 30, "E" * 32: 900}
        for mint, lag in lags.items():
            self.seed(mint, lag)
        result, _ = self.tick()
        order = [row["mint"] for row in result["written"] if row["window"] == "15m"]
        self.assertEqual(order, ["B" * 32, "C" * 32, "A" * 32, "D" * 32, "E" * 32])
        late = {row["mint"]: row["status"] for row in result["written"]}
        self.assertEqual(late["E" * 32], "missing_late")
        self.assertTrue(all(late[m] == "observed" for m in ("A" * 32, "B" * 32, "C" * 32, "D" * 32)))

    def test_a_mint_due_twice_in_one_tick_costs_one_set_of_dexscreener_calls(self):
        mint = "F" * 32
        self.seed(mint, 100)
        self.seed(mint, 120)
        result, calls = self.tick()
        self.assertEqual(result["due"], 2)
        self.assertEqual([row["status"] for row in result["written"]], ["observed", "observed"])
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
