#!/usr/bin/env python3
"""Mode + venue classification for Chaos token reads.

Read-only heuristic layer. It names the game before any token receives a gate.
No execution, alerts, signing, or wallet action.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

LOW_CAP_MAX = 150_000
CONVICTION_MIN_MC = 150_000
CONVICTION_MIN_LIQ = 35_000
FRESH_SECONDS = 4 * 60 * 60


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def pair_age_seconds(market: dict[str, Any]) -> int | None:
    raw = market.get("pair_created_at") or market.get("pairCreatedAt")
    ts = as_float(raw, None)
    if ts is None or ts <= 0:
        return None
    # Dexscreener pairCreatedAt is ms; tolerate seconds if supplied.
    created_ms = int(ts if ts > 10_000_000_000 else ts * 1000)
    return max(0, int((now_ms() - created_ms) / 1000))


def venue_label(market: dict[str, Any], pump: dict[str, Any] | None = None) -> str:
    dex = str(market.get("dex_id") or "").lower()
    url = str(market.get("url") or "").lower()
    pump = pump or {}
    if pump.get("pumpfun_activity_visible") or "pump" in dex or "pump" in url:
        return "pumpfun/pumpswap"
    if pump.get("raydium_activity_visible") or "raydium" in dex:
        return "raydium"
    if "bags" in dex or "bags" in url:
        return "bags"
    if "bonk" in dex or "letsbonk" in dex or "bonk" in url:
        return "bonkfun"
    if "moonshot" in dex or "moonshot" in url:
        return "moonshot"
    if "launchlab" in dex or "launchlab" in url:
        return "launchlab"
    return dex or "unknown"


def venue_regime(venue: str) -> str:
    # Until Dune/API venue metrics are automated, keep this neutral and explicit.
    if venue in {"unknown", ""}:
        return "venue_unknown"
    return "venue_neutral"


def classify_mode(result: dict[str, Any]) -> dict[str, Any]:
    market = result.get("market") or {}
    cls = result.get("classification") or {}
    pump = result.get("pumpfun") or {}
    flow = cls.get("flow") or {}
    flags = [str(x).lower() for x in (cls.get("risk_flags") or [])]
    mc = as_float(market.get("market_cap") or market.get("fdv"), None)
    liq = as_float(market.get("liquidity_usd"), None)
    pc_h1 = as_float(market.get("price_change_h1"), 0) or 0
    pc_h24 = as_float(market.get("price_change_h24"), 0) or 0
    age = pair_age_seconds(market)
    venue = venue_label(market, pump)

    reasons: list[str] = []
    risk_notes: list[str] = []
    mode = "unknown"

    severe_drawdown = pc_h1 <= -50 or pc_h24 <= -80 or any("severe drawdown" in f for f in flags)
    fake_flow = int(flow.get("severity") or 0) >= 5 or any("extreme volume/liquidity" in f for f in flags)
    on_pump_bonding_curve = bool(pump and not pump.get("complete"))
    if severe_drawdown:
        mode = "dead/fake"
        reasons.append("severe drawdown")
    elif fake_flow and liq is not None and liq < 7_500 and not on_pump_bonding_curve:
        mode = "dead/fake"
        reasons.append("extreme fake-flow/liquidity failure")
    elif mc is not None and mc < LOW_CAP_MAX:
        mode = "low-cap trench"
        reasons.append(f"market cap below {LOW_CAP_MAX:,}")
        if age is not None and age <= FRESH_SECONDS:
            reasons.append("fresh pair window")
        if liq is not None and liq < 25_000:
            risk_notes.append("thin liquidity is expected here; dust size only")
    elif mc is not None and mc >= CONVICTION_MIN_MC and liq is not None and liq >= CONVICTION_MIN_LIQ:
        mode = "conviction trench"
        reasons.append("market cap and liquidity can support structure review")
    elif pc_h1 >= 150 and not (result.get("wallet_timing") or {}).get("watch_wallet_hit_count"):
        mode = "late"
        reasons.append("large move without wallet validation")
    else:
        mode = "unknown"
        reasons.append("insufficient mode evidence")

    if fake_flow and mode != "dead/fake":
        risk_notes.append("fake-flow flags present; conviction gate must be stricter")

    return {
        "mode": mode,
        "venue": venue,
        "venue_regime": venue_regime(venue),
        "pair_age_seconds": age,
        "market_cap": mc,
        "liquidity_usd": liq,
        "reasons": reasons,
        "risk_notes": risk_notes,
    }


__all__ = ["classify_mode", "venue_label", "venue_regime", "pair_age_seconds"]
