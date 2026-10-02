import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import chaos_cmd
import helius_common
import holder_resolver
import pumpfun_launch_read
import token_event_analyzer
import token_scan

MINT = "So11111111111111111111111111111111111111112"
MARKER = "unavailable (rate limited)"


class HolderRateLimitTests(unittest.TestCase):
    """The public RPC inside a rate window answers every call with a 429. Only the network call is faked."""

    def setUp(self):
        self.calls = 0
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)

        def rate_limited(req, timeout=None):
            self.calls += 1
            err = urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"error": "rate limited"}'))
            self.addCleanup(err.close)
            raise err

        patches = [
            mock.patch.dict(os.environ, {"SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com", "HELIUS_API_KEY": ""}),
            mock.patch.object(helius_common.no_redirect, "open_no_redirect", side_effect=rate_limited),
            mock.patch.object(helius_common.time, "sleep"),
            # The public RPC host is checked by DNS; answer that check here so the tests run with DNS down.
            mock.patch.object(helius_common, "_host_is_private_or_reserved", return_value=False),
            # The holder read's own 429 waits, and an empty cache folder so no earlier sample answers.
            mock.patch.object(holder_resolver, "_sleep"),
            mock.patch.object(holder_resolver, "HOLDER_CACHE", Path(cache.name)),
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.sleep = started[2]
        self.holder_sleep = started[4]

    def test_retry_backoff_is_two_four_eight_seconds_then_a_clean_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            helius_common.rpc_request("getTokenSupply", [MINT])
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 4, 8])
        self.assertEqual(self.calls, 4)
        self.assertEqual(json.loads(str(ctx.exception))["http_status"], 429)

    def test_holder_read_degrades_to_an_empty_set_with_a_marker(self):
        result = holder_resolver.resolve_holders(MINT, 20)
        self.assertEqual([c.args[0] for c in self.holder_sleep.call_args_list], [1, 2, 4])
        self.assertTrue(result["ok"])
        self.assertEqual(result["holders"], [])
        self.assertEqual(result["holder_data"], MARKER)

    def test_token_read_still_returns_a_card_with_the_marker(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["token_scan.py", MINT, "--raw"]), redirect_stdout(out):
            token_scan.main()
        scan = json.loads(out.getvalue())
        self.assertEqual(scan["holder_resolution"]["holder_data"], MARKER)
        self.assertEqual(token_event_analyzer.holder_data_of({"token_scan": scan}), MARKER)
        card = chaos_cmd.compact_token({"mint": MINT, "holder_data": MARKER})
        self.assertIn(f"HOLDERS: {MARKER}", card)

    def test_degraded_holder_read_is_flagged_without_moving_the_verdict(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["token_scan.py", MINT, "--raw"]), redirect_stdout(out):
            token_scan.main()
        degraded = json.loads(out.getvalue())
        normal = {"holder_resolution": {"adjusted_discretionary_pct": 10.0, "holders": []}}
        flag = f"holder data {MARKER}"
        with_marker = token_event_analyzer.classify({"token_scan": degraded})
        without = token_event_analyzer.classify({"token_scan": normal})
        self.assertIn(flag, with_marker["risk_flags"])
        self.assertNotIn(flag, without["risk_flags"])
        self.assertEqual((with_marker["verdict"], with_marker["score"]), (without["verdict"], without["score"]))
        self.assertEqual(token_event_analyzer.holder_data_of({"token_scan": normal}), None)
        marked = {"token_scan": degraded}
        token_event_analyzer.mark_holder_data(marked)
        self.assertIs(marked["holder_data_unavailable"], True)
        self.assertEqual(marked["holder_data"], MARKER)
        clean = {"token_scan": normal}
        token_event_analyzer.mark_holder_data(clean)
        self.assertNotIn("holder_data_unavailable", clean)

    def test_launch_probe_still_returns_with_supply_unread(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["pumpfun_launch_read.py", MINT, "--raw"]), redirect_stdout(out):
            pumpfun_launch_read.main()
        probe = json.loads(out.getvalue())
        self.assertIsNone(probe["supply"])
        self.assertEqual(json.loads(probe["supply_error"])["http_status"], 429)
        self.assertIn("Token supply unavailable from RPC; supply unread.", probe["risk_flags"])


if __name__ == "__main__":
    unittest.main()
