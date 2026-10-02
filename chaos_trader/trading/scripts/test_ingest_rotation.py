"""The bounded ingest rotates through the roster: a run reads never-ingested wallets first, then the oldest.

The real `run_ingest` and `ingest_one_wallet` write real `ingestion_runs` rows to a temp database built from the
shipped schema. A JSON-RPC server on loopback answers every wallet with an empty history. Replaced: the RPC URL,
the pipeline's clock (one minute per call), and, for the wallet a run is cut on, the transaction fetch, which
raises the way a killed process stops: mid-wallet, leaving its run row `running`.
"""
from __future__ import annotations

import functools
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import elite_wallet_pipeline as elite
import helius_common

WALLETS = ["A" * 31 + c for c in "BCDEFGHJK"]


class EmptyHistory(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": [] if req["method"] == "getSignaturesForAddress" else None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Cut(BaseException):
    """The time bound stopping the process in the middle of a wallet."""


class IngestRotationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "roster.json").write_text(json.dumps({"version": "rotation-test", "data_through": "2026-10-01",
                                                           "wallets": [{"address": w, "tier": "B"} for w in WALLETS]}), encoding="utf-8")
        self.roster = elite.load_roster(self.root / "roster.json")
        self.db = self.root / "smart_wallets.sqlite"
        server = HTTPServer(("127.0.0.1", 0), EmptyHistory)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        ticks = iter(range(10_000))
        start = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        clock = lambda: (start + timedelta(minutes=next(ticks))).isoformat(timespec="seconds")  # noqa: E731
        for patch in (mock.patch.object(helius_common, "rpc_endpoint", return_value=f"http://127.0.0.1:{server.server_address[1]}/"),
                      mock.patch.object(elite, "now_utc", side_effect=clock)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_once(self, *, cut_after: int | None = None) -> list[str]:
        """One ingest run; returns the wallets it started, in order. With `cut_after`, the wallet after that many is cut."""
        started: list[str] = []

        def ingest(con, wallet, **kwargs):
            started.append(wallet)
            if cut_after is not None and len(started) > cut_after:
                def killed(*_args):
                    raise Cut()
                return elite.ingest_one_wallet(con, wallet, fetch_func=killed, **kwargs)
            return elite.ingest_one_wallet(con, wallet, **kwargs)

        run = functools.partial(elite.run_ingest, self.roster, db_path=self.db, report_dir=self.root / "reports", ingest_func=ingest)
        if cut_after is None:
            self.assertEqual(run()["succeeded"], len(WALLETS))
        else:
            with self.assertRaises(Cut):
                run()
        return started

    def statuses(self) -> dict[str, list[str]]:
        con = elite.connect_db(self.db)
        try:
            rows = con.execute("SELECT json_extract(notes,'$.wallet'), status FROM ingestion_runs ORDER BY started_at").fetchall()
        finally:
            con.close()
        out: dict[str, list[str]] = {}
        for wallet, status in rows:
            out.setdefault(wallet, []).append(status)
        return out

    def test_a_run_cut_after_five_wallets_is_followed_by_one_that_starts_with_the_four_it_did_not_reach(self):
        self.assertEqual(self.run_once(cut_after=5), WALLETS[:6])
        statuses = self.statuses()
        self.assertEqual([statuses[w] for w in WALLETS[:5]], [["completed"]] * 5)
        self.assertEqual(statuses[WALLETS[5]], ["running"])  # the wallet the bound stopped mid-read
        self.assertEqual(self.run_once(), WALLETS[5:] + WALLETS[:5])

    def test_once_every_wallet_has_completed_the_next_run_starts_with_the_oldest(self):
        self.assertEqual(self.run_once(), WALLETS)
        self.assertEqual(self.run_once(cut_after=2), WALLETS[:3])
        # The first two were read again just now, the third was cut, so the oldest completed reads lead.
        self.assertEqual(self.run_once(), WALLETS[2:] + WALLETS[:2])
        self.assertEqual(self.run_once(), WALLETS[2:] + WALLETS[:2])


if __name__ == "__main__":
    unittest.main()
