"""Full-transaction reads ask for transaction version 1, and once more for the version an RPC's refusal names.

A real JSON-RPC server on loopback answers as a Solana node does: a transaction newer than the version asked for,
or any versioned transaction when the parameter is missing, gets error -32015 naming the version to ask for.
Replaced here: the RPC URL (to point at that server) and the tracker's per-call sleep.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import helius_common
import smart_wallet_tracker as swt
from test_fetch_txs_standard import WALLET, parsed_swap

# Newest first, as getSignaturesForAddress returns them; each a wallet-signed swap of one mint against SOL.
TXS = {
    "sigV2Sell": (2,1785371800, 1_499_995_000, 1_699_990_000, 1_000_000, 500_000),
    "sigV1Buy": (1, 1785371200, 1_999_995_000, 1_499_995_000, 500_000, 1_000_000),
    "sigV0Buy": (0, 1785370600, 2_499_995_000, 1_999_995_000, 250_000, 500_000),
    "sigLegacyBuy": ("legacy", 1785370000, 3_000_000_000, 2_499_995_000, None, 250_000),
}


def refusal(version: int) -> dict:
    """The error a Solana RPC sends for a transaction newer than the version asked for (VERIFIED on mainnet-beta for 1)."""
    return {"code": -32015, "message": "Transaction version (%d) is not supported by the requesting client. Please try the request "
                                       "again with the following configuration parameter: \"maxSupportedTransactionVersion\": %d" % (version, version)}


def solana_rpc() -> tuple[type[BaseHTTPRequestHandler], list[dict]]:
    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(req)
            method, params = req["method"], req["params"]
            if method == "getSignaturesForAddress":
                reply = {"result": [] if (params[1] or {}).get("before") else [{"signature": s} for s in TXS]}
            elif method == "getTransaction":
                version, *swap = TXS[params[0]]
                number = -1 if version == "legacy" else int(version)
                asked = params[1].get("maxSupportedTransactionVersion")
                if number >= 0 and (asked is None or asked < number):
                    reply = {"error": refusal(number)}
                else:
                    tx = parsed_swap(params[0], *swap)
                    tx["version"] = version if version == "legacy" else number
                    reply = {"result": tx}
            else:
                reply = {"error": {"code": -32601, "message": f"Method not found: {method}"}}
            body = json.dumps({"jsonrpc": "2.0", "id": req["id"], **reply}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler, bodies


class TxVersionTests(unittest.TestCase):
    def setUp(self):
        handler, self.bodies = solana_rpc()
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        for patch in (mock.patch.object(helius_common, "rpc_endpoint", return_value=url), mock.patch.object(swt.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def tx_requests(self) -> list[tuple[str, object]]:
        return [(b["params"][0], b["params"][1].get("maxSupportedTransactionVersion", "missing")) for b in self.bodies if b["method"] == "getTransaction"]

    def test_the_server_refuses_a_missing_parameter_as_a_node_does(self):
        with self.assertRaises(SystemExit) as ctx:
            helius_common.rpc_request("getTransaction", ["sigV0Buy", {"encoding": "jsonParsed"}])
        self.assertEqual(json.loads(str(ctx.exception))["error"], refusal(0))
        self.assertEqual(helius_common._named_tx_version(ctx.exception), 0)

    def test_the_ingest_asks_for_version_1_and_retries_once_with_the_version_named(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys=ON")
        swt.ensure_db(con)
        result = swt.enrich_wallet(con, WALLET, 10, 1, include_api=False)  # a loopback URL is not Helius, so the standard path runs
        self.assertEqual(self.tx_requests(), [("sigV2Sell", 1), ("sigV2Sell", 2), ("sigV1Buy", 1), ("sigV0Buy", 1), ("sigLegacyBuy", 1)])
        self.assertEqual(result["events"], {"buy": 3, "sell": 1})
        self.assertEqual(result["null_transactions"], 0)
        events = con.execute("SELECT signature, event_type FROM wallet_token_events WHERE wallet=? ORDER BY block_time_utc", (WALLET,)).fetchall()
        self.assertEqual(events, [("sigLegacyBuy", "buy"), ("sigV0Buy", "buy"), ("sigV1Buy", "buy"), ("sigV2Sell", "sell")])

    def test_an_error_that_names_no_newer_version_is_raised_after_one_call(self):
        for error in ({"code": -32602, "message": "Invalid params: invalid type: string \"x\", expected u8"}, refusal(1)):
            calls = []

            def rpc(method, params, **kwargs):
                calls.append(params[1]["maxSupportedTransactionVersion"])
                raise SystemExit(json.dumps({"ok": False, "method": method, "error": error}))

            with self.assertRaises(SystemExit):
                helius_common.rpc_tx_request("getTransaction", ["sig", {"encoding": "jsonParsed"}], rpc=rpc)
            self.assertEqual(calls, [1], error)

    def test_the_caller_config_is_left_as_it_was(self):
        config = {"encoding": "jsonParsed", "commitment": "finalized"}
        helius_common.rpc_tx_request("getTransaction", ["sigV2Sell", config])
        self.assertEqual(config, {"encoding": "jsonParsed", "commitment": "finalized"})
        self.assertEqual(self.tx_requests(), [("sigV2Sell", 1), ("sigV2Sell", 2)])


if __name__ == "__main__":
    unittest.main()
