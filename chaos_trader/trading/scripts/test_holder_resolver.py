"""Holder reads on a rate-limited RPC: retry after 1, 2 and 4 seconds, then a sample cached in the last 15 minutes.

A real JSON-RPC server on loopback answers every call. Replaced here: the RPC URL switch (to point at that
server), the resolver's sleep clock, and the cache directory constant (to a temp dir). The leak check runs a
child Python under -X dev against the same kind of server, with its sleep clock made a no-op.
"""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import helius_common
import holder_resolver
import mint_cluster_query
import smart_wallet_tracker as swt
import token_event_analyzer
import token_scan

SCRIPT_DIR = Path(__file__).resolve().parent
MINT = "So11111111111111111111111111111111111111112"
TOKEN_ACCOUNTS = {"Acc1111111111111111111111111111111111111111": 600.0, "Acc2222222222222222222222222222222222222222": 150.0}
OWNERS = {"Acc1111111111111111111111111111111111111111": "Own1111111111111111111111111111111111111111",
          "Acc2222222222222222222222222222222222222222": "Own2222222222222222222222222222222222222222"}
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# The public mainnet RPC answers getTokenLargestAccounts with exactly these headers.
NOT_SERVED = {"retry-after": "10", "x-ratelimit-method-limit": "0"}


