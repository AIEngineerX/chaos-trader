#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import helius_common  # noqa: E402
from helius_common import rpc_request  # noqa: E402


class HeliusEndpointPolicyTests(unittest.TestCase):
    def endpoint_with_env(self, env: dict[str, str]) -> str:
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(helius_common, "load_env", return_value=None):
                return helius_common.rpc_endpoint()

    def test_private_http_rpc_rejected_by_default_without_leaking_endpoint(self):
        with self.assertRaises(SystemExit) as ctx:
            self.endpoint_with_env({"SOLANA_RPC_URL": "http://127.0.0.1:8899"})
        self.assertIn("SOLANA_RPC_URL", str(ctx.exception))
        self.assertIn("https", str(ctx.exception))
        self.assertNotIn("127.0.0.1", str(ctx.exception))

    def test_private_rpc_requires_private_gate_and_https(self):
        with self.assertRaises(SystemExit):
            self.endpoint_with_env({"SOLANA_RPC_URL": "http://127.0.0.1:8899"})
        with self.assertRaises(SystemExit):
            self.endpoint_with_env({"SOLANA_RPC_URL": "https://127.0.0.1:8899"})
        with self.assertRaises(SystemExit):
            self.endpoint_with_env({"SOLANA_RPC_URL": "https://[::1]:8899"})
        with self.assertRaises(SystemExit):
            self.endpoint_with_env({
                "SOLANA_RPC_URL": "http://127.0.0.1:8899",
                "CHAOS_ALLOW_PRIVATE_RPC": "1",
            })

        self.assertEqual(
            self.endpoint_with_env({
                "SOLANA_RPC_URL": "https://127.0.0.1:8899",
                "CHAOS_ALLOW_PRIVATE_RPC": "1",
            }),
            "https://127.0.0.1:8899",
        )

    def test_rpc_url_rejects_userinfo_and_helius_suffix_tricks(self):
        for env in (
            {"SOLANA_RPC_URL": "https://key@mainnet.helius-rpc.com/?api-key=test-key"},
            {"SOLANA_RPC_URL": "https://mainnet.helius-rpc.com.evil.tld/?api-key=test-key"},
        ):
            with self.assertRaises(SystemExit):
                self.endpoint_with_env(env)

    def test_rpc_request_rejects_non_readonly_methods_before_network(self):
        with mock.patch.object(helius_common, "rpc_endpoint", return_value="https://mainnet.helius-rpc.com/?api-key=test-key"):
            with self.assertRaises(SystemExit) as ctx:
                rpc_request("sendTransaction", ["payload"])
        self.assertIn("not read-only allowlisted", str(ctx.exception))

    def test_mainnet_helius_endpoint_allowed_by_default(self):
        endpoint = "https://mainnet.helius-rpc.com/?api-key=test-key"
        self.assertEqual(self.endpoint_with_env({"SOLANA_RPC_URL": endpoint}), endpoint)

    def test_api_key_constructs_mainnet_helius_endpoint(self):
        self.assertEqual(
            self.endpoint_with_env({"HELIUS_API_KEY": "test-key"}),
            "https://mainnet.helius-rpc.com/?api-key=test-key",
        )

    def test_safe_print_redacts_helius_key(self):
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HELIUS_API_KEY": "secret-key-123"}, clear=True):
            with contextlib.redirect_stdout(buf):
                helius_common.safe_print({"url": "https://mainnet.helius-rpc.com/?api-key=secret-key-123"})
        self.assertNotIn("secret-key-123", buf.getvalue())
        self.assertIn("<HELIUS_API_KEY_REDACTED>", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
