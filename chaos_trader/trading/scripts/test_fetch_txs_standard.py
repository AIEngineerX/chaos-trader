import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr
from unittest import mock
import smart_wallet_tracker as swt


_sleep_patch = mock.patch.object(swt.time, "sleep")


def setUpModule():
    _sleep_patch.start()  # the real call gap is 0.25 s per transaction; nothing here needs it


def tearDownModule():
    _sleep_patch.stop()


def fake_rpc_factory(sigs_per_page, tx_by_sig):
    calls = []
    def rpc(method, params=None, **kw):
        calls.append((method, params))
        if method == "getSignaturesForAddress":
            before = (params[1] or {}).get("before")
            page = sigs_per_page.get(before, [])
            return [{"signature": s} for s in page]
        if method == "getTransaction":
            return tx_by_sig.get(params[0])
        raise AssertionError(method)
    return rpc, calls


class FetchTxsStandardTests(unittest.TestCase):
    def test_pages_until_limit_and_skips_null_transactions(self):
        sigs = {None: ["s1", "s2"], "s2": ["s3", "s4"], "s4": []}
        txs = {"s1": {"meta": {}, "transaction": {}, "blockTime": 1}, "s2": None,
               "s3": {"meta": {}, "transaction": {}, "blockTime": 3}, "s4": {"meta": {}, "transaction": {}, "blockTime": 4}}
        rpc, calls = fake_rpc_factory(sigs, txs)
        stats = {}
        out = swt.fetch_txs_standard("W", limit=3, pages=5, rpc=rpc, stats=stats)
        self.assertEqual([t["blockTime"] for t in out], [1, 3, 4])
        self.assertEqual(stats, {"null_transactions": 1})
        self.assertEqual(sum(1 for m, _ in calls if m == "getSignaturesForAddress"), 2)
        self.assertTrue(all(p[1].get("encoding") == "jsonParsed" and p[1].get("maxSupportedTransactionVersion") == 1
                            for m, p in calls if m == "getTransaction"))

    def test_signature_page_size_capped_at_100(self):
        rpc, calls = fake_rpc_factory({None: []}, {})
        swt.fetch_txs_standard("W", limit=500, pages=1, rpc=rpc)
        self.assertEqual(calls[0][1][1]["limit"], 100)

    def test_source_id_follows_endpoint(self):
        with mock.patch("smart_wallet_tracker.is_helius_endpoint", return_value=True):
            self.assertEqual(swt.current_source_id(), "helius_rpc")
        with mock.patch("smart_wallet_tracker.is_helius_endpoint", return_value=False):
            self.assertEqual(swt.current_source_id(), "solana_rpc")

    def test_standard_rows_carry_fetch_marker(self):
        rpc, _ = fake_rpc_factory({None: ["s1"]}, {"s1": {"meta": {}, "transaction": {}, "blockTime": 1}})
        out = swt.fetch_txs_standard("W", limit=1, pages=1, rpc=rpc)
        self.assertEqual(out[0]["_fetch"], "standard_rpc")


WALLET = "Wa11etStandardRpcTest1111111111111111111111"
MINT = "MintStandardRpcTest11111111111111111111111"


def parsed_swap(sig, block_time, sol_pre, sol_post, tok_pre, tok_post):
    """A getTransaction jsonParsed result for a wallet-signed swap of MINT against native SOL."""
    def bal(raw):
        return [] if raw is None else [{"accountIndex": 1, "mint": MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(raw), "decimals": 6}}]
    return {
        "blockTime": block_time,
        "slot": 1,
        "meta": {"err": None, "fee": 5000, "preBalances": [sol_pre, 2039280], "postBalances": [sol_post, 2039280],
                 "preTokenBalances": bal(tok_pre), "postTokenBalances": bal(tok_post), "innerInstructions": []},
        "transaction": {"signatures": [sig], "message": {"accountKeys": [
            {"pubkey": WALLET, "signer": True, "source": "transaction", "writable": True},
            {"pubkey": "TokenAcct1111111111111111111111111111111111", "signer": False, "source": "transaction", "writable": True},
        ], "instructions": []}},
        "version": 0,
    }


