import io
import os
import sqlite3
import unittest
import urllib.error
from unittest import mock

import helius_common
import mint_cluster_query
import smart_wallet_tracker as swt

MINT = "So11111111111111111111111111111111111111112"


class UnresolvedConcentrationTests(unittest.TestCase):
    """A rate-limited holder read must read as unresolved concentration, not as a resolver-adjusted figure."""

    def test_degraded_holder_result_is_not_labelled_resolver_adjusted(self):
        def rate_limited(req, timeout=None):
            err = urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
            self.addCleanup(err.close)
            raise err

        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        swt.ensure_db(con)
        with mock.patch.dict(os.environ, {"SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com", "HELIUS_API_KEY": ""}), \
             mock.patch.object(helius_common.urllib.request, "urlopen", side_effect=rate_limited), \
             mock.patch.object(helius_common.time, "sleep"), \
             mock.patch.object(helius_common, "_host_is_private_or_reserved", return_value=False):  # no DNS lookup
            result = mint_cluster_query.score_mint(con, MINT)
        self.assertEqual(result["holder_resolution"]["holder_data"], "unavailable (rate limited)")
        self.assertEqual(result["metrics"]["concentration_source"], "none")
        self.assertIn("no_concentration_snapshot", result["negatives"])


if __name__ == "__main__":
    unittest.main()
