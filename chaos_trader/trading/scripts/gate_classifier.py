#!/usr/bin/env python3
"""Mode-aware gate classifier for Chaos token reads.

Consumes market, onchain, and secondary evidence and emits the gate:
MODE / GATE / WHY / RISK / NEXT. Read-only, no execution.
"""
from __future__ import annotations

from typing import Any


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", [], {}):
            return default
        return int(float(value))
    except Exception:
        return default


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def _secondary_counts(secondary: dict[str, Any]) -> dict[str, int]:
    metrics = secondary.get("metrics") or {}
    return {
        "hidden": as_int(metrics.get("hidden_buyer_count")),
        "early_hidden": as_int(metrics.get("early_hidden_count")),
        "scout": as_int(metrics.get("scout_buyer_count")),
        "early_scout": as_int(metrics.get("early_scout_count")),
        "tracked_buyers": as_int(metrics.get("tracked_buyer_count")),
        "tracked_sellers": as_int(metrics.get("tracked_seller_count")),
        "cluster_edges": as_int(metrics.get("pair_cluster_edges")),
        "tg_channels": as_int(metrics.get("tg_channel_count")),
        "holders": as_int(metrics.get("latest_holder_count")),
    }


def _confirmed_wallet_timing(result: dict[str, Any]) -> bool:
    """Require an observed first-touch timestamp before wallet priors promote gates."""
    timing = result.get("wallet_timing") or {}
    if not isinstance(timing, dict):
        return False
    for row in timing.get("wallet_timing") or []:
        if not isinstance(row, dict):
            continue
        if row.get("first_touch_utc") and str(row.get("first_touch_type") or "").strip():
            return True
    return False


def classify_gate(result: dict[str, Any]) -> dict[str, Any]:
    mode_obj = result.get("mode_context") or {}
    mode = mode_obj.get("mode") or "unknown"
    cls = result.get("classification") or {}
    flow = cls.get("flow") or {}
    validation = cls.get("validation") or {}
    secondary = result.get("secondary_evidence") or {}
    counts = _secondary_counts(secondary)
    market = result.get("market") or {}
    liq = as_float(market.get("liquidity_usd"), 0) or 0
    mc = as_float(market.get("market_cap") or market.get("fdv"), 0) or 0
    holder_adj = None
    for container_key in ("token_scan", "pumpfun"):
        container = result.get(container_key) if isinstance(result.get(container_key), dict) else {}
        hr = container.get("holder_resolution") if isinstance(container, dict) else None
        if isinstance(hr, dict) and hr.get("adjusted_discretionary_pct") is not None:
            holder_adj = as_float(hr.get("adjusted_discretionary_pct"), None)
            break

    why: list[str] = []
    risk: list[str] = []
    gate = "avoid"
    confidence = "low"
    next_action = f"analyze token {result.get('mint') or ''}".strip()

    trusted_scouts = counts["early_hidden"] + counts["early_scout"]
    any_scouts = counts["hidden"] + counts["scout"]
    confirmed_timing = _confirmed_wallet_timing(result)
    fake_flow = as_int(flow.get("severity")) >= 5
    high_conc = holder_adj is not None and holder_adj >= 35

    if mode == "dead/fake":
        gate = "avoid"
        risk.append("dead/fake mode: severe drawdown or extreme churn")
        confidence = "medium"
    elif mode == "low-cap trench":
        if confirmed_timing and (trusted_scouts >= 2 or counts["tracked_buyers"] >= 4):
            gate = "micro-watch"
            confidence = "medium"
            why.append("trusted scout/multi-wallet evidence plus confirmed first-touch timing")
        elif any_scouts >= 1 or counts["tracked_buyers"] >= 2:
            gate = "micro-study"
            why.append("some scout evidence, but timing remains unproven")
        else:
            gate = "avoid"
            risk.append("low-cap token lacks trusted scout evidence")
        if mc and mc > 120_000 and gate.startswith("micro"):
            risk.append("near upper low-cap band; may already be late")
        if liq < 25_000:
            risk.append("thin liquidity; dust size only")
        if high_conc:
            risk.append("high adjusted holder concentration")
            if gate == "micro-watch":
                gate = "micro-study"
        if fake_flow:
            risk.append("fake-flow severity high")
            if gate != "avoid":
                gate = "micro-study"
    elif mode == "conviction trench":
        structure = liq >= 35_000 and not high_conc and not fake_flow
        wallet_confirm = confirmed_timing and (counts["tracked_buyers"] >= 3 or validation.get("quality_wallet_hits", 0) > 0)
        exits = counts["tracked_sellers"] >= 1
        if structure and wallet_confirm and exits:
            gate = "deep-check"
            confidence = "medium"
            why.append("liquidity, wallet confirmation, and tracked exits present")
        elif structure and wallet_confirm:
            gate = "watch"
            why.append("structure and wallet confirmation present; exits still thin")
        elif structure:
            gate = "study"
            why.append("structure exists but wallet/source confirmation is weak")
        else:
            gate = "avoid"
            blockers = []
            if high_conc:
                blockers.append("high concentration")
            if fake_flow:
                blockers.append("fake-flow")
            if not wallet_confirm:
                blockers.append("no wallet/source confirmation")
            if wallet_confirm and blockers:
                why.append("quality wallet confirmation exists, but " + ", ".join(blockers) + " blocks conviction")
            else:
                why.append("conviction gate failed: structure or wallet/source confirmation insufficient")
            risk.append("conviction structure failed")
        if high_conc:
            risk.append("high adjusted holder concentration")
        if fake_flow:
            risk.append("fake-flow severity high")
    elif mode == "late":
        gate = "avoid"
        risk.append("late move without enough validation")
    else:
        if cls.get("verdict") in {"avoid", "exit-liquidity-watch"}:
            gate = "avoid"
            risk.append("legacy classifier flags avoid/exit-liquidity")
        else:
            gate = "study"
            why.append("mode unknown; collect more evidence")

    if secondary.get("verdict") in {"deep-check", "paper-plan-candidate"} and gate in {"study", "watch"}:
        why.append(f"Secondary historical evidence: {secondary.get('verdict')}")
    if counts["cluster_edges"]:
        why.append(f"wallet cluster overlap: {counts['cluster_edges']}")
    if counts["tg_channels"] >= 3:
        why.append(f"TG/channel secondary confirmation: {counts['tg_channels']}")
    if not why:
        why.append(cls.get("why_not_watch") or cls.get("why_watch") or "no positive gate evidence")
    if not risk:
        risk.extend((mode_obj.get("risk_notes") or [])[:2])
    if not risk:
        risks = cls.get("risk_flags") or []
        risk.extend(str(x) for x in risks[:2])
    if not risk:
        risk.append("unresolved execution/exit path; read-only research")

    return {
        "gate": gate,
        "confidence": confidence,
        "why": why[:4],
        "risk": risk[:4],
        "next": next_action,
        "counts": counts,
    }


__all__ = ["classify_gate"]
