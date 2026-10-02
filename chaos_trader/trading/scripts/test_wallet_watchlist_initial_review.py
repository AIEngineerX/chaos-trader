#!/usr/bin/env python3
from __future__ import annotations

import inspect
import unittest

import token_event_analyzer as analyzer
import wallet_watchlist_initial_review as review


class ReviewOutputSeamTests(unittest.TestCase):
    def test_default_review_file_is_one_the_token_analyzer_reads(self):
        parser_default = "wallet_initial_review"
        self.assertIn(f'"--out-prefix", default="{parser_default}"', inspect.getsource(review))
        written = review.REVIEW_ROOT / f"{parser_default}.json"
        self.assertIn(written, analyzer.WATCHLIST_FILES)
        self.assertEqual(review.REVIEW_ROOT, review.ALPHA_ROOT / "secondary")


if __name__ == "__main__":
    unittest.main()
