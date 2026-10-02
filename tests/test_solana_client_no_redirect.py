"""The Solana skill helper never follows a redirect on an RPC call: SOLANA_RPC_URL can carry a provider key.

Two real HTTP servers on loopback, no network. Server A 302s every request to server B with the key in the
Location; B records what reaches it. Replaced: the helper's RPC URL, which is the network boundary.
"""
import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "blockchain" / "solana" / "scripts" / "solana_client.py"
SPEC = importlib.util.spec_from_file_location("solana_client_no_redirect_under_test", SCRIPT)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)

KEY = "keyed-url-secret-not-real"


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def drain(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)


class SolanaClientNoRedirectTests(unittest.TestCase):
    def serve(self, handler):
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def setUp(self):
        self.received = []
        received = self.received

        class Target(_Quiet):
            def do_POST(self):
                self.drain()
                received.append(self.path)
                body = b'{"jsonrpc": "2.0", "id": 1, "result": 7}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        url_b = self.serve(Target)

        class Redirector(_Quiet):
            def do_POST(self):
                self.drain()
                self.send_response(302)
                self.send_header("Location", f"{url_b}?api-key={KEY}")
                self.send_header("Content-Length", "0")
                self.end_headers()

        patch = mock.patch.object(client, "RPC_URL", f"{self.serve(Redirector)}?api-key={KEY}")
        patch.start()
        self.addCleanup(patch.stop)

    def test_rpc_call_refuses_the_redirect(self):
        with self.assertRaises(SystemExit) as caught:
            client.rpc("getSlot")
        self.assertEqual(str(caught.exception), "RPC unavailable: redirect refused (HTTP 302)")
        self.assertEqual(self.received, [])

    def test_rpc_batch_refuses_the_redirect(self):
        with self.assertRaises(SystemExit) as caught:
            client.rpc_batch([{"method": "getSlot"}])
        self.assertEqual(str(caught.exception), "RPC unavailable: redirect refused (HTTP 302)")
        self.assertEqual(self.received, [])


if __name__ == "__main__":
    unittest.main()
