#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
EXPECTED_PROFILE = Path.home() / ".chaos-trader"


class RuntimePathTests(unittest.TestCase):
    def test_source_checkout_defaults_runtime_writes_to_chaos_profile(self):
        if not (SCRIPT_DIR.parents[2] / ".git").exists():
            self.skipTest("running from a deployed profile; the repo-path assertion only applies to source checkouts")
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        env.pop("CHAOS_HOME", None)
        env.pop("CHAOS_PROFILE_HOME", None)
        env["PYTHONPATH"] = f"{SCRIPT_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}"
        code = textwrap.dedent(
            """
            import json
            import alpha_paper_trade
            import alpha_tape
            import chaos_cmd
            import dexscreener_client
            import event_tape
            import paper_learning_report
            import secondary_evidence
            import signal_ledger
            import smart_wallet_tracker
            import tg_card_policy
            import wallet_ledger_mapper
            import token_event_analyzer
            import trending_token_sweep

            print(json.dumps({
                "alpha_paper_db": str(alpha_paper_trade.DB_PATH),
                "alpha_tape_db": str(alpha_tape.DB_PATH),
                "chaos_cmd": str(chaos_cmd.PROFILE_HOME),
                "dex_cache": str(dexscreener_client.CACHE_DIR),
                "event_tape": str(event_tape.DEFAULT_OUT),
                "secondary_evidence": str(secondary_evidence.SUMMARY_PATH),
                "signal_db": str(signal_ledger.DEFAULT_DB),
                "smart_wallet_db": str(smart_wallet_tracker.DEFAULT_DB),
                "smart_wallet_schema": str(smart_wallet_tracker.DEFAULT_SCHEMA),
                "wallet_ledger_mapper_db": str(wallet_ledger_mapper.DEFAULT_DB),
                "wallet_ledger_mapper_out": str(wallet_ledger_mapper.OUT_DIR),
                "paper_learning_db": str(paper_learning_report.PAPER_DB),
                "paper_learning_out": str(paper_learning_report.REPORT_DIR),
                "tg_roots": [str(p) for p in tg_card_policy.ALLOWED_ARTIFACT_ROOTS],
                "token_out": str(token_event_analyzer.OUT_ROOT),
                "sweep_out": str(trending_token_sweep.OUT_ROOT),
            }, sort_keys=True))
            """
        )
        proc = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, env=env, check=True)
        resolved: list[str] = []
        for value in json.loads(proc.stdout).values():
            resolved.extend(value if isinstance(value, list) else [value])
        self.assertTrue(any(p.startswith(str(EXPECTED_PROFILE)) for p in resolved), resolved)
        repo_trading = str(SCRIPT_DIR.parent)
        self.assertFalse(any(p.startswith(repo_trading) for p in resolved), resolved)


if __name__ == "__main__":
    unittest.main()