def rpc_server(largest_statuses: list[int], error_headers: dict[str, str] | None = None) -> tuple[type[BaseHTTPRequestHandler], list[str]]:
    """getTokenLargestAccounts answers each status in `largest_statuses` in turn, with `error_headers`, then 200.
    Other methods answer 200."""
    calls: list[str] = []
    script = list(largest_statuses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method, params = req["method"], req["params"]
            calls.append(method)
            if method == "getTokenLargestAccounts":
                status = script.pop(0) if script else 200
                if status != 200:
                    self.reply(status, {"error": "scripted"}, error_headers)
                    return
                value = [{"address": a, "uiAmount": amt, "uiAmountString": str(amt)} for a, amt in TOKEN_ACCOUNTS.items()]
                result = {"value": value}
            elif method == "getTokenSupply":
                result = {"value": {"uiAmount": 1000.0}}
            elif method == "getMultipleAccounts":
                value = []
                for address in params[0]:
                    if address in OWNERS:
                        value.append({"owner": TOKEN_PROGRAM, "executable": False, "data": {"parsed": {"info": {"owner": OWNERS[address]}}}})
                    else:
                        value.append({"owner": SYSTEM_PROGRAM, "executable": False, "data": ["", "base64"]})
                result = {"value": value}
            else:
                self.reply(400, {"error": f"unexpected {method}"})
                return
            self.reply(200, {"jsonrpc": "2.0", "id": req["id"], "result": result})

    return Handler, calls


class HolderRetryAndCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_dir = Path(tmp.name) / "trading" / "cache" / "holders"
        self.cache_file = self.cache_dir / f"{MINT}.json"
        self.sleeps: list[float] = []
        for patch in (mock.patch.object(holder_resolver, "HOLDER_CACHE", self.cache_dir),
                      mock.patch.object(holder_resolver, "_sleep", self.sleeps.append)):
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, largest_statuses: list[int], error_headers: dict[str, str] | None = None) -> tuple[str, list[str]]:
        handler, calls = rpc_server(largest_statuses, error_headers)
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        patch = mock.patch.object(helius_common, "rpc_endpoint", return_value=url)
        patch.start()
        self.addCleanup(patch.stop)
        return url, calls

    def read(self, largest_statuses: list[int], error_headers: dict[str, str] | None = None) -> tuple[dict, list[str]]:
        _, calls = self.serve(largest_statuses, error_headers)
        return holder_resolver.resolve_holders(MINT, 20), calls

    def age_cache(self, minutes: int) -> str:
        sample = json.loads(self.cache_file.read_text(encoding="utf-8"))
        sample["fetched_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        self.cache_file.write_text(json.dumps(sample), encoding="utf-8")
        return sample["fetched_at"]

    def fetched_at(self) -> datetime:
        return datetime.fromisoformat(json.loads(self.cache_file.read_text(encoding="utf-8"))["fetched_at"])

    def test_two_429s_then_a_200_returns_the_sample_after_waits_of_1_and_2_and_caches_it(self):
        result, calls = self.read([429, 429])
        self.assertEqual(self.sleeps, [1, 2])
        self.assertEqual(calls.count("getTokenLargestAccounts"), 3)
        self.assertNotIn("holder_data", result)
        self.assertEqual([h["owner"] for h in result["holders"]], list(OWNERS.values()))
        self.assertEqual(result["raw_top_pct"], 75.0)
        cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(cached.pop("fetched_at"))
        self.assertEqual(fetched.utcoffset(), timedelta(0))
        self.assertLess(datetime.now(timezone.utc) - fetched, timedelta(minutes=1))
        self.assertEqual(cached, json.loads(json.dumps(result)))

    def test_four_429s_with_a_fresh_cache_return_the_cached_sample_marked_with_its_age(self):
        live, _ = self.read([])
        self.age_cache(3)
        result, calls = self.read([429, 429, 429, 429])
        self.assertEqual(self.sleeps, [1, 2, 4])
        self.assertEqual(calls, ["getTokenLargestAccounts"] * 4)
        self.assertEqual(result["holder_data"], "cached 3m")
        self.assertEqual({k: v for k, v in result.items() if k != "holder_data"}, json.loads(json.dumps(live)))

    def test_four_429s_with_a_stale_cache_are_unavailable_and_leave_the_cache_alone(self):
        self.read([])
        self.age_cache(20)
        before = self.cache_file.read_bytes()
        result, _ = self.read([429, 429, 429, 429])
        self.assertEqual(result["holder_data"], "unavailable (rate limited)")
        self.assertEqual(result["holders"], [])
        self.assertEqual(self.cache_file.read_bytes(), before)

    def test_four_429s_with_no_cache_are_unavailable_and_create_no_file(self):
        result, _ = self.read([429, 429, 429, 429])
        self.assertEqual(result["holder_data"], "unavailable (rate limited)")
        self.assertFalse(self.cache_dir.exists())

    def test_a_200_after_a_cached_fallback_refreshes_the_file(self):
        self.read([])
        old = self.age_cache(3)
        fallback, _ = self.read([429, 429, 429, 429])
        self.assertEqual(fallback["holder_data"], "cached 3m")
        fresh, _ = self.read([])
        self.assertNotIn("holder_data", fresh)
        self.assertGreater(self.fetched_at(), datetime.fromisoformat(old))

    def test_a_503_keeps_the_rpc_loop_and_no_resolver_wait(self):
        with mock.patch.object(helius_common.time, "sleep") as rpc_sleep:
            result, calls = self.read([503, 503])
        self.assertEqual(self.sleeps, [])
        self.assertEqual([c.args[0] for c in rpc_sleep.call_args_list], [2])
        self.assertEqual(calls.count("getTokenLargestAccounts"), 3)
        self.assertNotIn("holder_data", result)

    def test_a_cached_sample_renders_the_full_table_with_its_age(self):
        self.read([])
        self.age_cache(3)
        result, _ = self.read([429, 429, 429, 429])
        text = holder_resolver.render_md(result)
        self.assertIn("- Holder data: cached 3m", text)
        self.assertIn("| Rank | Owner | Class | Supply % | Reason |", text)

    def test_a_429_saying_the_method_is_not_served_stops_at_once(self):
        result, calls = self.read([429, 429, 429, 429], NOT_SERVED)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(calls, ["getTokenLargestAccounts"])
        self.assertEqual(result["holder_data"], "unavailable (not served by this RPC)")
        self.assertEqual(result["holders"], [])
        self.assertFalse(self.cache_dir.exists())

    def test_rpc_request_does_not_retry_a_method_the_rpc_does_not_serve(self):
        _, calls = self.serve([429, 429, 429, 429], NOT_SERVED)
        with mock.patch.object(helius_common.time, "sleep") as rpc_sleep, self.assertRaises(SystemExit) as ctx:
            helius_common.rpc_request("getTokenLargestAccounts", [MINT])
        self.assertEqual(rpc_sleep.call_args_list, [])
        self.assertEqual(calls, ["getTokenLargestAccounts"])
        self.assertEqual(json.loads(str(ctx.exception))["http_status"], 429)
        self.assertEqual(ctx.exception.headers.get("x-ratelimit-method-limit"), "0")

    def test_a_token_scan_waits_for_nothing_when_largest_accounts_is_not_served(self):
        # token_scan reads the largest accounts itself and again through the holder read; neither may wait.
        _, calls = self.serve([429] * 8, NOT_SERVED)
        out = io.StringIO()
        with mock.patch.object(helius_common.time, "sleep") as rpc_sleep, \
             mock.patch.object(sys, "argv", ["token_scan.py", MINT, "--raw"]), redirect_stdout(out):
            token_scan.main()
        scan = json.loads(out.getvalue())
        self.assertEqual(rpc_sleep.call_args_list, [])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(calls.count("getTokenLargestAccounts"), 2)
        self.assertEqual(json.loads(scan["endpoint_errors"]["largest_accounts"])["http_status"], 429)
        self.assertEqual(scan["holder_resolution"]["holder_data"], "unavailable (not served by this RPC)")

    def test_a_429_with_a_nonzero_method_limit_still_retries(self):
        result, _ = self.read([429, 429, 429, 429], {"x-ratelimit-method-limit": "40"})
        self.assertEqual(self.sleeps, [1, 2, 4])
        self.assertEqual(result["holder_data"], "unavailable (rate limited)")

    def cached_after_live(self) -> tuple[dict, dict]:
        live, _ = self.read([])
        self.age_cache(3)
        cached, _ = self.read([429, 429, 429, 429])
        self.assertEqual(cached["holder_data"], "cached 3m")
        return live, cached

    def test_the_analyzer_scores_a_cached_sample_like_a_live_one_and_shows_its_age(self):
        live, cached = self.cached_after_live()
        marked = {"token_scan": {"holder_resolution": cached}}
        token_event_analyzer.mark_holder_data(marked)
        self.assertEqual(marked["holder_data"], "cached 3m")
        self.assertNotIn("holder_data_unavailable", marked)
        from_cache = token_event_analyzer.classify({"token_scan": {"holder_resolution": cached}})
        from_live = token_event_analyzer.classify({"token_scan": {"holder_resolution": live}})
        self.assertEqual((from_cache["verdict"], from_cache["score"]), (from_live["verdict"], from_live["score"]))
        self.assertIn("holder data cached 3m", from_cache["risk_flags"])
        self.assertNotIn("holder data cached 3m", from_live["risk_flags"])

    def test_mint_cluster_scores_a_cached_sample_with_the_resolver_numbers(self):
        self.read([])
        self.age_cache(3)
        self.serve([429, 429, 429, 429])
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        swt.ensure_db(con)
        result = mint_cluster_query.score_mint(con, MINT)
        self.assertEqual(result["holder_resolution"]["holder_data"], "cached 3m")
        self.assertEqual(result["metrics"]["concentration_source"], "holder_resolver_adjusted")


class HttpErrorIsClosedTests(unittest.TestCase):
    """rpc_request closes the error response on the retry path and the raise path: no ResourceWarning under -X dev."""

    def test_two_429s_leave_no_resource_warning(self):
        handler, calls = rpc_server([429, 429])
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        # retries=1: the first 429 takes the retry path, the second the raise path. The child's sleep is a no-op clock.
        code = "\n".join([
            "import gc, time",
            "import helius_common",
            f"helius_common.rpc_endpoint = lambda: {url!r}",
            "time.sleep = lambda seconds: None",
            "try:",
            f"    helius_common.rpc_request('getTokenLargestAccounts', [{MINT!r}], retries=1)",
            "except SystemExit as exc:",
            "    print(exc.headers.get('Content-Type'))",
            "gc.collect()",
        ])
        p = subprocess.run([sys.executable, "-X", "dev", "-c", code], cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(calls, ["getTokenLargestAccounts", "getTokenLargestAccounts"])
        self.assertEqual(p.stdout.strip(), "application/json")
        self.assertNotIn("ResourceWarning", p.stderr)


if __name__ == "__main__":
    unittest.main()
