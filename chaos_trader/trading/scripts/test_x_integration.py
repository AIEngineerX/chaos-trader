#!/usr/bin/env python3
"""X evidence end to end. The real analyzer, paper engine, `chaos` command router and paper
autopilot run on a fresh CHAOS_HOME; only the HTTP boundary is replaced, urllib.request.urlopen and
no_redirect.open_no_redirect (fixtures/xai_http_fake.py), in-process and in every child Python
process. No network.

Owner decisions pinned here: D1 an unavailable, degraded or citation-free X answer is no
evidence; D2 the X budget unit is charged only when a request actually went out; D3 `--with-x`
with no provider behaves exactly like X off, plus one stderr notice.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import token_event_analyzer as analyzer  # noqa: E402
import x_provider  # noqa: E402
from chaos_paper_autopilot import PaperAutopilotRunner  # noqa: E402
from strategy_paper_engine import decide  # noqa: E402
from test_chaos_paper_autopilot import MINT, config_for, insert_candidate, seed_wallet_event_db  # noqa: E402
from test_x_provider import isolated_tools_import, write_broken_hermes_tree  # noqa: E402

FAKE_PATH = SCRIPT_DIR / "fixtures" / "xai_http_fake.py"
_spec = importlib.util.spec_from_file_location("xai_http_fake", FAKE_PATH)
FAKE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FAKE)

NOTICE = "X search: no provider configured (set XAI_API_KEY or HERMES_AGENT_SRC)"
# Everything that could point a child at a real key, RPC, Hermes tree or another home.
CLEARED = (
    "HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME", "HERMES_AGENT_SRC",
    "X_SEARCH_PROVIDER", "XAI_API_KEY", "X_SEARCH_MODEL", "X_SEARCH_REASONING_EFFORT",
    "X_SEARCH_TIMEOUT_SECONDS", "X_SEARCH_RETRIES", "GMGN_API_KEY", "GMGN_CLI", "PYTHONPATH", "CHAOS_PYTHON",
)
TEST_KEY = "xai-test-key"
XAI = {"X_SEARCH_PROVIDER": "xai", "XAI_API_KEY": TEST_KEY}
NO_PROVIDER = {"X_SEARCH_PROVIDER": "none"}


@contextlib.contextmanager
def fake_http(fake):
    """Replace both HTTP boundaries in-process: urlopen for public reads, open_no_redirect for Bearer calls."""
    with mock.patch("urllib.request.urlopen", fake), mock.patch("no_redirect.open_no_redirect", fake):
        yield


class Sandbox:
    """A fresh CHAOS_HOME, with the HTTP fake loaded into every child Python through sitecustomize."""

    def __init__(self, root: Path, xai: str = "fixture"):
        self.root = root
        self.home = root / "home"
        for sub in ("trading/db", "trading/reports", "trading/alpha"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.log = root / "http.log"
        self.log.write_text("", encoding="utf-8")
        self.config = root / "http.json"
        self.config.write_text(json.dumps({"xai": xai, "log": str(self.log)}), encoding="utf-8")
        self.site = root / "site"
        self.site.mkdir()
        fake_dir = str(FAKE_PATH.parent)
        (self.site / "sitecustomize.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {fake_dir!r})\n"
            "import xai_http_fake\n"
            f"sys.path.remove({fake_dir!r})\n"
            f"xai_http_fake.install({str(self.config)!r})\n",
            encoding="utf-8",
        )

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update({"CHAOS_HOME": str(self.home), "PYTHONPATH": str(self.site), "PYTHONIOENCODING": "utf-8", "X_SEARCH_RETRIES": "0"})
        env.update(extra or {})
        return env

    def run(self, args: list[str], extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args], cwd=SCRIPT_DIR, env=self.env(extra), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
        )

    def urls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def xai_requests(self) -> int:
        return sum(1 for url in self.urls() if url == FAKE.XAI_URL)

    def ledger_x_citation_counts(self) -> list[int]:
        con = sqlite3.connect(self.home / "trading" / "db" / "signal_ledger.sqlite")
        try:
            return [row[0] for row in con.execute("SELECT x_citation_count FROM signals ORDER BY id")]
        finally:
            con.close()


def analyze_in_sandbox(case: unittest.TestCase, xai: str, *flags: str, extra: dict[str, str] | None = None) -> tuple[dict, Sandbox, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    box = Sandbox(Path(tmp.name), xai)
    proc = box.run(["token_event_analyzer.py", MINT, "--tx-limit", "1", "--x-days", "1", "--raw", *flags], extra)
    case.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
    return json.loads(proc.stdout), box, tmp


def verdict(payload: dict) -> dict:
    """Everything an X answer could move: the legacy classification, the gates, the catalyst and the
    fact grade. The pair age is wall-clock time since the canned pair was created, so it is dropped."""
    picked = {key: payload.get(key) for key in ("classification", "gate", "entry_gate", "social_catalyst", "flow_conversion", "position_context", "fact_grade", "mode_context")}
    picked["mode_context"] = {k: v for k, v in picked["mode_context"].items() if k != "pair_age_seconds"}
    return picked


def paper(payload: dict) -> dict:
    decision = decide(payload)
    decision.pop("generated_at_utc")
    return decision


def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


class XEvidenceEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        box = Sandbox(Path(cls._tmp.name))
        proc = box.run(["token_event_analyzer.py", MINT, "--tx-limit", "1", "--x-days", "1", "--raw"])
        if proc.returncode != 0:
            raise AssertionError(proc.stderr[-2000:])
        cls.off = json.loads(proc.stdout)
        cls.off_requests = box.xai_requests()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_baseline_x_off_is_a_plain_study_read(self):
        self.assertEqual(self.off_requests, 0)
        self.assertFalse(self.off["x_enabled"])
        self.assertIsNone(self.off["x_attention"])
        self.assertIsNone(self.off["x_error"])
        self.assertFalse(self.off["x_request_made"])
        self.assertEqual(self.off["classification"]["verdict"], "study")
        self.assertNotIn("X: no sourced evidence", self.off["card"])

    def test_with_x_and_no_provider_matches_x_off_byte_for_byte(self):
        """Review Focus 1 / D3: the decision JSON is identical, and stderr carries exactly one notice line."""
        outputs = {}
        for flag in ("--no-x", "--with-x"):
            with tempfile.TemporaryDirectory() as td:
                box = Sandbox(Path(td))
                proc = box.run(["chaos_cmd.py", "strategy-paper", MINT, "--tx-limit", "1", "--raw", flag], NO_PROVIDER)
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                self.assertEqual(box.xai_requests(), 0)
                decision = json.loads(proc.stdout)
                decision.pop("generated_at_utc")
                outputs[flag] = (canonical(decision), proc.stderr)
        self.assertEqual(outputs["--with-x"][0], outputs["--no-x"][0])
        self.assertEqual(outputs["--no-x"][1], "")
        self.assertEqual(outputs["--with-x"][1], NOTICE + "\n")

    def test_analyzer_x_flag_with_no_provider_is_x_off(self):
        payload, box, _ = analyze_in_sandbox(self, "fixture", "--x", extra=NO_PROVIDER)
        self.assertEqual(box.xai_requests(), 0)
        self.assertFalse(payload["x_enabled"])
        self.assertIsNone(payload["x_error"])
        self.assertEqual(canonical(verdict(payload)), canonical(verdict(self.off)))

    def test_http_429_is_no_evidence_and_leaves_the_verdict_unchanged(self):
        """Review Focus 2: unavailable, verdict and paper decision as with X off (budget: see the autopilot tests)."""
        payload, box, _ = analyze_in_sandbox(self, "http429", "--x", extra=XAI)
        self.assertEqual(box.xai_requests(), 1)
        self.assertTrue(payload["x_enabled"])
        self.assertTrue(payload["x_request_made"])
        self.assertIsNone(payload["x_attention"])
        self.assertTrue(payload["x_error"].startswith("xAI HTTP 429"), payload["x_error"])
        self.assertEqual(canonical(verdict(payload)), canonical(verdict(self.off)))
        with_x, without_x = paper(payload), paper(self.off)
        self.assertEqual(with_x.pop("x"), {"enabled": True, "success": False, "citations": 0, "risk_confidence": "low"})
        without_x.pop("x")
        self.assertEqual(canonical(with_x), canonical(without_x))
        self.assertIn("X: no sourced evidence (xAI HTTP 429", payload["card"])
        self.assertNotIn(TEST_KEY, json.dumps(payload))

    def test_http_503_is_unavailable_and_leaves_the_verdict_unchanged(self):
        """A 5xx (retries off in the sandbox) is the same unavailable shape as a 429: made, no evidence, same verdict."""
        payload, box, _ = analyze_in_sandbox(self, "http503", "--x", extra=XAI)
        self.assertEqual(box.xai_requests(), 1)
        self.assertTrue(payload["x_request_made"])
        self.assertIsNone(payload["x_attention"])
        self.assertTrue(payload["x_error"].startswith("xAI HTTP 503"), payload["x_error"])
        self.assertEqual(canonical(verdict(payload)), canonical(verdict(self.off)))
        self.assertIn("X: no sourced evidence (xAI HTTP 503", payload["card"])
        self.assertNotIn(TEST_KEY, json.dumps(payload))

    def test_unknown_provider_prints_its_own_notice_and_is_x_off(self):
        outputs = {}
        for flag in ("--no-x", "--with-x"):
            with tempfile.TemporaryDirectory() as td:
                box = Sandbox(Path(td))
                proc = box.run(["chaos_cmd.py", "strategy-paper", MINT, "--tx-limit", "1", "--raw", flag], {"X_SEARCH_PROVIDER": "grok", "XAI_API_KEY": TEST_KEY})
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                self.assertEqual(box.xai_requests(), 0)
                decision = json.loads(proc.stdout)
                decision.pop("generated_at_utc")
                outputs[flag] = (canonical(decision), proc.stderr)
        self.assertEqual(outputs["--with-x"][0], outputs["--no-x"][0])
        self.assertEqual(outputs["--no-x"][1], "")
        self.assertEqual(outputs["--with-x"][1], "X search: unknown provider 'grok' (use hermes, xai, or none)\n")

    def test_autopilot_with_x_and_no_provider_prints_the_notice(self):
        with tempfile.TemporaryDirectory() as td:
            box = Sandbox(Path(td))
            config = Path(td) / "paper_autopilot.yaml"
            shutil.copy(SCRIPT_DIR.parent / "config" / "paper_autopilot.yaml", config)
            plain = box.run(["chaos_paper_autopilot.py", "--config", str(config), "--status"], NO_PROVIDER)
            asked = box.run(["chaos_paper_autopilot.py", "--config", str(config), "--status", "--with-x"], NO_PROVIDER)
        self.assertEqual(plain.returncode, 0, plain.stderr[-2000:])
        self.assertEqual(asked.returncode, 0, asked.stderr[-2000:])
        self.assertEqual(plain.stderr, "")
        self.assertEqual(asked.stderr, NOTICE + "\n")

    def test_sweep_x_flag_reports_the_provider_aware_decision(self):
        """trending_token_sweep.py --x with no provider reports x_enabled false and prints the notice."""
        with tempfile.TemporaryDirectory() as td:
            box = Sandbox(Path(td))
            out_dir = Path(td) / "sweep"
            proc = box.run(["trending_token_sweep.py", "--x", "--deep", "0", "--limit", "1", "--raw", "--out-dir", str(out_dir)], NO_PROVIDER)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertFalse(json.loads(proc.stdout)["x_enabled"])
            self.assertEqual(proc.stderr, NOTICE + "\n")
            keyed = box.run(["trending_token_sweep.py", "--x", "--deep", "0", "--limit", "1", "--raw", "--out-dir", str(out_dir)], XAI)
            self.assertEqual(keyed.returncode, 0, keyed.stderr[-2000:])
            self.assertTrue(json.loads(keyed.stdout)["x_enabled"])

    def test_catalyst_answer_without_citations_is_no_evidence(self):
        """Review Focus 3 / D1: the answer names a catalyst and a risk word, but cites nothing."""
        payload, box, _ = analyze_in_sandbox(self, "catalyst_uncited", "--x", extra=XAI)
        self.assertEqual(box.xai_requests(), 1)
        self.assertTrue(payload["x_request_made"])
        self.assertIsNone(payload["x_attention"])
        self.assertIn("no citations", payload["x_error"])
        cls = payload["classification"]
        self.assertEqual(cls["x_risk"]["flags"], [])
        self.assertFalse(cls["validation"]["credible_catalyst"])
        self.assertNotIn("X attention inconclusive", cls["risk_flags"])
        self.assertEqual(payload["social_catalyst"]["catalyst_type"], "none")
        self.assertEqual(canonical(verdict(payload)), canonical(verdict(self.off)))
        decision = paper(payload)
        self.assertNotIn("X enabled but catalyst/evidence not strong enough", decision["paper_plan"]["required_trigger"])
        self.assertEqual(decision["paper_plan"]["required_trigger"], paper(self.off)["paper_plan"]["required_trigger"])
        self.assertEqual(box.ledger_x_citation_counts(), [0])
        self.assertEqual(sum(1 for line in payload["card"].splitlines() if line.startswith("X: no sourced evidence (")), 1)

    def test_cited_answer_runs_the_keyword_scan_and_records_citations(self):
        payload, box, _ = analyze_in_sandbox(self, "catalyst_cited", "--x", extra=XAI)
        fixture = FAKE.xai_body("catalyst_cited")
        expected = len(fixture["citations"]) + sum(
            1 for item in fixture["output"] if item.get("type") == "message"
            for content in item["content"] for a in content.get("annotations", []) if a.get("type") == "url_citation"
        )
        self.assertEqual(expected, 5)
        self.assertEqual(box.xai_requests(), 1)
        self.assertTrue(payload["x_request_made"])
        self.assertIsNone(payload["x_error"])
        x = payload["x_attention"]
        self.assertEqual(x["answer"], FAKE.CATALYST_TEXT)
        self.assertEqual(len(x["citations"]) + len(x["inline_citations"]), expected)
        cls = payload["classification"]
        self.assertIn(f"X citations present ({expected})", cls["reasons"])
        self.assertIn("scam/rug/dev-sell claims on X — unverified", cls["x_risk"]["flags"])
        self.assertTrue(cls["validation"]["credible_catalyst"])
        self.assertEqual(payload["social_catalyst"]["catalyst_type"], "dev-stream")
        self.assertEqual(box.ledger_x_citation_counts(), [expected])
        self.assertEqual(paper(payload)["x"]["citations"], expected)
        self.assertNotIn("X: no sourced evidence", payload["card"])
        self.assertIn(f"X citations: {expected}", payload["card"])


class XBudgetTests(unittest.TestCase):
    """D2 through the real autopilot: the x_search unit is charged only when a request went out."""

    def charge(self, xai: str, extra: dict[str, str]) -> tuple[int, int, int, dict]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            box = Sandbox(root, xai)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(smart_db, run_id="elite-proof", observed_at=datetime.now(timezone.utc) - timedelta(minutes=5), wallets=2)
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            env = box.env(extra)
            with mock.patch.dict(os.environ, env), fake_http(FAKE.make_urlopen(box.config)):
                for name in CLEARED:
                    if name not in env:
                        os.environ.pop(name, None)
                with runner.connect() as con:
                    insert_candidate(con)
                    decision = runner.decide_candidate(con, MINT, use_x=True)
                    counters = dict(con.execute("SELECT counter,value FROM budget_counters").fetchall())
                    x_events = con.execute("SELECT COUNT(*) FROM events WHERE event_type='x_search'").fetchone()[0]
            return counters.get("x_search", 0), x_events, box.xai_requests(), decision

    def test_http_429_is_a_made_request_and_is_charged(self):
        charged, events, requests, decision = self.charge("http429", XAI)
        self.assertEqual((charged, events, requests), (1, 1, 1))
        self.assertEqual(decision["x"]["citations"], 0)

    def test_http_503_is_a_made_request_and_is_charged(self):
        charged, events, requests, decision = self.charge("http503", XAI)
        self.assertEqual((charged, events, requests), (1, 1, 1))
        self.assertEqual(decision["x"]["citations"], 0)

    def test_cited_answer_is_charged(self):
        charged, events, requests, decision = self.charge("catalyst_cited", XAI)
        self.assertEqual((charged, events, requests), (1, 1, 1))
        self.assertEqual(decision["x"]["citations"], 5)

    def test_no_provider_is_not_charged(self):
        charged, events, requests, _ = self.charge("fixture", NO_PROVIDER)
        self.assertEqual((charged, events, requests), (0, 0, 0))

    def test_xai_without_a_key_makes_no_request_and_is_not_charged(self):
        charged, events, requests, _ = self.charge("fixture", {"X_SEARCH_PROVIDER": "xai"})
        self.assertEqual((charged, events, requests), (0, 0, 0))

    def test_run_once_with_x_and_no_provider_runs_x_off(self):
        """D3 for the autopilot: --with-x with no provider analyses exactly as X off, no pending-X state."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            box = Sandbox(root)
            smart_db = root / "smart.sqlite"
            seed_wallet_event_db(smart_db, run_id="elite-proof", observed_at=datetime.now(timezone.utc) - timedelta(minutes=5), wallets=2)
            cfg = config_for(root)
            cfg.raw["paths"]["smart_wallet_sqlite"] = str(smart_db)
            runner = PaperAutopilotRunner(cfg)
            env = box.env(NO_PROVIDER)
            with mock.patch.dict(os.environ, env), fake_http(FAKE.make_urlopen(box.config)):
                for name in CLEARED:
                    if name not in env:
                        os.environ.pop(name, None)
                with runner.connect() as con:
                    insert_candidate(con)
                runner.run_once(limit=1, analyze_top=1, with_x=True)
                with runner.connect() as con:
                    deep = [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE event_type='deep_analyze'")]
            self.assertEqual(deep, [{"x_enabled": False}])
            self.assertEqual(box.xai_requests(), 0)


HUNTER_WALLETS = ("HunterSeedA" + "1" * 33, "HunterSeedB" + "1" * 33)


def seed_hunter_home(root: Path, home: Path) -> None:
    """What one wallet-hunter cycle reads: a wallet map naming two seed wallets, their recent buys of
    MINT in the home's smart-wallet ledger, and a paper config whose elite source has two fresh buys."""
    elite = root / "elite.sqlite"
    seed_wallet_event_db(elite, run_id="elite-proof", observed_at=datetime.now(timezone.utc) - timedelta(minutes=5), wallets=2)
    config = home / "trading" / "config" / "paper_autopilot.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    shipped = (SCRIPT_DIR.parent / "config" / "paper_autopilot.yaml").read_text(encoding="utf-8")
    config.write_text(shipped.replace("paths:\n", f"paths:\n  smart_wallet_sqlite: '{elite}'\n", 1), encoding="utf-8")
    maps = home / "trading" / "reports" / "wallet_maps"
    maps.mkdir(parents=True, exist_ok=True)
    seeds = [{"wallet": w, "strength_score": 60, "transfer_contamination": 0.1} for w in HUNTER_WALLETS]
    (maps / "wallet_ledger_map_20261001T000000Z.json").write_text(json.dumps({"top_strength": seeds}), encoding="utf-8")
    bought = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    con = sqlite3.connect(home / "trading" / "db" / "smart_wallets.sqlite")
    try:
        con.executescript((SCRIPT_DIR.parent / "schemas" / "smart_wallets_schema.sql").read_text(encoding="utf-8"))
        con.execute("INSERT INTO sources(source_id,source_type) VALUES('manual','manual')")
        con.execute("INSERT INTO tokens(mint) VALUES(?)", (MINT,))
        for wallet in HUNTER_WALLETS:
            con.execute("INSERT INTO wallets(address) VALUES(?)", (wallet,))
            # source 'manual', so the cycle's own enrichment (which clears on-chain rows) keeps them
            con.execute(
                "INSERT INTO wallet_token_events(wallet,mint,block_time_utc,event_type,side,source_id,confidence) VALUES(?,?,?,?,?,?,?)",
                (wallet, MINT, bought, "buy", "buy", "manual", "high"),
            )
        con.commit()
    finally:
        con.close()


class WalletHunterXTests(unittest.TestCase):
    """D3 for wallet_live_paper_hunter.py: --with-x with no provider is X off, with one notice."""

    def test_two_cycles_with_x_and_no_provider_run_x_off_and_keep_the_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            box = Sandbox(root)
            seed_hunter_home(root, box.home)
            proc = box.run(
                ["wallet_live_paper_hunter.py", "--max-cycles", "2", "--sleep-sec", "1", "--seed-limit", "2",
                 "--enrich-limit", "2", "--candidate-limit", "1", "--analyze-top", "1", "--with-x", "--raw"],
                {**NO_PROVIDER, "SOLANA_RPC_URL": FAKE.RPC_URL, "CHAOS_ALLOW_PRIVATE_RPC": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
            cycles = [json.loads(line) for line in proc.stdout.splitlines()[:2]]
            con = sqlite3.connect(box.home / "trading" / "db" / "paper_autopilot.sqlite")
            try:
                deep = [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE event_type='deep_analyze' AND mint=?", (MINT,))]
                x_checked = con.execute("SELECT x_checked FROM candidates WHERE mint=?", (MINT,)).fetchone()[0]
            finally:
                con.close()
            requests = box.xai_requests()
        self.assertEqual([c["analysis_candidates"] for c in cycles], [[MINT], []])  # cycle 2 inside the cooldown
        self.assertEqual(deep, [{"x_enabled": False}])
        self.assertEqual(x_checked, 0)  # as with X off: no X answer was ever asked for
        self.assertEqual(requests, 0)
        self.assertEqual([line for line in proc.stderr.splitlines() if line.startswith("X search:")], [NOTICE])


class XRuleTests(unittest.TestCase):
    """D1 and D2 on real x_provider results (only the HTTP boundary is replaced, for the HTTP cases)."""

    def search(self, env: dict[str, str], query: str = "q", **kwargs) -> dict:
        with mock.patch.dict(os.environ, {"X_SEARCH_RETRIES": "0", "HERMES_AGENT_SRC": "", "XAI_API_KEY": "", "X_SEARCH_PROVIDER": "", **env}):
            return x_provider.search(query, **kwargs)

    def test_request_made_only_when_a_request_went_out(self):
        with tempfile.TemporaryDirectory() as td:
            box = Sandbox(Path(td), "http429")
            with fake_http(FAKE.make_urlopen(box.config)):
                http_error = self.search(XAI)
        self.assertEqual(http_error["error_type"], "HTTPError")
        with fake_http(mock.Mock(side_effect=OSError("connection refused"))):
            transport = self.search(XAI)
        with tempfile.TemporaryDirectory() as src, isolated_tools_import():
            write_broken_hermes_tree(Path(src))
            hermes_syntax_error = self.search({"HERMES_AGENT_SRC": src})
        self.assertEqual(hermes_syntax_error["error_type"], "hermes_import")
        cases = {
            "malformed key": (self.search({"X_SEARCH_PROVIDER": "xai", "XAI_API_KEY": "xai-bad\nkey-0001"}), False),
            "hermes tool with a syntax error": (hermes_syntax_error, False),
            "http error": (http_error, True),
            "transport": (transport, True),
            "no provider": (self.search(NO_PROVIDER), False),
            "no key": (self.search({"X_SEARCH_PROVIDER": "xai"}), False),
            "bad dates": (self.search(XAI, from_date="2026-13-01"), False),
            "empty query": (self.search(XAI, query="  "), False),
        }
        for name, (result, made) in cases.items():
            with self.subTest(name):
                self.assertIs(analyzer.x_request_made(result), made)

    def test_only_a_sourced_answer_is_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            box = Sandbox(Path(td), "fixture")
            with fake_http(FAKE.make_urlopen(box.config)):
                cited = self.search(XAI, from_date="2026-09-29", to_date="2026-09-30")
            box.config.write_text(json.dumps({"xai": "catalyst_uncited", "log": str(box.log)}), encoding="utf-8")
            with fake_http(FAKE.make_urlopen(box.config)):
                degraded = self.search(XAI, from_date="2026-09-29", to_date="2026-09-30")
                uncited = self.search(XAI)
        self.assertTrue(degraded["degraded"])
        self.assertFalse(uncited["degraded"])
        subscription = {**cited, "citations": [], "inline_citations": [], "credential_source": "hermes", "credential_detail": "xai-oauth"}
        self.assertEqual(analyzer.x_evidence(cited), (True, ""))
        for name, result in {"degraded": degraded, "uncited": uncited, "subscription": subscription, "unavailable": self.search(NO_PROVIDER)}.items():
            with self.subTest(name):
                sourced, reason = analyzer.x_evidence(result)
                self.assertFalse(sourced)
                self.assertTrue(reason)
        self.assertEqual(analyzer.x_evidence(self.search(NO_PROVIDER))[1], "no provider")


class XProviderHomeEnvTests(unittest.TestCase):
    """XAI_API_KEY set only in CHAOS_HOME/.env (where the README says to put it) selects xai."""

    def test_status_reports_provider_from_home_env(self):
        with tempfile.TemporaryDirectory() as td:
            box = Sandbox(Path(td))
            proc = box.run(["chaos_status.py"])
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            status = json.loads(proc.stdout)
            self.assertEqual(status["x_provider"], "none")
            self.assertNotIn("x_search_tool_expected", status)
            (box.home / ".env").write_text(f"XAI_API_KEY={TEST_KEY}\n", encoding="utf-8")
            proc = box.run(["chaos_status.py"])
            self.assertEqual(json.loads(proc.stdout)["x_provider"], "xai")


if __name__ == "__main__":
    unittest.main()
