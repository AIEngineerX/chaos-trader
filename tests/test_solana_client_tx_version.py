"""The Solana skill helper asks for transaction version 1, and once more for the version an RPC's refusal names.

`tx` and `whales` run in-process against a JSON-RPC server on loopback that answers as a node does: a transaction
newer than the version asked for gets error -32015 naming the version to ask for. Replaced: the helper's RPC URL
and its CoinGecko price read (the two network boundaries).
"""
import importlib.util
import io
import json
import threading
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "blockchain" / "solana" / "scripts" / "solana_client.py"
SPEC = importlib.util.spec_from_file_location("solana_client_under_test", SCRIPT)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)

SENDER = "Sender1111111111111111111111111111111111111"
RECEIVER = "Receiver111111111111111111111111111111111111"


def refusal(version):
    return {"code": -32015, "message": "Transaction version (%d) is not supported by the requesting client. Please try the request "
                                       "again with the following configuration parameter: \"maxSupportedTransactionVersion\": %d" % (version, version)}


def transfer(version):
    """A jsonParsed transaction moving 250 SOL from SENDER to RECEIVER."""
    return {"version": version, "slot": 7, "blockTime": 1790000000,
            "meta": {"err": None, "fee": 5000, "preBalances": [300_000_000_000, 0], "postBalances": [49_999_995_000, 250_000_000_000]},
            "transaction": {"signatures": ["sig"], "message": {"accountKeys": [{"pubkey": SENDER}, {"pubkey": RECEIVER}], "instructions": []}}}


def node(tx_version, block_version):
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(req)
            asked = (req["params"][-1] if req["params"] and isinstance(req["params"][-1], dict) else {}).get("maxSupportedTransactionVersion")
            if req["method"] == "getSlot":
                reply = {"result": 7}
            elif req["method"] in ("getTransaction", "getBlock"):
                newest = tx_version if req["method"] == "getTransaction" else block_version
                if asked is None or asked < newest:
                    reply = {"error": refusal(newest)}
                elif req["method"] == "getTransaction":
                    reply = {"result": transfer(newest)}
                else:
                    reply = {"result": {"transactions": [transfer(0), transfer(newest)]}}
            else:
                reply = {"error": {"code": -32601, "message": "Method not found"}}
            body = json.dumps({"jsonrpc": "2.0", "id": req["id"], **reply}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler, bodies


class SolanaClientTxVersionTests(unittest.TestCase):
    def serve(self, tx_version=1, block_version=1):
        handler, bodies = node(tx_version, block_version)
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for patch in (mock.patch.object(client, "RPC_URL", f"http://127.0.0.1:{server.server_address[1]}/"),
                      mock.patch.object(client, "fetch_sol_price", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)
        return bodies

    def asked(self, bodies, method):
        return [b["params"][-1]["maxSupportedTransactionVersion"] for b in bodies if b["method"] == method]

    def run_cmd(self, func, args):
        out = io.StringIO()
        with redirect_stdout(out):
            func(args)
        return json.loads(out.getvalue())

    def test_tx_reads_a_v1_transaction_with_one_request(self):
        bodies = self.serve(tx_version=1)
        result = self.run_cmd(client.cmd_tx, Namespace(signature="sig"))
        self.assertEqual(self.asked(bodies, "getTransaction"), [1])
        self.assertEqual(result["balance_changes"], [{"account": SENDER, "change_SOL": -250.000005}, {"account": RECEIVER, "change_SOL": 250.0}])

    def test_tx_asks_once_more_with_the_version_the_refusal_names(self):
        bodies = self.serve(tx_version=2)
        result = self.run_cmd(client.cmd_tx, Namespace(signature="sig"))
        self.assertEqual(self.asked(bodies, "getTransaction"), [1, 2])
        self.assertEqual(result["status"], "success")

    def test_whales_reads_a_block_holding_v1_transactions(self):
        bodies = self.serve(block_version=1)
        result = self.run_cmd(client.cmd_whales, Namespace(min_sol=100))
        self.assertEqual(self.asked(bodies, "getBlock"), [1])
        self.assertEqual(len(result["large_transfers"]), 2)
        self.assertEqual(result["large_transfers"][0], {"sender": SENDER, "receiver": RECEIVER, "amount_SOL": 250.0})

    def test_with_the_retry_spent_a_refusal_exits_and_only_a_newer_named_version_is_retried(self):
        handler_bodies = self.serve(tx_version=1)
        with self.assertRaises(SystemExit) as ctx:
            client._rpc_call("getTransaction", ["sig", {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}], version_retry=False)
        self.assertIn("-32015", str(ctx.exception))
        self.assertEqual(self.asked(handler_bodies, "getTransaction"), [0])
        self.assertIsNone(client._named_tx_version(refusal(1), ["sig", {"maxSupportedTransactionVersion": 1}]))
        self.assertEqual(client._named_tx_version(refusal(2), ["sig", {"maxSupportedTransactionVersion": 1}]), 2)
        self.assertIsNone(client._named_tx_version(refusal(2), []))


if __name__ == "__main__":
    unittest.main()
