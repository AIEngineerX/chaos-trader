#!/usr/bin/env python3
"""Classify social attention catalyst for Chaos token reads.

Uses already-collected X/search text and market social links only. No posting,
DMs, alerts, or account actions.
"""
from __future__ import annotations

from typing import Any


def _text_from_x(x: Any) -> str:
    if not isinstance(x, dict):
        return ""
    parts = [str(x.get("answer") or "")]
    for key in ("citations", "inline_citations"):
        for item in x.get(key) or []:
            if isinstance(item, dict):
                parts.extend([str(item.get("title") or ""), str(item.get("url") or "")])
            else:
                parts.append(str(item))
    return "\n".join(parts)


def _citations(x: Any) -> list[Any]:
    if not isinstance(x, dict):
        return []
    return list((x.get("inline_citations") or []) + (x.get("citations") or []))[:8]


def classify_social_catalyst(result: dict[str, Any]) -> dict[str, Any]:
    market = result.get("market") or {}
    x = result.get("x_attention") or {}
    raw_text = _text_from_x(x)
    text = raw_text.lower()
    socials = market.get("socials") or []

    catalyst_type = "none"
    subtype = None
    source_quality = "none"
    fragility = "unknown"
    weight = "none"
    description = "No social catalyst detected from current read."

    major_terms = ("toly", "anatoly", "mert", "raj", "phantom", "jupiter", "bonk")
    if any(term in text for term in ("spam", "raid", "raiding", "bot replies", "botting")):
        return {
            "catalyst_type": "spam-raid",
            "catalyst_subtype": None,
            "source_quality": "low",
            "fragility": "high",
            "weight": "low",
            "description": "Spam/raid-style attention detected; do not upgrade to major-source catalyst without clean citation evidence.",
            "citations": _citations(x),
            "raw_evidence_present": bool(raw_text.strip() or socials),
        }
    if any(term in text for term in ("community", "grind", "raid community", "holders pushing")) and catalyst_type == "none":
        catalyst_type = "community-grind"
        source_quality = "low-medium"
        fragility = "medium"
        weight = "low-medium"
        description = "Community grind attention detected."
    if any(term in text for term in ("dev stream", "livestream", "streaming", "spaces")):
        catalyst_type = "dev-stream"
        source_quality = "medium"
        fragility = "medium"
        weight = "medium"
        description = "Developer stream/spaces catalyst detected."
    if any(term in text for term in ("boost", "dexscreener boost", "boost promise", "vote", "voting")):
        catalyst_type = "boost-promise"
        source_quality = "medium-low"
        fragility = "high"
        weight = "medium-low"
        description = "Boost/vote-promise catalyst detected."
    if any(term in text for term in ("kol", "caller", "called by", "telegram caller")):
        catalyst_type = "KOL-call"
        source_quality = "medium"
        fragility = "medium-high"
        weight = "medium"
        description = "KOL/caller catalyst detected."
    if any(term in text for term in major_terms) and any(term in text for term in ("reply", "quote", "quoted", "quote tweet", "cascade")):
        catalyst_type = "soft-shill"
        subtype = "quote-cascade"
        source_quality = "high"
        fragility = "high"
        weight = "high"
        description = "Major-source reply/quote cascade interpreted as soft-shill."
    if any(term in text for term in major_terms) and any(term in text for term in ("ticker", "contract", " ca ", "direct mention", "posted")):
        catalyst_type = "hard-shill"
        subtype = "major-figure-direct"
        source_quality = "very high"
        fragility = "medium-high"
        weight = "very high"
        description = "Major-source direct ticker/contract mention detected."
    if any(term in text for term in ("screenshot", "image", "pfp", "banner")) and catalyst_type in {"soft-shill", "none"}:
        if catalyst_type == "none":
            catalyst_type = "image/screenshot-catalyst"
            subtype = "image/screenshot-catalyst"
            source_quality = "medium"
            weight = "medium"
            description = "Image/screenshot catalyst detected; high-fragility attention."
        fragility = "high"
    if catalyst_type == "none" and socials:
        # A token having social links is not itself a catalyst. Keep this as raw
        # evidence so cards do not launder generic DEX social metadata into
        # "community-grind" or any other attention claim.
        description = "Token has social links, but no attention catalyst was classified."

    return {
        "catalyst_type": catalyst_type,
        "catalyst_subtype": subtype,
        "source_quality": source_quality,
        "fragility": fragility,
        "weight": weight,
        "description": description,
        "citations": _citations(x),
        "raw_evidence_present": bool(raw_text.strip() or socials),
    }


__all__ = ["classify_social_catalyst"]
