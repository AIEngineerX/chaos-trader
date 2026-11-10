"""`chaos paper-positions` and `chaos size`: real subprocesses on a temp onboarded home, a real paper book on disk."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chaos_trader" / "trading" / "scripts"))
from chaos_paper_autopilot import SCHEMA  # noqa: E402

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
NO_PAPER_BOOK = 'No paper book yet. Run the paper tick first (see "Running it on a schedule" in the README).'
BOUNDARY = "Advisory + paper only. No wallet, signing, routing, or live execution."
DROP = ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME", "XAI_API_KEY", "X_SEARCH_PROVIDER", "HERMES_AGENT_SRC")
FIELDS = ["id", "mint", "symbol", "state", "opened_at_utc", "closed_at_utc", "entry_price", "entry_market_cap", "entry_liquidity_usd",
          "simulated_notional_usd", "stop_pct", "tp1_pct", "tp2_pct", "time_stop_minutes", "high_watermark_price", "remaining_pct",
          "tp1_done", "tp2_done", "realized_r", "exit_reason", "updated_at_utc"]
MINT_A = "A" * 44
MINT_B = "B" * 44
MINT_C = "C" * 44
# id, mint, symbol, state, opened, closed, entry, notional, stop, remaining, realized_r, exit_reason
BOOK = [
    ("p1", MINT_A, "AAA", "PAPER_CLOSED", "2026-01-01T00:00:00+00:00", "2026-01-01T02:00:00+00:00", 0.001, 100.0, 20.0, 0.0, -1.05, "stop"),
    ("p2", MINT_B, "BBB", "PAPER_TRIMMED", "2026-01-02T00:00:00+00:00", None, 0.002, 80.0, 25.0, 50.0, 0.4, None),
    ("p3", MINT_C, None, "PAPER_OPEN", "2026-01-03T00:00:00+00:00", None, 0.003, 60.0, 30.0, 100.0, 0.0, None),
]


def write_book(home: Path) -> Path:
    """A paper book with the autopilot's own schema and three positions: one closed, one trimmed, one open."""
    book = home / "trading" / "db" / "paper_autopilot.sqlite"
    book.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(book)) as con:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT INTO paper_positions (id,mint,symbol,state,opened_at_utc,closed_at_utc,entry_price,simulated_notional_usd,stop_pct,"
            "remaining_pct,realized_r,exit_reason,decision_json,updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'{\"private\": \"blob\"}',?)",
            [(*row, row[4]) for row in BOOK],
        )
        con.commit()
    return book


class OnboardedHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        user = self.tmp / "user"
        user.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in DROP}
        self.env.update(CHAOS_HOME=str(self.home), SOLANA_RPC_URL=PUBLIC_RPC, PYTHONIOENCODING="utf-8", HOME=str(user), USERPROFILE=str(user))
        p = self.run_chaos("onboard", "--rpc-url", PUBLIC_RPC, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def run_chaos(self, *args):
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", env=self.env, cwd=self.tmp, timeout=120)

    def data(self, p, command):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(list(out), ["schema_version", "command", "generated_at", "data"])
        self.assertEqual(out["command"], command)
        return out["data"]

    def files(self):
        return sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))


class PaperPositionsTests(OnboardedHome):
    def test_a_fresh_home_says_there_is_no_book_and_creates_none(self):
        before = self.files()
        p = self.run_chaos("paper-positions")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), NO_PAPER_BOOK)
        self.assertEqual(self.data(self.run_chaos("paper-positions", "--json"), "paper-positions"), {"status": "no-paper-book", "message": NO_PAPER_BOOK})
        self.assertEqual(self.files(), before)

    def test_open_is_the_default_and_lists_open_and_trimmed_newest_first(self):
        write_book(self.home)
        data = self.data(self.run_chaos("paper-positions", "--json"), "paper-positions")
        self.assertEqual(list(data), ["state", "count", "positions"])
        self.assertEqual((data["state"], data["count"]), ("open", 2))
        self.assertEqual([p["id"] for p in data["positions"]], ["p3", "p2"])
        for position in data["positions"]:
            self.assertEqual(list(position), FIELDS)
        trimmed = data["positions"][1]
        self.assertEqual((trimmed["mint"], trimmed["state"], trimmed["entry_price"], trimmed["simulated_notional_usd"], trimmed["stop_pct"],
                          trimmed["remaining_pct"], trimmed["realized_r"], trimmed["closed_at_utc"]),
                         (MINT_B, "PAPER_TRIMMED", 0.002, 80.0, 25.0, 50.0, 0.4, None))

    def test_closed_and_all_select_by_state_and_the_decision_blob_never_leaves(self):
        write_book(self.home)
        closed = self.data(self.run_chaos("paper-positions", "--state", "closed", "--json"), "paper-positions")
        self.assertEqual([(p["id"], p["exit_reason"], p["realized_r"]) for p in closed["positions"]], [("p1", "stop", -1.05)])
        p = self.run_chaos("paper-positions", "--state", "all", "--json")
        everything = self.data(p, "paper-positions")
        self.assertEqual((everything["count"], [x["id"] for x in everything["positions"]]), (3, ["p3", "p2", "p1"]))
        self.assertNotIn("blob", p.stdout)

    def test_limit_keeps_the_newest_and_count_stays_the_total(self):
        write_book(self.home)
        data = self.data(self.run_chaos("paper-positions", "--state", "all", "--limit", "1", "--json"), "paper-positions")
        self.assertEqual((data["count"], [p["id"] for p in data["positions"]]), (3, ["p3"]))
        p = self.run_chaos("paper-positions", "--limit", "0")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(p.stderr.strip(), "--limit must be 1 or more.")

    def test_the_card_has_one_line_per_position_and_the_boundary(self):
        write_book(self.home)
        p = self.run_chaos("paper-positions", "--state", "all", "--limit", "2")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "☄️ PAPER POSITIONS · all · 3 (newest 2 of 3)")
        self.assertEqual(lines[1], f"? {MINT_C} PAPER_OPEN · entry 0.003 · $60.0 · stop 30.0% · left 100% · realized +0.000R · opened 2026-01-03T00:00:00+00:00")
        self.assertEqual(lines[2], f"BBB {MINT_B} PAPER_TRIMMED · entry 0.002 · $80.0 · stop 25.0% · left 50% · realized +0.400R · opened 2026-01-02T00:00:00+00:00")
        self.assertEqual(lines[3:], [BOUNDARY])
        closed = self.run_chaos("paper-positions", "--state", "closed").stdout.splitlines()
        self.assertEqual(closed[0], "☄️ PAPER POSITIONS · closed · 1")
        self.assertTrue(closed[1].endswith("realized -1.050R · opened 2026-01-01T00:00:00+00:00 · closed 2026-01-01T02:00:00+00:00 (stop)"), closed[1])

    def test_reading_the_book_does_not_change_it(self):
        book = write_book(self.home)
        before = book.read_bytes()
        for state in ("open", "closed", "all"):
            self.assertEqual(self.run_chaos("paper-positions", "--state", state).returncode, 0)
        self.assertEqual(book.read_bytes(), before)

    def test_a_corrupt_book_exits_1_with_one_line(self):
        book = self.home / "trading" / "db" / "paper_autopilot.sqlite"
        book.write_bytes(bytes(range(256)) * 24)
        p = self.run_chaos("paper-positions")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertEqual(p.stderr.strip(), f"{book} is not a readable SQLite database. Move it aside and the next paper tick starts a new book.")


