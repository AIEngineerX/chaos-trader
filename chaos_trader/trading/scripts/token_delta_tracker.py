#!/usr/bin/env python3
"""Token read delta tracker for Chaos artifacts.

Compares the current read with the latest previous token_event artifact for the
same mint. Read-only local artifact inspection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def pct_delta(prev: Any, cur: Any) -> float | None:
    p = as_float(prev, None)
    c = as_float(cur, None)
    if p is None or c is None or p == 0:
        return None
    return round(((c - p) / abs(p)) * 100.0, 3)


def _v_l(result: dict[str, Any]) -> Any:
    return ((result.get("flow_conversion") or {}).get("volume_liquidity_ratio")
            or (((result.get("classification") or {}).get("flow") or {}).get("volume_liquidity_ratio")))


def _latest_previous(mint: str, root: Path, current_json_path: str | None = None) -> dict[str, Any] | None:
    candidates = []
    pattern = f"token_event_*_{mint[:8]}_*.json"
    for path in root.glob(f"**/{pattern}"):
        if current_json_path and str(path) == str(current_json_path):
            continue
        candidates.append(path)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("mint") or "") == mint:
            data["_delta_source_path"] = str(path)
            return data
    return None


def track_token_delta(result: dict[str, Any], artifact_root: Path, current_json_path: str | None = None) -> dict[str, Any]:
    mint = str(result.get("mint") or "")
    prev = _latest_previous(mint, artifact_root, current_json_path=current_json_path)
    if not prev:
        return {"has_previous": False, "useful": False, "reason": "no previous artifact for mint"}

    cur_gate = (result.get("entry_gate") or {}).get("action") or (result.get("gate") or {}).get("gate")
    prev_gate = (prev.get("entry_gate") or {}).get("action") or (prev.get("gate") or {}).get("gate")
    cur_pos = (result.get("position_context") or {}).get("position_action")
    prev_pos = (prev.get("position_context") or {}).get("position_action")
    cur_cat = (result.get("social_catalyst") or {}).get("catalyst_type") or "none"
    prev_cat = (prev.get("social_catalyst") or {}).get("catalyst_type") or "none"
    cur_market = result.get("market") or {}
    prev_market = prev.get("market") or {}
    cur_owner = result.get("position_context") or result.get("owner_exposure") or {}
    prev_owner = prev.get("position_context") or prev.get("owner_exposure") or {}
    cur_wallet = result.get("wallet_timing") or {}
    prev_wallet = prev.get("wallet_timing") or {}

    delta = {
        "has_previous": True,
        "previous_path": prev.get("_delta_source_path"),
        "previous_generated_at": prev.get("generated_at"),
        "previous_gate": prev_gate,
        "current_gate": cur_gate,
        "previous_position_action": prev_pos,
        "current_position_action": cur_pos,
        "mc_delta_pct": pct_delta(prev_market.get("market_cap"), cur_market.get("market_cap")),
        "liquidity_delta_pct": pct_delta(prev_market.get("liquidity_usd"), cur_market.get("liquidity_usd")),
        "vl_delta_pct": pct_delta(_v_l(prev), _v_l(result)),
        "quality_wallet_count_delta": int(cur_wallet.get("quality_wallet_hit_count") or cur_wallet.get("watch_wallet_hit_count") or 0) - int(prev_wallet.get("quality_wallet_hit_count") or prev_wallet.get("watch_wallet_hit_count") or 0),
        "owner_exposure_delta": int(cur_owner.get("owner_wallet_hit_count") or 0) - int(prev_owner.get("owner_wallet_hit_count") or 0),
        "catalyst_changed": cur_cat != prev_cat,
        "catalyst_delta": f"{prev_cat} → {cur_cat}" if cur_cat != prev_cat else None,
        "gate_changed": cur_gate != prev_gate,
        "position_action_changed": cur_pos != prev_pos,
    }
    useful = any([
        delta["catalyst_changed"], delta["gate_changed"], delta["position_action_changed"],
        abs(delta["mc_delta_pct"] or 0) >= 10,
        abs(delta["liquidity_delta_pct"] or 0) >= 10,
        delta["quality_wallet_count_delta"] != 0,
        delta["owner_exposure_delta"] != 0,
    ])
    delta["useful"] = bool(useful)
    return delta


__all__ = ["track_token_delta"]
