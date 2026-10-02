import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
ENVELOPE_KEYS = ["schema_version", "command", "generated_at", "data"]
NO_INGEST_REVIEW = "No ingest yet. Run chaos run chaos_alpha_elite_ingest first."
SWEEP_FAST = "The roster tape is empty until the ingest job has run. Run the ingest job, or use chaos sweep without --fast for the trending sweep."
JSON_EXCLUSIVE = "--json cannot be combined with --raw or --render-json; pick one."
JSON_VERBS = ("sweep", "token", "analyze", "strategy-paper", "smart-signals", "paper-report", "outcomes", "wallets")


def strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


class JsonContractTests(unittest.TestCase):
    """`--json` prints one envelope on every pipeline verb. Real subprocesses on a temp onboarded home, HOME redirected."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        user = Path(tmp.name) / "user"
        user.mkdir()
        drop = ("HELIUS_API_KEY", "SOLANA_RPC_URL", "CHAOS_PROFILE_HOME", "HERMES_HOME", "XAI_API_KEY", "X_SEARCH_PROVIDER", "HERMES_AGENT_SRC")
        self.env = {k: v for k, v in os.environ.items() if k not in drop}
        self.env.update(CHAOS_HOME=str(self.home), SOLANA_RPC_URL=PUBLIC_RPC, PYTHONIOENCODING="utf-8", HOME=str(user), USERPROFILE=str(user))
        p = self.run_chaos("onboard", "--rpc-url", PUBLIC_RPC, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def run_chaos(self, *args, timeout=120, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", *args], capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout)

    def envelope(self, p, command):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(list(out), ENVELOPE_KEYS)
        self.assertEqual(out["schema_version"], "1")
        self.assertEqual(out["command"], command)
        self.assertRegex(out["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(datetime.fromisoformat(out["generated_at"]).utcoffset(), timedelta(0))
        return out

    def test_sweep_fast_json_on_a_fresh_home_is_a_no_ingest_envelope(self):
        out = self.envelope(self.run_chaos("sweep", "--fast", "--json"), "sweep")
        self.assertEqual(out["data"], {"status": "no-ingest", "message": SWEEP_FAST})

    def test_sweep_fast_raw_stays_unwrapped_and_json_wraps_the_same_payload(self):
        self.assertEqual(self.run_chaos("wallets").returncode, 0)  # creates the smart-wallet schema, so the tape has a payload
        p = self.run_chaos("sweep", "--fast", "--raw")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        raw = json.loads(p.stdout)
        self.assertNotIn("schema_version", raw)
        self.assertEqual(raw["mode"], "alpha_tape_sweep")
        self.assertTrue(p.stdout.startswith('{\n  "boundary": '), "--raw keeps its sorted, indented form")
        out = self.envelope(self.run_chaos("sweep", "--fast", "--json"), "sweep")
        self.assertEqual(sorted(out["data"]), sorted(raw))
        self.assertEqual(out["data"]["mode"], "alpha_tape_sweep")

    def test_token_json_is_the_analyzer_payload_without_home_paths_or_keys(self):
        p = self.run_chaos("token", WSOL, "--no-x", "--json", timeout=480)
        out = self.envelope(p, "token")  # exit 0 even when the public RPC rate-limits the holder sample
        data = out["data"]
        for key in ("mint", "classification", "holder_data"):
            self.assertIn(key, data)
        self.assertEqual(data["mint"], WSOL)
        self.assertTrue(data["json_path"].startswith("$CHAOS_HOME"), data["json_path"])
        for home in (str(self.home), str(self.home.resolve()), self.home.resolve().as_posix()):
            self.assertEqual([s for s in strings(out) if home in s], [])
        self.assertNotIn("XAI_API_KEY=", p.stdout)

    def test_a_key_inside_the_payload_is_redacted(self):
        # The address being added doubles as the xAI key, so the key really sits in the payload.
        address = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
        p = self.run_chaos("wallets", "--add", address, "--tier", "A", "--json", extra_env={"XAI_API_KEY": address})
        out = self.envelope(p, "wallets --add")
        self.assertEqual(out["data"]["status"], "added")
        self.assertIn("<XAI_API_KEY_REDACTED>", out["data"]["message"])
        self.assertNotIn(address, p.stdout)

    def test_wallets_review_json_before_any_ingest_is_machine_readable(self):
        out = self.envelope(self.run_chaos("wallets", "--review", "--json"), "wallets --review")
        self.assertEqual(out["data"], {"status": "no-ingest", "message": NO_INGEST_REVIEW})

    def test_json_with_raw_or_render_json_exits_2_with_one_line(self):
        for args in (("sweep", "--fast", "--json", "--raw"), ("token", WSOL, "--json", "--render-json")):
            p = self.run_chaos(*args)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertEqual(p.stderr, JSON_EXCLUSIVE + "\n")
            self.assertEqual(p.stdout, "")

    def test_help_names_json_once_and_each_verb_help_shows_it(self):
        p = self.run_chaos("help")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(re.findall(r"--json\b", p.stdout)), 1)
        for verb in JSON_VERBS:
            p = self.run_chaos(verb, "--help")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("--json", p.stdout, verb)


if __name__ == "__main__":
    unittest.main()
