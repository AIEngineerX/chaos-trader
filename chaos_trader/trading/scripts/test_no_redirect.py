"""A Bearer token never follows a redirect. Two real HTTP servers on loopback, no network.

Server A answers every request with a redirect to server B; server B records the Authorization
header of every request it gets. Through no_redirect.open_no_redirect the redirect raises
HTTPError and B gets nothing. Through urllib's default urlopen, B gets the token: that is the
bug the handler is there for.
"""
from __future__ import annotations

import inspect
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import no_redirect
import smart_money_signal_client
import x_provider

TOKEN_HEADER = "Bearer secret"
REDIRECT_CODES = (301, 302, 303, 307, 308)


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def recorder() -> tuple[type[BaseHTTPRequestHandler], list[str | None]]:
    """Server B: records each request's Authorization header and answers 200."""
    received: list[str | None] = []

    class Recorder(_QuietHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    return Recorder, received


def redirector(location: str, code: int) -> type[BaseHTTPRequestHandler]:
    """Server A: answers every request with `code` and Location: server B."""

    class Redirector(_QuietHandler):
        def do_GET(self):
            # Read any request body first, so closing the socket never resets the connection.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(code)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

    return Redirector


class TwoServerCase(unittest.TestCase):
    def serve(self, handler: type[BaseHTTPRequestHandler]) -> str:
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        # Cleanups run last-in first-out: stop the loop, close the socket, then join the thread.
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def servers(self, code: int = 302) -> tuple[str, list[str | None]]:
        handler_b, received = recorder()
        url_b = self.serve(handler_b)
        url_a = self.serve(redirector(url_b, code))
        return url_a, received


class OpenNoRedirectTests(TwoServerCase):
    def test_redirect_raises_and_the_target_gets_no_request(self):
        for code in REDIRECT_CODES:
            with self.subTest(code=code):
                url_a, received = self.servers(code)
                request = urllib.request.Request(url_a, headers={"Authorization": TOKEN_HEADER})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    no_redirect.open_no_redirect(request, timeout=5)
                caught.exception.close()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(received, [])

    def test_post_redirect_is_refused_too(self):
        url_a, received = self.servers(307)
        request = urllib.request.Request(url_a, data=b"{}", method="POST", headers={"Authorization": TOKEN_HEADER})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            no_redirect.open_no_redirect(request, timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 307)
        self.assertEqual(received, [])

    def test_default_urlopen_forwards_the_token_which_is_the_bug(self):
        url_a, received = self.servers(302)
        request = urllib.request.Request(url_a, headers={"Authorization": TOKEN_HEADER})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            response.read()
        self.assertEqual(received, [TOKEN_HEADER])


class CallSiteWiringTests(unittest.TestCase):
    """Both Bearer-token clients open their request through open_no_redirect, never urlopen."""

    def assert_wired(self, module, function) -> None:
        self.assertIs(module.no_redirect, no_redirect)
        source = inspect.getsource(function)
        self.assertIn("no_redirect.open_no_redirect(", source)
        self.assertNotIn("urlopen(", source)

    def test_signal_client_uses_open_no_redirect(self):
        self.assert_wired(smart_money_signal_client, smart_money_signal_client._get)

    def test_signal_client_refuses_http_so_it_cannot_be_pointed_at_a_loopback_server(self):
        # Why the signal client is covered by wiring, not by the two servers: it accepts only https,
        # even with the private-host escape hatch on, and the test servers speak plain http.
        names = (smart_money_signal_client.ENV_BASE, "CHAOS_ALLOW_PRIVATE_SIGNAL_API")
        saved = {name: os.environ.get(name) for name in names}

        def restore() -> None:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        os.environ[smart_money_signal_client.ENV_BASE] = "http://127.0.0.1:8000"
        os.environ["CHAOS_ALLOW_PRIVATE_SIGNAL_API"] = "1"
        with self.assertRaises(SystemExit) as caught:
            smart_money_signal_client.base_url()
        self.assertIn("https", str(caught.exception))

    def test_x_provider_uses_open_no_redirect(self):
        # x_provider has no base-URL setting (the xAI endpoint is a constant), so it is covered by
        # wiring here and by the redirect mapping test in test_x_provider.
        self.assert_wired(x_provider, x_provider._search_xai)
        self.assertEqual(x_provider.XAI_RESPONSES_URL, "https://api.x.ai/v1/responses")


if __name__ == "__main__":
    unittest.main()
