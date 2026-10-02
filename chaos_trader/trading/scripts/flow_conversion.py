#!/usr/bin/env python3
"""Fake-flow conversion classifier for Chaos token reads.

Separates useless churn from fake-looking activity that is converting into real
attention. Read-only; no execution labels.
"""
from __future__ import annotations

from typing import Any


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def classify_flow_conversion(result: dict[str, Any]) -> dict[str, Any]:
    market = result.get("market") or {}
    cls = result.get("classification") or {}
    flow = cls.get("flow") or {}
    catalyst = result.get("social_catalyst") or {}
    wallet = result.get("wallet_timing") or {}

    ratio = as_float(flow.get("volume_liquidity_ratio"), None)
    avg_tx = as_float(flow.get("avg_tx_usd"), None)
    tx_count = int(as_float(flow.get("tx_count"), 0) or 0)
    severity_score = int(as_float(flow.get("severity"), 0) or 0)
    pc_h1 = as_float(market.get("price_change_h1"), 0) or 0
    pc_h6 = as_float(market.get("price_change_h6"), 0) or 0
    pc_h24 = as_float(market.get("price_change_h24"), 0) or 0
    price_positive = pc_h1 > 0 or pc_h6 > 0 or pc_h24 > 0
    price_negative = pc_h1 <= -40 or pc_h6 <= -60 or pc_h24 <= -75
    catalyst_type = str(catalyst.get("catalyst_type") or "none")
    catalyst_quality = str(catalyst.get("source_quality") or "none")
    wallet_hits = int(wallet.get("quality_wallet_hit_count") or wallet.get("watch_wallet_hit_count") or 0)

    reasons: list[str] = []
    if ratio is not None and ratio >= 20:
        reasons.append("extreme or elevated volume/liquidity churn")
    if tx_count >= 10_000 and avg_tx is not None and avg_tx <= 150:
        reasons.append("micro-churn transaction profile")
    if catalyst_type != "none":
        reasons.append("social/catalyst attention present")
    if wallet_hits:
        reasons.append("quality wallet evidence present")
    if price_positive:
        reasons.append("price still responding")

    if severity_score >= 5 or (ratio is not None and ratio >= 25) or (tx_count >= 50_000 and avg_tx is not None and avg_tx <= 150):
        fake_severity = "high"
    elif severity_score >= 2 or (ratio is not None and ratio >= 10) or (tx_count >= 10_000 and avg_tx is not None and avg_tx <= 150):
        fake_severity = "medium"
    elif severity_score > 0:
        fake_severity = "low"
    else:
        fake_severity = "none"

    if price_negative and fake_severity in {"medium", "high"}:
        status = "exit-liquidity-churn"
    elif fake_severity == "high" and catalyst_type != "none" and price_positive:
        status = "attention-converting" if catalyst_quality in {"high", "very high", "medium"} else "visibility-engine"
    elif fake_severity in {"medium", "high"} and catalyst_type != "none":
        status = "visibility-engine"
    elif fake_severity in {"medium", "high"}:
        status = "dead-churn"
    elif catalyst_type != "none" and price_positive:
        status = "social-reflexivity"
    elif not flow.get("flags"):
        status = "clean-flow"
    else:
        status = "unknown"

    attention_conversion = "unknown"
    if catalyst_type != "none" and catalyst_quality in {"high", "very high"}:
        attention_conversion = "high"
    elif catalyst_type != "none":
        attention_conversion = "medium"
    elif status in {"dead-churn", "exit-liquidity-churn"}:
        attention_conversion = "low"

    holder_conversion = "unknown"
    if wallet_hits >= 2:
        holder_conversion = "medium"
    elif wallet_hits == 1:
        holder_conversion = "low-medium"

    if price_negative and fake_severity in {"medium", "high"}:
        price_conversion = "negative"
    else:
        price_conversion = "positive" if price_positive else ("negative" if price_negative else "flat/unknown")
    if not reasons:
        reasons.append("no clear fake-flow conversion evidence")

    return {
        "fake_flow_severity": fake_severity,
        "conversion_status": status,
        "attention_conversion": attention_conversion,
        "holder_conversion": holder_conversion,
        "price_conversion": price_conversion,
        "volume_liquidity_ratio": ratio,
        "avg_tx_usd": avg_tx,
        "tx_count": tx_count,
        "reason": reasons[:5],
    }


__all__ = ["classify_flow_conversion"]
