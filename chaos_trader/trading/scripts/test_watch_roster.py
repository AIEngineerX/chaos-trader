#!/usr/bin/env python3
"""The home roster is a watch source, and the promoter's candidate and sensor verdicts are watch roles.

Each test writes real JSON files in its own temporary home. The analyzer's watch-file paths are
remapped into it, so no test reads or touches the home the analyzer resolved at import, which can
be a real one (CHAOS_HOME, CHAOS_PROFILE_HOME or HERMES_HOME).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import token_event_analyzer as analyzer
import wallet_position_timing

# The analyzer's own constants, captured at import before any test remaps them.
REAL_HOME = analyzer.PROFILE_HOME
REAL_ROSTER_PATH = analyzer.ROSTER_PATH
REAL_WATCHLIST_FILES = list(analyzer.WATCHLIST_FILES)
SEED_ROSTER = Path(analyzer.SCRIPT_DIR).parents[1] / "seed" / "roster.json"

W_A = "RosterTierAwa11et111111111111111111111111111"
W_B = "RosterTierBwa11et111111111111111111111111111"
W_C = "RosterTierCwa11et111111111111111111111111111"
W_X = "RosterTierXwa11et111111111111111111111111111"
W_PAD = "RosterPaddedAwa11et1111111111111111111111111"
W_STUDY = "StudyDeepWatchwa11et111111111111111111111111"


def roster_doc(*pairs: tuple[str, str]) -> dict:
    return {"version": "test-v1", "wallets": [{"address": a, "tier": t} for a, t in pairs]}


PROMOTER_SCORES = {"smart-wallet-candidate": 72.0, "cluster-sensor": 55.0, "study": 40.0, "low-priority": 25.0, "avoid": 10.0}


def promoter_row(wallet: str, verdict: str, hard_flags: tuple[str, ...] = ()) -> dict:
    """One row as smart_wallet_promoter.run() writes it: the wallet_metrics keys plus classify()'s verdict
    fields. A wallet with no actors row has actor_label None, so the verdict is the label the analyzer reads."""
    return {
        "wallet": wallet, "handle": None, "actor_label": None, "actor_confidence": None, "secondary": {}, "helius": None,
        "positions": 4, "trade_positions": 4, "clean_trade_positions": 4, "closed_positions": 4, "wins": 3, "losses": 1,
        "sample_realized_pnl_sol": 1.2, "median_hold_seconds": 600.0, "contamination_ratio": 0.0,
        "shared_overlap_edges": 0, "max_shared_mints": 0, "helius_transfer_edges": 0,
        "score": PROMOTER_SCORES[verdict], "verdict": verdict, "positives": [], "negatives": [], "hard_flags": list(hard_flags),
    }


def promotions_doc(rows: list[dict]) -> dict:
    """The file smart_wallet_promoter.run(write=True) writes to trading/alpha/smart_wallet_promotions.json."""
    return {
        "ok": True, "mode": "multi_source_smart_wallet_promoter", "count": len(rows),
        "summary": {"smart_wallet_candidates": 0, "cluster_sensors": 0, "study": 0, "avoid": 0, "discovery_queue": 0},
        "rows": rows, "discovery_queue": [],
        "discovery_queue_note": "never-scored wallets found via funding/transfer edges; enrich with smart_wallet_tracker.py --discovered N",
        "boundary": "research only; secondary evidence is historical prior, not source of truth",
    }


class WatchRosterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        files = [home / path.relative_to(REAL_HOME) for path in REAL_WATCHLIST_FILES]
        self.roster = home / REAL_ROSTER_PATH.relative_to(REAL_HOME)
        self.study = home / "trading" / "alpha" / "secondary" / "wallet_study_set.json"
        self.scores = home / "trading" / "alpha" / "secondary" / "wallet_scores.json"
        self.promotions = home / "trading" / "alpha" / "smart_wallet_promotions.json"
        self.assertIn(self.roster, files)
        for patch in (mock.patch.object(analyzer, "WATCHLIST_FILES", files), mock.patch.object(analyzer, "ROSTER_PATH", self.roster)):
            patch.start()
            self.addCleanup(patch.stop)
        analyzer._ROSTER_CACHE.clear()
        self.addCleanup(analyzer._ROSTER_CACHE.clear)

    def write(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_roster_is_the_home_config_file_not_the_package_seed(self):
        roster = REAL_HOME / "trading" / "config" / "roster.json"
        self.assertIn(roster, REAL_WATCHLIST_FILES)
        self.assertEqual(REAL_ROSTER_PATH, roster)
        self.assertNotIn(SEED_ROSTER, REAL_WATCHLIST_FILES)

    def test_roster_shape_gives_one_row_per_wallet_with_address_and_tier(self):
        rows = analyzer.rowsets_from_data(roster_doc((W_A, "A"), (W_C, "C")))
        self.assertEqual(rows, [{"address": W_A, "tier": "A"}, {"address": W_C, "tier": "C"}])

    def test_tiers_a_and_b_are_quality_c_is_scout_unknown_is_none(self):
        self.write(self.roster, roster_doc((W_A, "A"), (W_B, "B"), (W_C, "C"), (W_X, "X"), (W_PAD, " a ")))
        watches = analyzer.load_watch_wallets()
        self.assertEqual((watches[W_A]["kind"], watches[W_A]["role"], watches[W_A]["source"]), ("quality", "roster-A", "roster.json"))
        self.assertEqual((watches[W_B]["kind"], watches[W_B]["role"]), ("quality", "roster-B"))
        self.assertEqual((watches[W_C]["kind"], watches[W_C]["role"]), ("scout", "roster-C"))
        self.assertEqual((watches[W_PAD]["kind"], watches[W_PAD]["role"]), ("quality", "roster-A"))
        self.assertNotIn(W_X, watches)
        self.assertEqual(analyzer.wallet_role({"address": W_X, "tier": "X"}, "roster.json")[:2], (None, "roster-unknown"))

    def test_missing_roster_returns_the_other_sources_rows(self):
        self.assertFalse(self.roster.exists())
        self.write(self.study, {"wallets": [{"wallet": W_STUDY, "actor_label": "deep-watch"}]})
        watches = analyzer.load_watch_wallets()
        self.assertEqual(set(watches), {W_STUDY})
        self.assertEqual(watches[W_STUDY]["kind"], "quality")

    def test_package_seed_wallets_are_not_watch_wallets_without_a_home_roster(self):
        self.assertFalse(self.roster.exists())
        seed_wallets = {row["address"] for row in json.loads(SEED_ROSTER.read_text(encoding="utf-8"))["wallets"]}
        self.assertTrue(seed_wallets)
        self.assertFalse(seed_wallets & set(analyzer.load_watch_wallets()))

    def test_promoter_verdicts_map_candidate_quality_sensor_scout_rest_none(self):
        verdicts = ["smart-wallet-candidate", "cluster-sensor", "study", "low-priority", "avoid"]
        wallets = {v: f"Promoted{v.replace('-', '')}".ljust(44, "1") for v in verdicts}
        flagged = "PromotedFlaggedCandidate".ljust(44, "1")
        rows = [promoter_row(w, v) for v, w in wallets.items()]
        # The promoter turns a contaminated candidate into a hard-flagged cluster-sensor (smart_wallet_promoter.classify).
        rows.append(promoter_row(flagged, "cluster-sensor", ("transfer_contaminated",)))
        self.write(self.promotions, promotions_doc(rows))
        watches = analyzer.load_watch_wallets()
        self.assertEqual(watches[wallets["smart-wallet-candidate"]]["kind"], "quality")
        self.assertEqual(watches[wallets["cluster-sensor"]]["kind"], "scout")
        for verdict in ("study", "low-priority", "avoid"):
            self.assertNotIn(wallets[verdict], watches, verdict)
        self.assertNotIn(flagged, watches)

    def test_local_avoid_verdict_vetoes_a_roster_entry(self):
        self.write(self.roster, roster_doc((W_A, "A")))
        self.write(self.study, {"wallets": [{"wallet": W_A, "actor_label": "avoid"}]})
        self.assertNotIn(W_A, analyzer.load_watch_wallets())

    def test_hard_flagged_row_vetoes_a_roster_entry(self):
        self.write(self.roster, roster_doc((W_B, "B")))
        self.write(self.promotions, promotions_doc([promoter_row(W_B, "cluster-sensor", ("transfer_contaminated",))]))
        self.assertNotIn(W_B, analyzer.load_watch_wallets())

    def test_roster_tier_a_without_a_negative_row_stays_quality(self):
        self.write(self.roster, roster_doc((W_A, "A")))
        self.write(self.study, {"wallets": [{"wallet": W_STUDY, "actor_label": "avoid"}]})
        watches = analyzer.load_watch_wallets()
        self.assertEqual((watches[W_A]["kind"], watches[W_A]["role"], watches[W_A]["source"]), ("quality", "roster-A", "roster.json"))
        self.assertNotIn(W_STUDY, watches)

    def test_veto_removes_only_the_roster_entry_not_other_sources(self):
        self.write(self.roster, roster_doc((W_A, "A")))
        self.write(self.study, {"wallets": [{"wallet": W_A, "actor_label": "avoid"}]})
        self.write(self.scores, {"wallets": [{"wallet": W_A, "score": 55}]})
        watches = analyzer.load_watch_wallets()
        self.assertEqual((watches[W_A]["kind"], watches[W_A]["source"]), ("scout", "wallet_scores.json"))

    def test_padded_roster_address_still_matches_a_sampled_holder(self):
        self.write(self.roster, roster_doc((f"  {W_A} ", "A")))
        touch = analyzer.extract_wallet_touch({"token_accounts_summary": {"holder_sample": [{"owner": W_A, "amount": 5}]}}, None)
        self.assertEqual([(h["wallet"], h["kind"], h["role"]) for h in touch["watch_wallet_hits"]], [(W_A, "quality", "roster-A")])

    def test_padded_roster_address_is_still_vetoed_by_a_local_avoid(self):
        self.write(self.roster, roster_doc((f" {W_A}  ", "A")))
        self.write(self.study, {"wallets": [{"wallet": W_A, "actor_label": "avoid"}]})
        watches = analyzer.load_watch_wallets()
        self.assertNotIn(W_A, watches)
        self.assertNotIn(f" {W_A}  ", watches)

    def test_roster_is_read_once_across_two_mints(self):
        self.write(self.roster, roster_doc((W_A, "A"), (W_C, "C")))
        with mock.patch.object(analyzer, "read_roster", wraps=analyzer.read_roster) as counted:
            first = analyzer.extract_wallet_touch({"token_accounts_summary": {"holder_sample": [{"owner": W_A, "amount": 5}]}}, None)
            second = analyzer.extract_wallet_touch({"token_accounts_summary": {"holder_sample": [{"owner": W_C, "amount": 7}]}}, None)
        self.assertEqual(counted.call_count, 1)
        self.assertEqual([(h["wallet"], h["kind"]) for h in first["watch_wallet_hits"]], [(W_A, "quality")])
        self.assertEqual([(h["wallet"], h["kind"]) for h in second["watch_wallet_hits"]], [(W_C, "scout")])

    def test_seed_tier_a_wallet_in_a_holder_sample_adds_the_watch_score(self):
        self.roster.parent.mkdir(parents=True, exist_ok=True)
        self.roster.write_bytes(SEED_ROSTER.read_bytes())
        tier_a = next(w["address"] for w in json.loads(SEED_ROSTER.read_text(encoding="utf-8"))["wallets"] if w["tier"] == "A")
        scan = {"token_accounts_summary": {"holder_sample": [{"owner": tier_a, "amount": 5}, {"owner": W_X, "amount": 7}]}}
        wallet = analyzer.extract_wallet_touch(scan, None)
        self.assertEqual(wallet["watch_wallet_hit_count"], 1)
        hit = wallet["watch_wallet_hits"][0]
        self.assertEqual((hit["wallet"], hit["kind"], hit["role"]), (tier_a, "quality", "roster-A"))
        empty_scan = {"token_accounts_summary": {"holder_sample": [{"owner": W_X, "amount": 7}]}}
        with_hit = analyzer.classify({"wallet_timing": wallet, "token_scan": scan})
        without = analyzer.classify({"wallet_timing": analyzer.extract_wallet_touch(empty_scan, None), "token_scan": empty_scan})
        self.assertIn("watch wallet touched sample", with_hit["reasons"])
        self.assertEqual(with_hit["score"] - without["score"], 4)


class RosterHitTimingTests(unittest.TestCase):
    """The token read has no first-touch data for a roster wallet, so a held roster hit is not a transfer claim."""

    def timing(self, role: str, kind: str, signer_count: int) -> dict:
        hit = {"wallet": W_A, "label": "watch", "kind": kind, "role": role, "score": -999.0, "source": "roster.json"}
        existing = {
            "quality_wallet_hits": [hit] if kind == "quality" else [],
            "scout_wallet_hits": [hit] if kind == "scout" else [],
            "top_holders_sample": [{"owner": W_A, "amount": 1250.0}],
            "top_signers_sample": [{"wallet": W_A, "count": signer_count}] if signer_count else [],
        }
        return wallet_position_timing.enrich_wallet_timing(existing, {})

    def test_roster_a_holder_with_no_repeat_signing_is_unknown(self):
        out = self.timing("roster-A", "quality", 0)
        self.assertEqual([(r["status"], r["confidence"]) for r in out["wallet_timing"]], [("unknown", "low")])
        self.assertEqual(out["timing_label"], "timing unresolved")

    def test_roster_c_holder_signing_once_is_unknown(self):
        out = self.timing("roster-C", "scout", 1)
        self.assertEqual([(r["status"], r["confidence"]) for r in out["wallet_timing"]], [("unknown", "low")])

    def test_roster_a_holder_that_signed_twice_is_a_scaler(self):
        out = self.timing("roster-A", "quality", 2)
        self.assertEqual([(r["status"], r["confidence"]) for r in out["wallet_timing"]], [("scaler", "medium")])

    def test_non_roster_deep_watch_holder_keeps_transfer_recipient(self):
        out = self.timing("deep-watch", "quality", 0)
        self.assertEqual([(r["status"], r["confidence"]) for r in out["wallet_timing"]], [("transfer-recipient", "medium")])
        self.assertEqual(out["timing_label"], "transfer-recipient")


if __name__ == "__main__":
    unittest.main()
