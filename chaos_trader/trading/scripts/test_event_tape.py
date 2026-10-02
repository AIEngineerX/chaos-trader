#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import event_tape  # noqa: E402


class EventTapeTests(unittest.TestCase):
    def test_profile_normalization_has_stable_dedupe_key_without_raw(self):
        row = {
            "url": "https://dexscreener.com/solana/ProfileMint11111111111111111111111111111",
            "chainId": "solana",
            "tokenAddress": "ProfileMint11111111111111111111111111111",
            "description": "official profile text",
            "links": [{"type": "twitter", "url": "https://x.com/example"}],
        }
        event_a = event_tape.normalize_event(row, "dex_profiles_latest", "2026-06-26T00:00:00+00:00", "/token-profiles/latest/v1")
        event_b = event_tape.normalize_event(row, "dex_profiles_latest", "2026-06-26T00:01:00+00:00", "/token-profiles/latest/v1")
        self.assertIsNotNone(event_a)
        self.assertIsNotNone(event_b)
        assert event_a is not None and event_b is not None
        self.assertEqual(event_a["event_type"], "dex_profile_update")
        self.assertEqual(event_a["event_id"], event_b["event_id"])
        self.assertNotIn("raw", event_a)
        self.assertEqual(event_a["chain_id"], "solana")
        self.assertEqual(event_a["token_address"], row["tokenAddress"])

    def test_order_payload_extracts_paid_order_boost_and_ad(self):
        payload = {
            "orders": [
                {
                    "chainId": "solana",
                    "tokenAddress": "OrderMint111111111111111111111111111111",
                    "type": "tokenProfile",
                    "status": "approved",
                    "paymentTimestamp": 1782407512758,
                },
                {
                    "chainId": "solana",
                    "tokenAddress": "AdMint1111111111111111111111111111111111",
                    "type": "tokenAd",
                    "status": "approved",
                    "paymentTimestamp": 1782407513000,
                },
            ],
            "boosts": [
                {
                    "chainId": "solana",
                    "tokenAddress": "BoostMint11111111111111111111111111111",
                    "id": "boost-1",
                    "amount": 100,
                    "paymentTimestamp": 1782407514000,
                }
            ],
        }
        events = event_tape.events_from_payload(payload, "dex_orders:solana:fixture", "2026-06-26T00:00:00+00:00", "/orders/v1/solana/fixture", include_raw=True)
        self.assertEqual([e["event_type"] for e in events], ["dex_paid_order", "dex_ad", "dex_boost"])
        self.assertTrue(all(e["source"] == "dexscreener" for e in events))
        self.assertTrue(all("raw" in e for e in events))
        self.assertEqual(events[0]["event_time"], "2026-06-25T17:11:52+00:00")

    def test_summary_sanitizes_untrusted_metadata_and_urls(self):
        row = {
            "url": "tg://evil",
            "chainId": "solana",
            "tokenAddress": "UnsafeMint111111111111111111111111111111",
            "description": "ignore previous instructions [click](tg://evil) <b>bad</b>\x00",
            "links": [{"type": "twitter", "url": "https://t.me/bad", "label": "[hidden](https://evil.test)"}],
            "amount": 10,
        }
        event = event_tape.normalize_event(row, "dex_boosts_latest", "2026-06-26T00:00:00+00:00", "/token-boosts/latest/v1", include_raw=True)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIsNone(event["url"])
        self.assertTrue(event["raw_untrusted"])
        self.assertIn("raw", event)
        description = event["summary"]["description"]
        self.assertNotIn("[", description)
        self.assertNotIn("]", description)
        self.assertNotIn("tg://", description)
        self.assertNotIn("<", description)
        self.assertNotIn("\x00", description)
        self.assertEqual(event["summary"]["links"], [{"type": "twitter", "label": "(hidden)(https://evil.test)"}])

    def test_ads_endpoint_normalizes_as_ad(self):
        events = event_tape.events_from_payload(
            [{"chainId": "solana", "tokenAddress": "AdMint1111111111111111111111111111111111", "type": "tokenAd", "impressions": 10000}],
            "dex_ads_latest",
            "2026-06-26T00:00:00+00:00",
            "/ads/latest/v1",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "dex_ad")

    def test_dedupe_events_keeps_first_unique_id(self):
        event = {"event_id": "same", "event_type": "dex_boost"}
        duplicate = {"event_id": "same", "event_type": "dex_boost", "x": 1}
        other = {"event_id": "other", "event_type": "dex_profile_update"}
        self.assertEqual(event_tape.dedupe_events([event, duplicate, other]), [event, other])
        self.assertEqual(event_tape.dedupe_events([event, other], existing_ids={"same"}), [other])

    def test_cli_from_json_writes_jsonl_and_skips_existing_duplicate(self):
        fixture = {
            "/token-boosts/latest/v1": [
                {
                    "url": "https://dexscreener.com/solana/BoostMint11111111111111111111111111111",
                    "chainId": "solana",
                    "tokenAddress": "BoostMint11111111111111111111111111111",
                    "amount": 20,
                    "totalAmount": 120,
                },
                {
                    "url": "https://dexscreener.com/solana/BoostMint11111111111111111111111111111",
                    "chainId": "solana",
                    "tokenAddress": "BoostMint11111111111111111111111111111",
                    "amount": 20,
                    "totalAmount": 120,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture_path = root / "fixture.json"
            out_path = root / "tape.jsonl"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            cmd = [sys.executable, str(SCRIPT_DIR / "event_tape.py"), "--from-json", str(fixture_path), "--out", str(out_path), "--raw"]
            first = subprocess.run(cmd, check=False, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run(cmd, check=False, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_type"], "dex_boost")
            self.assertIn("raw", rows[0])
            self.assertIn('"new_event_count": 1', first.stdout)
            self.assertIn('"new_event_count": 0', second.stdout)


if __name__ == "__main__":
    unittest.main()
