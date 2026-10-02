"""`scrub_home`: the home path leaves every string value and every dict key; a mint inside a value stays as it is."""
from __future__ import annotations

import os
import unittest

from json_contract import HOME_TOKEN, scrub_home

HOME = "C:\\Users\\Trader\\chaos" if os.name == "nt" else "/home/trader/chaos"
POSIX_FORM = "C:/Users/Trader/chaos" if os.name == "nt" else HOME
PREFIXES = sorted({HOME, POSIX_FORM}, key=len, reverse=True)
MINT = "2AKrTTxzD5Sj9JkWTbZYvrK1PLHVJ7FWZsJ8QpKqpump"
SEP = "\\" if os.name == "nt" else "/"


class ScrubHomeTests(unittest.TestCase):
    def test_a_home_path_used_as_a_dict_key_is_replaced(self):
        payload = {"reports": {f"{HOME}{SEP}trading{SEP}reports{SEP}a.json": {"path": f"{POSIX_FORM}/trading/b.json"}}}
        out = scrub_home(payload, PREFIXES)
        self.assertEqual(out, {"reports": {f"{HOME_TOKEN}{SEP}trading{SEP}reports{SEP}a.json": {"path": f"{HOME_TOKEN}/trading/b.json"}}})

    def test_values_that_only_contain_a_mint_are_untouched(self):
        payload = {"url": f"https://dexscreener.com/solana/{MINT}", MINT: [MINT, 3, None, True], "note": f"{MINT} at {HOME}{SEP}x"}
        out = scrub_home(payload, PREFIXES)
        self.assertEqual(out["url"], payload["url"])
        self.assertEqual(out[MINT], [MINT, 3, None, True])
        self.assertEqual(out["note"], f"{MINT} at {HOME_TOKEN}{SEP}x")

    def test_no_prefixes_leaves_the_payload_as_it_is(self):
        self.assertEqual(scrub_home({"a": "text"}, []), {"a": "text"})

    @unittest.skipUnless(os.name == "nt", "Windows paths ignore case")
    def test_on_windows_the_home_matches_in_any_case(self):
        payload = {"c:\\users\\trader\\CHAOS\\k": "C:/USERS/trader/Chaos/trading/x.json"}
        self.assertEqual(scrub_home(payload, PREFIXES), {f"{HOME_TOKEN}\\k": f"{HOME_TOKEN}/trading/x.json"})

    @unittest.skipIf(os.name == "nt", "POSIX paths keep case")
    def test_on_posix_a_differently_cased_path_is_another_path(self):
        self.assertEqual(scrub_home({"p": "/HOME/trader/chaos/x"}, PREFIXES), {"p": "/HOME/trader/chaos/x"})


if __name__ == "__main__":
    unittest.main()
