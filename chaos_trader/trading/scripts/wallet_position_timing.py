#!/usr/bin/env python3
"""Wallet timing evidence for Chaos token reads.

Turns wallet presence into explicit timing-status evidence without pretending that
sampled holder/signature data proves a trade. Read-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

STATUS_LABELS = {
    "early-holder",
    "late-buyer",
    "scaler",
    "distributor",
    "round-tripper",
    "transfer-recipient",
    "airdrop/spam-recipient",
    "unknown",
}


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def _iso_from_ts(value: Any) -> str | None:
    try:
        if value in (None, "", [], {}):
            return None
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _holder_amount(existing: dict[str, Any], wallet: str) -> float | None:
    for row in existing.get("top_holders_sample") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("owner") or row.get("wallet") or "") == wallet:
            return as_float(row.get("amount"), None)
    return None


def _signer_count(existing: dict[str, Any], wallet: str) -> int:
    for row in existing.get("top_signers_sample") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("wallet") or row.get("owner") or "") == wallet:
            try:
                return int(float(row.get("count") or 0))
            except Exception:
                return 0
    return 0


def _status_for_hit(hit: dict[str, Any], *, current_balance: float | None, signer_count: int) -> tuple[str, str]:
    role = str(hit.get("role") or "").lower()
    source = str(hit.get("source") or "").lower()
    if current_balance and current_balance > 0:
        if signer_count > 1:
            return "scaler", "medium"
        if "airdrop" in role or "spam" in role:
            return "airdrop/spam-recipient", "medium"
        if "deep-first" in role or "final" in source or "early" in role:
            return "early-holder", "low"
        # A roster wallet that holds without repeat signing: the read has no first-touch data, so no transfer claim.
        if role.startswith("roster-"):
            return "unknown", "low"
        return "transfer-recipient", "medium"
    if signer_count > 1:
        return "scaler", "low"
    return "unknown", "low"


def enrich_wallet_timing(existing: dict[str, Any] | None, result: dict[str, Any]) -> dict[str, Any]:
    existing = dict(existing or {})
    owner = result.get("owner_exposure") or {}
    catalyst = result.get("social_catalyst") or {}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in existing.get("quality_wallet_hits") or []:
        if not isinstance(hit, dict):
            continue
        wallet = str(hit.get("wallet") or "")
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        bal = _holder_amount(existing, wallet)
        signer_count = _signer_count(existing, wallet)
        status, confidence = _status_for_hit(hit, current_balance=bal, signer_count=signer_count)
        rows.append({
            "wallet": wallet,
            "label": hit.get("label") or wallet[:6],
            "role": hit.get("role") or hit.get("kind") or "quality",
            "current_balance": bal,
            "first_touch_utc": None,
            "first_touch_type": "sample-holder" if bal else ("sample-signer" if signer_count else None),
            "status": status,
            "confidence": confidence,
        })
    for hit in existing.get("scout_wallet_hits") or []:
        if not isinstance(hit, dict):
            continue
        wallet = str(hit.get("wallet") or "")
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        bal = _holder_amount(existing, wallet)
        signer_count = _signer_count(existing, wallet)
        status, confidence = _status_for_hit(hit, current_balance=bal, signer_count=signer_count)
        rows.append({
            "wallet": wallet,
            "label": hit.get("label") or wallet[:6],
            "role": hit.get("role") or "scout",
            "current_balance": bal,
            "first_touch_utc": None,
            "first_touch_type": "sample-holder" if bal else ("sample-signer" if signer_count else None),
            "status": status,
            "confidence": confidence,
        })
    for hit in owner.get("owner_wallet_hits") or []:
        if not isinstance(hit, dict):
            continue
        wallet = str(hit.get("wallet") or "")
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        amount = as_float(hit.get("amount"), None)
        rows.append({
            "wallet": wallet,
            "label": hit.get("label") or wallet[:6],
            "role": "owner-private",
            "current_balance": amount,
            "first_touch_utc": None,
            "first_touch_type": "owner-current-balance" if amount else None,
            "status": "transfer-recipient" if amount else "unknown",
            "confidence": "medium" if amount else "low",
            "private_owner_context": True,
        })

    summary = {label.replace("-", "_") + "s": 0 for label in STATUS_LABELS}
    # Preserve requested singular-ish keys where useful.
    summary = {
        "early_holders": 0,
        "late_buyers": 0,
        "distributors": 0,
        "transfer_recipients": 0,
        "round_trippers": 0,
        "scalers": 0,
        "airdrop_spam_recipients": 0,
        "unknown": 0,
    }
    for row in rows:
        status = row.get("status") or "unknown"
        if status == "early-holder":
            summary["early_holders"] += 1
        elif status == "late-buyer":
            summary["late_buyers"] += 1
        elif status == "distributor":
            summary["distributors"] += 1
        elif status == "round-tripper":
            summary["round_trippers"] += 1
        elif status == "scaler":
            summary["scalers"] += 1
        elif status == "transfer-recipient":
            summary["transfer_recipients"] += 1
        elif status == "airdrop/spam-recipient":
            summary["airdrop_spam_recipients"] += 1
        else:
            summary["unknown"] += 1

    if summary["distributors"]:
        label = "distributing"
    elif summary["early_holders"]:
        label = "early"
    elif summary["transfer_recipients"]:
        label = "transfer-recipient"
    elif rows:
        label = "timing unresolved"
    else:
        label = "none"

    existing["wallet_timing"] = rows
    existing["timing_summary"] = summary
    existing["timing_label"] = label
    existing["catalyst_reference"] = catalyst.get("catalyst_type")
    existing["note"] = "wallet timing is evidence quality only; no replication or execution inference"
    return existing


__all__ = ["STATUS_LABELS", "enrich_wallet_timing"]
