import unittest
from unittest.mock import patch

import dexscreener_client


class DexscreenerBestMarketTests(unittest.TestCase):
    def test_resolver_prefers_migrated_active_pumpswap_pair(self):
        mint = "Mint111"
        pumpfun_pair = {
            "pairAddress": "BondingCurve111",
            "dexId": "pumpfun",
            "baseToken": {"address": mint, "name": "TEST", "symbol": "TEST"},
            "liquidity": None,
            "marketCap": 2_200,
            "volume": {"h1": 0, "m5": 0},
            "txns": {"m5": {"buys": 0, "sells": 0}, "h1": {"buys": 0, "sells": 0}},
            "priceChange": {"h1": None, "h24": 1000},
            "pairCreatedAt": 1,
        }
        pumpswap_pair = {
            "pairAddress": "PumpSwap111",
            "dexId": "pumpswap",
            "baseToken": {"address": mint, "name": "TEST", "symbol": "TEST"},
            "liquidity": {"usd": 44_000, "base": 1, "quote": 2},
            "marketCap": 305_000,
            "volume": {"h1": 90_000, "m5": 20_000},
            "txns": {"m5": {"buys": 100, "sells": 90}, "h1": {"buys": 500, "sells": 500}},
            "priceChange": {"h1": 25, "h24": 575},
            "pairCreatedAt": 2,
        }

        def fake_get_json(path, timeout=20):
            if path.startswith("/latest/dex/tokens/"):
                return {"pairs": [pumpfun_pair, pumpswap_pair]}
            if path.startswith("/token-pairs/v1/"):
                return [pumpfun_pair, pumpswap_pair]
            if path.startswith("/tokens/v1/"):
                return [pumpfun_pair]
            raise AssertionError(path)

        with patch.object(dexscreener_client, "get_json", side_effect=fake_get_json):
            out = dexscreener_client.resolve_best_token_market(mint, cache=False)

        self.assertEqual(out["best_pair"], "PumpSwap111")
        self.assertEqual(out["dex_id"], "pumpswap")
        self.assertEqual(out["liquidity_usd"], 44_000)
        self.assertEqual(out["pair_selection_reason"], "highest_active_liquidity")
        self.assertEqual(out["pair_count_total"], 2)


if __name__ == "__main__":
    unittest.main()
