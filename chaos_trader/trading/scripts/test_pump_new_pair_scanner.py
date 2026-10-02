from __future__ import annotations

import unittest

from pump_new_pair_scanner import coin_liquidity_usd, coin_usd_market_cap, score_coin


class PumpNewPairScannerTests(unittest.TestCase):
    def test_uses_usd_market_cap_reported_by_pump(self) -> None:
        coin = {"usd_market_cap": 25_000, "market_cap": 999_999}
        self.assertEqual(coin_usd_market_cap(coin), 25_000)

    def test_real_sol_reserves_convert_to_usd_liquidity(self) -> None:
        coin = {"real_sol_reserves": 10_000_000_000}
        self.assertEqual(coin_liquidity_usd(coin, 150), 1_500)

    def test_real_liquidity_earns_score_bonus(self) -> None:
        base = {
            "usd_market_cap": 20_000,
            "created_timestamp": 1,
            "last_trade_timestamp": 1,
            "reply_count": 0,
            "real_sol_reserves": 0,
        }
        liquid = dict(base, real_sol_reserves=50_000_000_000)
        self.assertEqual(score_coin(liquid, 150) - score_coin(base, 150), 5)


if __name__ == "__main__":
    unittest.main()
