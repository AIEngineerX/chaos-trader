#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import chaos_cmd
import token_event_analyzer as analyzer
from token_event_analyzer import attach_gmgn_enrichment

MINT = "So11111111111111111111111111111111111111112"


class AnalyzerAttachmentTests(unittest.TestCase):
    def base_result(self):
        return {
            "market": {"price_usd": 1.0, "liquidity_usd": 100000.0},
            "classification": {"label": "watch"},
            "gate": {"action": "hold"},
            "entry_gate": {"allowed": False},
            "position_context": {"state": "flat"},
            "fact_grade": {"grade": "B"},
            "token_scan": {"ok": True},
        }

    def test_enabled_attachment_preserves_primary_pipeline_fields(self):
        result = self.base_result()
        primary_before = copy.deepcopy(result)

        def fetcher(mint):
            self.assertEqual(mint, MINT)
            return {
                "available": True,
                "source": "gmgn",
                "sources": {"security": {"ok": True, "data": {"rug_ratio": 0.2}}},
            }

        attach_gmgn_enrichment(result, MINT, enabled=True, fetcher=fetcher)
        for key, value in primary_before.items():
            self.assertEqual(result[key], value, key)
        self.assertTrue(result["gmgn_enabled"])
        self.assertEqual(result["gmgn"]["source"], "gmgn")

    def test_disabled_attachment_never_calls_provider(self):
        result = self.base_result()

        def forbidden(_mint):
            raise AssertionError("provider must not be called")

        attach_gmgn_enrichment(result, MINT, enabled=False, fetcher=forbidden)
        self.assertFalse(result["gmgn_enabled"])
        self.assertNotIn("gmgn", result)

    def test_provider_failure_is_observable_and_nonfatal(self):
        result = self.base_result()

        def broken(_mint):
            raise RuntimeError("provider unavailable")

        attach_gmgn_enrichment(result, MINT, enabled=True, fetcher=broken)
        self.assertFalse(result["gmgn"]["available"])
        self.assertEqual(result["gmgn"]["errors"][0]["kind"], "adapter_failure")
        self.assertNotIn("provider unavailable", json.dumps(result["gmgn"]))
        self.assertEqual(result["classification"], {"label": "watch"})

    def test_phase1_payload_is_attached_after_ledger_and_artifact_writes(self):
        persisted = {}

        def record_primary(result, **_kwargs):
            persisted["ledger"] = copy.deepcopy(result)
            return {"ok": True}

        gmgn_payload = {"available": True, "source": "gmgn", "data": {"untrusted": True}}
        fixed_returns = {
            "dex_summary": ({"ok": True, "pair_count": 0, "summary": {}}, None),
            "resolve_best_token_market": {"symbol": "TEST"},
            "extract_wallet_touch": {},
            "owner_wallet_exposure": {},
            "classify": {},
            "compact_secondary_evidence": {},
            "classify_mode": {},
            "classify_gate": {},
            "normalize_entry_gate": {},
            "classify_social_catalyst": {},
            "classify_flow_conversion": {},
            "analyze_position_context": {},
            "enrich_wallet_timing": {},
            "track_token_delta": {},
            "fact_grade": {},
            "render_markdown": "primary only\n",
            "compact_card": "primary card",
            "query_token_bundle": gmgn_payload,
        }
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for name, value in fixed_returns.items():
                stack.enter_context(patch.object(analyzer, name, return_value=value))
            stack.enter_context(patch.object(analyzer, "run_json", side_effect=[({}, None), ({}, None)]))
            stack.enter_context(patch.object(analyzer, "record_signal", side_effect=record_primary))
            returned = analyzer.analyze(
                MINT,
                tx_limit=1,
                x_enabled=False,
                x_days=1,
                gmgn_enabled=True,
                out_dir=Path(tmp),
            )

            artifact = json.loads(Path(returned["json_path"]).read_text(encoding="utf-8"))

        self.assertNotIn("gmgn", persisted["ledger"])
        self.assertNotIn("gmgn_enabled", persisted["ledger"])
        self.assertNotIn("gmgn", artifact)
        self.assertNotIn("gmgn_enabled", artifact)
        self.assertEqual(returned["gmgn"], gmgn_payload)


class RouterForwardingTests(unittest.TestCase):
    def test_analyze_gmgn_flag_is_forwarded_to_readonly_analyzer(self):
        args = argparse.Namespace(
            mint=MINT,
            tx_limit=20,
            x_days=2,
            default_x=True,
            with_x=False,
            no_x=True,
            gmgn=True,
            timeout=30,
            render_json=True,
        )
        payload = {"ok": True}
        descriptor = {"text": "ok"}
        with patch("chaos_cmd.run_raw", return_value=payload) as run_raw, patch(
            "chaos_cmd.token_render_descriptor", return_value=descriptor
        ), redirect_stdout(io.StringIO()):
            chaos_cmd.cmd_analyze(args)
        script_args = run_raw.call_args.args[0]
        self.assertIn("--gmgn", script_args)
        self.assertNotIn("--x", script_args)


if __name__ == "__main__":
    unittest.main()
