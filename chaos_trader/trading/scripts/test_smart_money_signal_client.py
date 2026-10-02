"""Tests for the read-only smart-money signal client. No network."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

import signal_ledger
import smart_money_signal_client as c

BANNED = re.compile(r"\b(buy|sell|swap|snipe|execute)\b", re.IGNORECASE)

SAMPLE = {
    "id": "abc123", "token": "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump",
    "symbol": "COLA", "trigger_at": "2026-07-20T00:48:00+00:00",
    "distinct_wallets": 4, "weighted_score": 10,
    "tier_breakdown": {"A": 2, "B": 1, "C": 1}, "call_market_cap": 54965,
}


class RenderTests(unittest.TestCase):
    def test_signals_card_is_read_only_and_verb_clean(self):
        out = {"mode": "smart_money_live_signals", "signals": [SAMPLE], "boundary": c.BOUNDARY}
        msg = c.render_md(out)
        self.assertIn("Smart-Money Cluster Signals", msg)
        self.assertIn("4 wallets clustered", msg)
        self.assertTrue(msg.rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))
        self.assertIsNone(BANNED.search(msg), f"banned verb in card: {msg!r}")

    def test_wallets_card_is_verb_clean(self):
        out = {"mode": "smart_money_wallets", "count": 2,
               "wallets": [{"tier": "A"}, {"tier": "C"}], "boundary": "x"}
        msg = c.render_md(out)
        self.assertIn("A=1", msg)
        self.assertIsNone(BANNED.search(msg))
        self.assertTrue(msg.rstrip().endswith("Advisory + paper only. No wallet, signing, routing, or live execution."))


class EnvGuardTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (c.ENV_BASE, c.ENV_TOKEN, "CHAOS_ALLOW_PRIVATE_SIGNAL_API")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_base_raises(self):
        os.environ.pop(c.ENV_BASE, None)
        with self.assertRaises(SystemExit):
            c.base_url()

    def test_http_rejected(self):
        os.environ[c.ENV_BASE] = "http://api.example.com"
        with self.assertRaises(SystemExit):
            c.base_url()

    def test_private_host_rejected(self):
        os.environ[c.ENV_BASE] = "https://localhost:8000"
        os.environ.pop("CHAOS_ALLOW_PRIVATE_SIGNAL_API", None)
        with self.assertRaises(SystemExit):
            c.base_url()

    def test_valid_https_accepted_with_escape_hatch(self):
        # The SSRF guard fail-closes on unresolvable hosts (no DNS in CI), so exercise the
        # accept path deterministically via the documented private-allow escape hatch.
        os.environ[c.ENV_BASE] = "https://localhost:8000"
        os.environ["CHAOS_ALLOW_PRIVATE_SIGNAL_API"] = "1"
        self.assertEqual(c.base_url(), "https://localhost:8000")

    def test_missing_token_raises(self):
        os.environ.pop(c.ENV_TOKEN, None)
        with self.assertRaises(SystemExit):
            c.read_token()


class LedgerWiringTests(unittest.TestCase):
    def test_ledger_result_mapping(self):
        r = c._to_ledger_result(SAMPLE)
        self.assertEqual(r["mint"], SAMPLE["token"])
        self.assertEqual(r["verdict"], "cluster_observed")
        self.assertEqual(r["market"]["market_cap"], 54965)
        self.assertEqual(r["candidate_score"], 10)

    def test_record_into_signal_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "signal_ledger.sqlite"
            res = signal_ledger.record_signal(c._to_ledger_result(SAMPLE),
                                              source_command="smart-signals", db_path=db)
            self.assertTrue(res["ok"])
            rows = signal_ledger.recent(limit=5, db_path=db)
            self.assertTrue(any(row["source_command"] == "smart-signals"
                                and row["verdict"] == "cluster_observed" for row in rows))

    def test_archive_signals(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "arch.sqlite"
            n = c._archive_signals([SAMPLE], db_path=db)
            self.assertEqual(n, 1)
            con = sqlite3.connect(db)
            row = con.execute("SELECT token, weighted_score, tier_a FROM smart_money_signals").fetchone()
            con.close()
            self.assertEqual(row[0], SAMPLE["token"])
            self.assertEqual(row[1], 10)
            self.assertEqual(row[2], 2)


if __name__ == "__main__":
    unittest.main()
