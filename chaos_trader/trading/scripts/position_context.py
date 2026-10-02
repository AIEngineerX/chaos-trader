#!/usr/bin/env python3
"""Position-aware read layer for Chaos token reads.

This is private owner-exposure context only. It never emits execution, signing,
wallet-connect, or public-alpha instructions.
"""
from __future__ import annotations

from typing import Any

ALLOWED_ENTRY_ACTIONS = {
    "study",
    "watch",
    "manual-review",
    "study-caution",
    "exit-liquidity-watch",
    "avoid-entry",
}

ALLOWED_POSITION_ACTIONS = {
    "no-position",
    "avoid-entry",
    "watch-entry",
    "manage",
    "trim-risk",
    "hold-core",
    "exit-watch",
}


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def holder_concentration_pct(result: dict[str, Any]) -> float | None:
    """Return adjusted holder concentration when available."""
    for container_key in ("token_scan", "pumpfun"):
        container = result.get(container_key) if isinstance(result.get(container_key), dict) else {}
        hr = container.get("holder_resolution") if isinstance(container, dict) else None
        if isinstance(hr, dict):
            value = as_float(hr.get("adjusted_discretionary_pct"), None)
            if value is not None:
                return value
    return None


def normalize_entry_gate(gate: dict[str, Any] | None, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map existing structural gate labels into display-safe entry actions."""
    raw = str((gate or {}).get("gate") or (classification or {}).get("verdict") or "study").strip().lower()
    mapping = {
        "avoid": "avoid-entry",
        "micro-study": "study",
        "micro-watch": "watch",
        "deep-check": "manual-review",
        "paper-plan": "manual-review",
        "paper-plan-candidate": "manual-review",
        "study": "study",
        "watch": "watch",
        "manual-review": "manual-review",
        "study-caution": "study-caution",
        "exit-liquidity-watch": "exit-liquidity-watch",
    }
    action = mapping.get(raw, "study")
    if action not in ALLOWED_ENTRY_ACTIONS:
        action = "study"
    return {
        "gate": raw,
        "action": action,
        "confidence": (gate or {}).get("confidence") or "low",
        "why": list((gate or {}).get("why") or [])[:4],
        "risk": list((gate or {}).get("risk") or [])[:4],
        "note": "entry gate is separate from private owner position management",
    }


def analyze_position_context(result: dict[str, Any]) -> dict[str, Any]:
    market = result.get("market") or {}
    owner = result.get("owner_exposure") or {}
    gate = result.get("entry_gate") or normalize_entry_gate(result.get("gate"), result.get("classification"))
    catalyst = result.get("social_catalyst") or {}
    flow = result.get("flow_conversion") or {}

    hits = owner.get("owner_wallet_hits") or []
    owner_count = int(owner.get("owner_wallet_count") or 0)
    hit_count = int(owner.get("owner_wallet_hit_count") or len(hits) or 0)
    amount = sum(as_float(h.get("amount"), 0.0) or 0.0 for h in hits if isinstance(h, dict))
    price = as_float(market.get("price_usd"), None)
    liq = as_float(market.get("liquidity_usd"), None)
    value_usd = amount * price if price is not None and amount else None
    pct_liq = ((value_usd / liq) * 100.0) if value_usd is not None and liq and liq > 0 else None

    entry_estimate = None
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        entry_estimate = as_float(hit.get("estimated_entry_usd") or hit.get("entry_value_usd"), None)
        if entry_estimate is not None:
            break
    unrealized = ((value_usd - entry_estimate) / entry_estimate * 100.0) if value_usd is not None and entry_estimate and entry_estimate > 0 else None

    concentration = holder_concentration_pct(result)
    high_conc = concentration is not None and concentration >= 35
    catalyst_active = str(catalyst.get("catalyst_type") or "none") != "none"
    fragile_catalyst = str(catalyst.get("fragility") or "").lower() in {"high", "medium-high"}
    flow_status = str(flow.get("conversion_status") or "unknown")
    entry_action = str(gate.get("action") or "study")

    why: list[str] = []
    risk: list[str] = []
    if hit_count:
        why.append("owner exposed")
    if catalyst_active:
        why.append("attention catalyst active")
    if flow_status in {"visibility-engine", "attention-converting", "holder-converting", "social-reflexivity"}:
        why.append(f"flow context: {flow_status}")
    if high_conc:
        risk.append("high concentration")
    if fragile_catalyst:
        risk.append("catalyst retrace risk")
    if flow.get("fake_flow_severity") in {"high", "extreme"}:
        risk.append("fake-flow visibility risk")

    if not hit_count:
        action = "no-position"
    elif entry_action in {"avoid-entry", "exit-liquidity-watch", "study-caution"}:
        if catalyst_active and flow_status in {"visibility-engine", "attention-converting", "holder-converting", "social-reflexivity"}:
            action = "manage"
        elif high_conc or fragile_catalyst or (pct_liq is not None and pct_liq >= 1.0):
            action = "trim-risk"
        else:
            action = "manage"
    elif entry_action in {"watch", "manual-review"}:
        action = "hold-core" if catalyst_active and not high_conc else "manage"
    else:
        action = "manage"

    if action not in ALLOWED_POSITION_ACTIONS:
        action = "manage" if hit_count else "no-position"
    if not why:
        why.append("no private owner exposure detected")
    if not risk:
        risk.append("position risk unresolved")

    return {
        "owner_exposed": bool(hit_count),
        "owner_wallet_count": owner_count,
        "owner_wallet_hit_count": hit_count,
        "position_token_amount": _round(amount, 6),
        "position_value_usd": _round(value_usd, 2),
        "position_pct_liquidity": _round(pct_liq, 3),
        "estimated_entry_usd": _round(entry_estimate, 2),
        "estimated_unrealized_pct": _round(unrealized, 2),
        "position_action": action,
        "position_why": why[:4],
        "position_risk": risk[:4],
        "private_owner_context": True,
        "note": "owner wallet behavior is private owner-analysis context; not public alpha",
    }


__all__ = ["ALLOWED_ENTRY_ACTIONS", "ALLOWED_POSITION_ACTIONS", "normalize_entry_gate", "analyze_position_context", "holder_concentration_pct"]