class StandardPathIntegrationTests(unittest.TestCase):
    """Real classify_tx, schema, insert and position rebuild; only the RPC endpoint is faked."""

    def standard_rpc(self):
        buy = parsed_swap("sigBuy", 1785370000, 2_000_000_000, 1_499_995_000, None, 1_000_000)
        sell = parsed_swap("sigSell", 1785370600, 1_499_995_000, 2_299_990_000, 1_000_000, 0)
        # Newest first, as getSignaturesForAddress returns them.
        return fake_rpc_factory({None: ["sigSell", "sigBuy"], "sigBuy": []}, {"sigSell": sell, "sigBuy": buy})

    def test_fetch_txs_dispatches_to_standard_rpc_off_helius(self):
        rpc, calls = self.standard_rpc()
        with mock.patch.object(swt, "is_helius_endpoint", return_value=False), mock.patch.object(swt, "rpc_request", side_effect=rpc):
            out = swt.fetch_txs(WALLET, 10, 1)
        self.assertEqual([swt.tx_sig(t) for t in out], ["sigSell", "sigBuy"])
        self.assertNotIn("getTransactionsForAddress", [m for m, _ in calls])

    def test_classify_tx_copies_fetch_marker_into_metadata(self):
        tx = parsed_swap("sigBuy", 1785370000, 2_000_000_000, 1_499_995_000, None, 1_000_000)
        self.assertNotIn("fetch", swt.classify_tx(WALLET, tx)[0]["metadata"])
        tx["_fetch"] = "standard_rpc"
        rows = swt.classify_tx(WALLET, tx)
        self.assertEqual([(r["event_type"], r["mint"]) for r in rows], [("buy", MINT)])
        self.assertEqual(rows[0]["metadata"]["fetch"], "standard_rpc")

    def test_enrich_wallet_off_helius_writes_solana_rpc_rows_and_skips_wallet_api(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys=ON")
        swt.ensure_db(con)
        rpc, _ = self.standard_rpc()
        err = io.StringIO()
        with mock.patch.object(swt, "is_helius_endpoint", return_value=False), \
             mock.patch.object(swt, "rpc_request", side_effect=rpc), \
             mock.patch.object(swt, "wallet_get", side_effect=AssertionError("Wallet API must not be called off Helius")), \
             redirect_stderr(err):
            result = swt.enrich_wallet(con, WALLET, 10, 1, include_api=True)
        self.assertEqual(1, len([ln for ln in err.getvalue().splitlines() if ln.strip()]))
        self.assertIn("Wallet API skipped", err.getvalue())
        self.assertEqual(result["events"], {"buy": 1, "sell": 1})
        self.assertEqual(result["closed_positions"], 1)
        self.assertEqual(result["null_transactions"], 0)
        counts = json.loads(con.execute("SELECT row_counts_json FROM ingestion_runs WHERE run_id=?", (result["run_id"],)).fetchone()[0])
        self.assertEqual(counts["null_transactions"], 0)
        events = con.execute("SELECT source_id, metadata_json FROM wallet_token_events WHERE wallet=?", (WALLET,)).fetchall()
        self.assertEqual({row[0] for row in events}, {"solana_rpc"})
        self.assertEqual({json.loads(row[1])["fetch"] for row in events}, {"standard_rpc"})
        self.assertEqual(con.execute("SELECT source_id FROM ingestion_runs WHERE run_id=?", (result["run_id"],)).fetchone()[0], "solana_rpc")
        self.assertEqual(con.execute("SELECT source_id FROM wallet_scores WHERE wallet=?", (WALLET,)).fetchone()[0], "solana_rpc")
        self.assertAlmostEqual(con.execute("SELECT realized_pnl_sol FROM positions WHERE wallet=? AND mint=?", (WALLET, MINT)).fetchone()[0], 0.29999, places=6)

    def test_re_enrich_after_provider_switch_replaces_rows_instead_of_doubling(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys=ON")
        swt.ensure_db(con)
        rpc, _ = self.standard_rpc()
        with mock.patch.object(swt, "is_helius_endpoint", return_value=False), mock.patch.object(swt, "rpc_request", side_effect=rpc):
            swt.enrich_wallet(con, WALLET, 10, 1, include_api=False)
        con.execute("UPDATE wallet_token_events SET source_id='helius_rpc' WHERE wallet=?", (WALLET,))
        con.commit()
        with mock.patch.object(swt, "is_helius_endpoint", return_value=False), mock.patch.object(swt, "rpc_request", side_effect=rpc):
            swt.enrich_wallet(con, WALLET, 10, 1, include_api=False)
        rows = con.execute("SELECT source_id, COUNT(*) FROM wallet_token_events WHERE wallet=? GROUP BY 1", (WALLET,)).fetchall()
        self.assertEqual(rows, [("solana_rpc", 2)])


if __name__ == "__main__":
    unittest.main()
