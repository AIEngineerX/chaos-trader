#!/usr/bin/env python3
"""Secondary-evidence bridge for token reads.

Reads JSON files a separate ingest process left under trading/alpha/secondary. No network, auth, alerts, or execution.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chaos_home import chaos_home

PROFILE_HOME = chaos_home()
SUMMARY_PATH = PROFILE_HOME / "trading" / "alpha" / "secondary" / "mint_score_summary.json"
MINT_SCORE_DIR = PROFILE_HOME / "trading" / "alpha" / "mint_scores"


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def compact_secondary_evidence(mint: str) -> dict[str, Any]:
    """Return compact local secondary evidence for a mint, if present."""
    path = MINT_SCORE_DIR / f"{mint}.json"
    data = load_json(path, None)
    if not data:
        summary = load_json(SUMMARY_PATH, {"rows": []})
        rows = summary.get("rows") or []
        data = next((r for r in rows if r.get("mint") == mint), None)
    if not isinstance(data, dict):
        return {
            "present": False,
            "source": "local_secondary_export",
            "verdict": "no-secondary-evidence",
            "score": None,
            "metrics": {},
            "positive": [],
            "negative": ["mint not present in local secondary export"],
        }
    metrics = dict(data.get("metrics") or {})
    return {
        "present": True,
        "source": data.get("source") or "local_secondary_export",
        "verdict": data.get("verdict"),
        "score": data.get("score"),
        "symbol": data.get("symbol"),
        "metrics": {
            "tracked_buyer_count": metrics.get("tracked_buyer_count"),
            "tracked_seller_count": metrics.get("tracked_seller_count"),
            "hidden_buyer_count": metrics.get("hidden_buyer_count"),
            "early_hidden_count": metrics.get("early_hidden_count"),
            "scout_buyer_count": metrics.get("scout_buyer_count"),
            "early_scout_count": metrics.get("early_scout_count"),
            "pair_cluster_edges": metrics.get("pair_cluster_edges"),
            "tg_channel_count": metrics.get("tg_channel_count"),
            "call_market_cap": metrics.get("call_market_cap"),
            "current_market_cap": metrics.get("current_market_cap"),
            "ath_multiplier": metrics.get("ath_multiplier"),
            "latest_supply_pct": metrics.get("latest_supply_pct"),
            "latest_holder_count": metrics.get("latest_holder_count"),
        },
        "positive": (data.get("positive") or [])[:6],
        "negative": (data.get("negative") or [])[:6],
        "hard_flags": (data.get("hard_flags") or [])[:6],
        "hidden_buyers": (data.get("hidden_buyers") or [])[:5],
        "actor_buyers": (data.get("actor_buyers") or [])[:5],
    }


__all__ = ["compact_secondary_evidence"]