class SizeTests(OnboardedHome):
    def test_size_is_max_loss_over_stop_distance(self):
        data = self.data(self.run_chaos("size", "--max-loss", "25", "--stop-pct", "20", "--json"), "size")
        self.assertEqual(data, {"max_loss_usd": 25.0, "stop_pct": 20.0, "uncapped_notional_usd": 125.0, "liquidity_usd": None, "liquidity_bps": 50.0,
                                "liquidity_cap_usd": None, "notional_usd": 125.0, "loss_at_stop_usd": 25.0, "capped_by_liquidity": False,
                                "entry_price": None, "token_qty": None, "control_status": "analytical"})

    def test_liquidity_caps_the_size_and_entry_gives_the_token_quantity(self):
        data = self.data(self.run_chaos("size", "--max-loss", "25", "--stop-pct", "20", "--liquidity", "8000", "--entry", "0.002", "--json"), "size")
        self.assertEqual((data["liquidity_cap_usd"], data["notional_usd"], data["loss_at_stop_usd"], data["capped_by_liquidity"]), (40.0, 40.0, 8.0, True))
        self.assertAlmostEqual(data["token_qty"], 20000.0)
        roomy = self.data(self.run_chaos("size", "--max-loss", "25", "--stop-pct", "20", "--liquidity", "100000", "--liquidity-bps", "100", "--json"), "size")
        self.assertEqual((roomy["liquidity_cap_usd"], roomy["notional_usd"], roomy["capped_by_liquidity"]), (1000.0, 125.0, False))

    def test_the_card_says_it_is_arithmetic_and_carries_the_boundary(self):
        before = self.files()
        p = self.run_chaos("size", "--max-loss", "25", "--stop-pct", "20", "--liquidity", "8000")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.splitlines(), [
            "☄️ RISK SIZE (arithmetic only; no fees, no slippage, no market read)",
            "Max loss $25 at a 20% stop: notional $125",
            "Liquidity cap: $40 (50 bps of $8000)",
            "Size: $40 · loss at stop $8 · capped by liquidity",
            "Control status: analytical",
            BOUNDARY,
        ])
        self.assertEqual(self.files(), before)

    def test_values_that_make_no_size_exit_1_with_one_line(self):
        cases = [(("--max-loss", "0", "--stop-pct", "20"), "--max-loss and --liquidity-bps must be above 0, and --stop-pct above 0 and at most 100."),
                 (("--max-loss", "25", "--stop-pct", "0"), "--max-loss and --liquidity-bps must be above 0, and --stop-pct above 0 and at most 100."),
                 (("--max-loss", "25", "--stop-pct", "101"), "--max-loss and --liquidity-bps must be above 0, and --stop-pct above 0 and at most 100."),
                 (("--max-loss", "25", "--stop-pct", "20", "--liquidity", "-1"), "--liquidity and --entry must be above 0 when given."),
                 (("--max-loss", "25", "--stop-pct", "20", "--entry", "0"), "--liquidity and --entry must be above 0 when given.")]
        for args, line in cases:
            p = self.run_chaos("size", *args)
            self.assertEqual(p.returncode, 1, args)
            self.assertEqual((p.stdout, p.stderr.strip()), ("", line), args)


if __name__ == "__main__":
    unittest.main()
