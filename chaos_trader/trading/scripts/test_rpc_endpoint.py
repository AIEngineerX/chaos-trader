import importlib
import os
import unittest
from unittest import mock

import helius_common


def reload():
    """Reload the module, then stub load_env so CHAOS_HOME/.env on the host cannot leak into a test."""
    module = importlib.reload(helius_common)
    module.load_env = lambda *args, **kwargs: None
    return module


class RpcEndpointTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(helius_common)  # undo the load_env stub

    def _env(self, **kw):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("SOLANA_", "HELIUS_", "CHAOS_ALLOW"))}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_solana_rpc_url_is_used_verbatim(self):
        with self._env(SOLANA_RPC_URL="https://8.8.8.8/x"):
            self.assertEqual(reload().rpc_endpoint(), "https://8.8.8.8/x")

    def test_helius_key_alone_builds_helius_url(self):
        with self._env(HELIUS_API_KEY="abc"):
            self.assertEqual(reload().rpc_endpoint(), "https://mainnet.helius-rpc.com/?api-key=abc")

    def test_solana_rpc_url_beats_helius_key(self):
        with self._env(SOLANA_RPC_URL="https://8.8.8.8", HELIUS_API_KEY="abc"):
            self.assertEqual(reload().rpc_endpoint(), "https://8.8.8.8")

    def test_http_scheme_is_rejected_naming_the_variable(self):
        with self._env(SOLANA_RPC_URL="http://rpc.example.com"):
            with self.assertRaises(SystemExit) as cm:
                reload().rpc_endpoint()
            self.assertIn("SOLANA_RPC_URL", str(cm.exception))

    def test_http_is_rejected_even_with_private_rpc_flag(self):
        with self._env(SOLANA_RPC_URL="http://127.0.0.1:8899", CHAOS_ALLOW_PRIVATE_RPC="1"):
            with self.assertRaises(SystemExit) as cm:
                reload().rpc_endpoint()
            self.assertIn("SOLANA_RPC_URL", str(cm.exception))
            self.assertIn("https", str(cm.exception))

    def test_private_https_host_is_accepted_with_flag(self):
        with self._env(SOLANA_RPC_URL="https://127.0.0.1:8899", CHAOS_ALLOW_PRIVATE_RPC="1"):
            self.assertEqual(reload().rpc_endpoint(), "https://127.0.0.1:8899")

    def test_private_host_is_rejected(self):
        with self._env(SOLANA_RPC_URL="https://10.0.0.5/"):
            with self.assertRaises(SystemExit) as cm:
                reload().rpc_endpoint()
            self.assertIn("SOLANA_RPC_URL", str(cm.exception))

    def test_nothing_set_names_both_variables(self):
        with self._env():
            with self.assertRaises(SystemExit) as cm:
                reload().rpc_endpoint()
            self.assertIn("SOLANA_RPC_URL", str(cm.exception))
            self.assertIn("HELIUS_API_KEY", str(cm.exception))

    def test_wallet_api_key_missing_names_variable(self):
        with self._env(SOLANA_RPC_URL="https://rpc.example.com"):
            with self.assertRaises(SystemExit) as cm:
                reload().wallet_api_key()
            self.assertIn("HELIUS_API_KEY", str(cm.exception))
            self.assertIn("wallet", str(cm.exception).lower())


if __name__ == "__main__":
    unittest.main()
