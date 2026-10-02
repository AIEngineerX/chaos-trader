#!/usr/bin/env python3
"""Strategy-faithful paper decision engine for Chaos token reads.

Consumes the same read-only `token_event_analyzer.py --raw --x` payload that
the operator and Chaos use for normal analyze-token work and converts it into paper-only
state decisions. No wallet, signing, routing, orders, alerts, or webhooks.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BOUNDARY = "read-only strategy paper engine; no execution, wallet, signing, routing, alerts, or webhooks"
BLOCKED_ENTRY_ACTIONS = {"avoid-entry", "exit-liquidity-watch", "avoid"}
BLOCKED_VERDICTS = {"avoid", "exit-liquidity-watch"}
PASS_ENTRY_ACTIONS = {"micro-watch", "watch", "deep-check", "manual-review"}
WATCH_ENTRY_ACTIONS = {"study", "micro-study", "study-caution"}
SPAM_CATALYSTS = {"spam-raid"}
GOOD_CATALYST_QUALITY = {"medium", "high", "very high"}
CONVERTING_FLOW = {"attention-converting", "holder-converting", "social-reflexivity", "clean-flow"}
BAD_FLOW = {"dead-churn", "exit-liquidity-churn"}


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", [], {}):
            return default
        return int(float(value))
    except Exception:
        return default


def x_citation_count(x: Any) -> int:
    if not isinstance(x, dict):
        return 0
    return len(x.get("citations") or []) + len(x.get("inline_citations") or [])


def holder_pct(payload: dict[str, Any]) -> float | None:
    for key in ("token_scan", "pumpfun"):
        container = payload.get(key) if isinstance(payload.get(key), dict) else {}
        hr = container.get("holder_resolution") if isinstance(container, dict) else None
        if isinstance(hr, dict) and hr.get("adjusted_discretionary_pct") is not None:
            return as_float(hr.get("adjusted_discretionary_pct"), None)
    return None


def entry_action(payload: dict[str, Any]) -> str:
    gate = payload.get("entry_gate") or {}
    structural = payload.get("gate") or {}
    cls = payload.get("classification") or {}
    return str(gate.get("action") or structural.get("gate") or cls.get("verdict") or "study").strip().lower()


def max_simulated_size_usd(liquidity_usd: float | None, *, base_risk_usd: float = 100.0, max_notional_usd: float = 250.0, liquidity_bps: float = 50.0) -> float:
    """Cap simulated size by liquidity. 50 bps = 0.5% of liquidity."""
    if liquidity_usd is None or liquidity_usd <= 0:
        return 0.0
    liq_cap = liquidity_usd * (liquidity_bps / 10_000.0)
    return round(max(0.0, min(base_risk_usd, max_notional_usd, liq_cap)), 2)


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def decide(payload: dict[str, Any], *, base_risk_usd: float = 100.0, max_notional_usd: float = 250.0, liquidity_bps: float = 50.0, min_liquidity_usd: float = 25_000.0, max_holder_pct: float = 45.0) -> dict[str, Any]:
    market = payload.get("market") or {}
    cls = payload.get("classification") or {}
    mode = payload.get("mode_context") or {}
    catalyst = payload.get("social_catalyst") or {}
    flow = payload.get("flow_conversion") or {}
    wallet = payload.get("wallet_timing") or {}
    x = payload.get("x_attention")
    x_risk = cls.get("x_risk") or {}

    action = entry_action(payload)
    verdict = str(cls.get("verdict") or "study").strip().lower()
    attention_phase = str(cls.get("attention_phase") or "unknown").strip().lower()
    mode_name = str(mode.get("mode") or "unknown").strip().lower()
    catalyst_type = str(catalyst.get("catalyst_type") or "none").strip()
    catalyst_quality = str(catalyst.get("source_quality") or "none").strip().lower()
    flow_status = str(flow.get("conversion_status") or "unknown").strip()
    fake_flow = str(flow.get("fake_flow_severity") or "none").strip().lower()
    liq = as_float(market.get("liquidity_usd"), None)
    mc = as_float(market.get("market_cap") or market.get("fdv"), None)
    price = as_float(market.get("price_usd"), None)
    holders = holder_pct(payload)
    watch_hits = as_int(wallet.get("watch_wallet_hit_count") or wallet.get("quality_wallet_hit_count"), 0)
    x_enabled = bool(payload.get("x_enabled"))
    citations = x_citation_count(x) if x_enabled else 0
    x_ok = x_enabled and isinstance(x, dict) and bool(x.get("success"))
    x_risk_conf = str(x_risk.get("confidence") or "low").lower()

    reasons: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []
    required_trigger: list[str] = []

    if action in PASS_ENTRY_ACTIONS:
        _add_unique(reasons, f"entry gate {action}")
    elif action in WATCH_ENTRY_ACTIONS:
        _add_unique(required_trigger, f"entry gate must upgrade from {action}")
    if verdict in BLOCKED_VERDICTS or action in BLOCKED_ENTRY_ACTIONS:
        _add_unique(blockers, f"blocked entry label: {action or verdict}")
    if mode_name in {"dead/fake", "late"}:
        _add_unique(blockers, f"mode is {mode_name}")
    if liq is None or liq < min_liquidity_usd:
        _add_unique(blockers if liq is None or liq < 7_500 else warnings, f"liquidity below paper threshold: {liq}")
    else:
        _add_unique(reasons, f"liquidity supports paper sizing: ${liq:,.0f}")
    if holders is not None and holders >= max_holder_pct:
        _add_unique(blockers, f"adjusted holder concentration high: {holders:.1f}%")
    elif holders is not None and holders >= 35:
        _add_unique(warnings, f"holder concentration elevated: {holders:.1f}%")
    if catalyst_type in SPAM_CATALYSTS:
        _add_unique(blockers, "X/social catalyst is spam-raid")
    elif catalyst_type != "none" and catalyst_quality in GOOD_CATALYST_QUALITY:
        _add_unique(reasons, f"social catalyst {catalyst_type}/{catalyst_quality}")
    elif x_ok and citations:
        # The analyzer stores x_attention only for a sourced answer, so this is the only X rule:
        # an unavailable or unsourced answer adds no reason, trigger or blocker here.
        _add_unique(reasons, f"X evidence present: {citations} citations")
    if x_risk_conf in {"high"}:
        _add_unique(blockers, "high X-risk confidence")
    if fake_flow == "high" and flow_status not in CONVERTING_FLOW:
        _add_unique(blockers, f"fake-flow high and not converting: {flow_status}")
    elif flow_status in BAD_FLOW:
        _add_unique(blockers, f"bad flow conversion: {flow_status}")
    elif fake_flow in {"medium", "high"}:
        _add_unique(warnings, f"fake-flow {fake_flow}; requires conversion proof")
    if watch_hits:
        _add_unique(reasons, f"wallet timing/watch evidence: {watch_hits}")
    elif catalyst_type == "none":
        _add_unique(required_trigger, "needs wallet timing or credible catalyst")
    if attention_phase in {"late", "failed"}:
        _add_unique(blockers, f"attention phase {attention_phase}")

    size = max_simulated_size_usd(liq, base_risk_usd=base_risk_usd, max_notional_usd=max_notional_usd, liquidity_bps=liquidity_bps)
    if size <= 0:
        _add_unique(blockers, "simulated size resolves to zero")
    elif size < base_risk_usd * 0.5:
        _add_unique(warnings, f"dust-size only: ${size}")

    if blockers:
        decision = "paper_avoid"
    elif action in PASS_ENTRY_ACTIONS and (watch_hits or catalyst_quality in GOOD_CATALYST_QUALITY or (x_ok and citations)):
        decision = "paper_enter"
    else:
        decision = "paper_wait"
        if not required_trigger:
            _add_unique(required_trigger, "needs one more confirmation: wallet timing, clean catalyst, or improving structure")

    stop_pct = -30.0 if mode_name == "low-cap trench" else -25.0
    tp1_pct = 75.0
    tp2_pct = 200.0
    time_stop_minutes = 15 if mode_name == "low-cap trench" else 25
    invalidation = [
        f"market/price falls {abs(stop_pct):.0f}% from paper entry",
        "liquidity drops >25% from entry",
        "gate flips to avoid or exit-liquidity-watch",
        "flow conversion becomes exit-liquidity-churn/dead-churn",
    ]
    if catalyst_type != "none":
        invalidation.append("catalyst attention expires without buyer follow-through")

    return {
        "ok": True,
        "mode": "strategy_paper_decision_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "boundary": BOUNDARY,
        "mint": payload.get("mint"),
        "symbol": market.get("symbol"),
        "decision": decision,
        "entry_action": action,
        "legacy_verdict": verdict,
        "mode_context": mode_name,
        "attention_phase": attention_phase,
        "social_catalyst": {"type": catalyst_type, "quality": catalyst_quality, "fragility": catalyst.get("fragility")},
        "flow": {"status": flow_status, "fake_flow_severity": fake_flow, "volume_liquidity_ratio": flow.get("volume_liquidity_ratio")},
        "market": {"price_usd": price, "market_cap": mc, "liquidity_usd": liq},
        "holder_adjusted_pct": holders,
        "watch_wallet_hits": watch_hits,
        "x": {"enabled": x_enabled, "success": x_ok, "citations": citations, "risk_confidence": x_risk_conf},
        "paper_plan": {
            "simulated_notional_usd": size,
            "base_risk_usd": base_risk_usd,
            "max_notional_usd": max_notional_usd,
            "liquidity_bps_cap": liquidity_bps,
            "stop_pct": stop_pct,
            "tp1_pct": tp1_pct,
            "tp2_pct": tp2_pct,
            "time_stop_minutes": time_stop_minutes,
            "required_trigger": required_trigger,
            "invalidation": invalidation,
        },
        "reasons": reasons[:8],
        "warnings": warnings[:8],
        "blockers": blockers[:8],
    }


def render(decision: dict[str, Any]) -> str:
    plan = decision.get("paper_plan") or {}
    x = decision.get("x") or {}
    flow = decision.get("flow") or {}
    market = decision.get("market") or {}
    lines = [
        "☄️ STRATEGY PAPER DECISION",
        f"{decision.get('symbol') or 'UNKNOWN'} · `{decision.get('mint')}`",
        f"Decision: **{decision.get('decision')}** · Gate: `{decision.get('entry_action')}` · Mode: `{decision.get('mode_context')}`",
        f"X: {'on' if x.get('enabled') else 'off'} · citations {x.get('citations')} · catalyst {((decision.get('social_catalyst') or {}).get('type'))}/{((decision.get('social_catalyst') or {}).get('quality'))}",
        f"Flow: {flow.get('status')} · fake {flow.get('fake_flow_severity')} · V/L {flow.get('volume_liquidity_ratio')}",
        f"Liq: {market.get('liquidity_usd')} · MC: {market.get('market_cap')} · size: ${plan.get('simulated_notional_usd')}",
    ]
    if decision.get("blockers"):
        lines.append("Blockers: " + "; ".join(decision.get("blockers") or []))
    if decision.get("warnings"):
        lines.append("Warnings: " + "; ".join(decision.get("warnings") or []))
    if plan.get("required_trigger"):
        lines.append("Required trigger: " + "; ".join(plan.get("required_trigger") or []))
    lines.append("Invalidation: " + "; ".join((plan.get("invalidation") or [])[:3]))
    lines.append(BOUNDARY)
    return "\n".join(lines)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Convert analyze-token JSON into strategy-faithful paper decision. No execution.")
    p.add_argument("json_file", help="Path to token_event_analyzer JSON artifact or '-' for stdin")
    p.add_argument("--raw", action="store_true")
    p.add_argument("--base-risk-usd", type=float, default=100.0)
    p.add_argument("--max-notional-usd", type=float, default=250.0)
    p.add_argument("--liquidity-bps", type=float, default=50.0)
    args = p.parse_args()
    if args.json_file == "-":
        payload = json.loads(sys.stdin.read())
    else:
        payload = json.loads(Path(args.json_file).expanduser().read_text())
    out = decide(payload, base_risk_usd=args.base_risk_usd, max_notional_usd=args.max_notional_usd, liquidity_bps=args.liquidity_bps)
    if args.raw:
        print(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    else:
        print(render(out))


if __name__ == "__main__":
    main()
