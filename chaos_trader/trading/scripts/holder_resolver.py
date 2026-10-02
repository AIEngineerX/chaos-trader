#!/usr/bin/env python3
"""Resolve token largest accounts into holder classes.

Read-only. Separates LP/pool/program/custody-like accounts from discretionary wallets
before making concentration claims.
"""
from __future__ import annotations
import os

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helius_common import load_env, method_not_served, require_address, rpc_request, safe_print

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
# The last good sample per mint, used when a rate limit outlasts the retries. Safe to delete.
HOLDER_CACHE = PROFILE_HOME / "trading" / "cache" / "holders"
CACHE_MAX_AGE_S = 15 * 60
RATE_LIMIT_WAITS = (1, 2, 4)
# Failures rpc_request retries by itself; a first failure of this kind goes back through its own loop.
TRANSIENT_STATUSES = {500, 502, 503, 504}
_sleep = time.sleep  # a clock: tests replace it

KNOWN_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": ("pump.fun", "program"),
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": ("pump.swap/pAMM", "lp_pool"),
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": ("Raydium CPMM", "lp_pool"),
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": ("Raydium CLMM", "lp_pool"),
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("Raydium AMM v4", "lp_pool"),
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": ("Orca Whirlpool", "lp_pool"),
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": ("Meteora DLMM", "lp_pool"),
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": ("Meteora Pools", "lp_pool"),
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": ("Jupiter", "program"),
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": ("SPL Token", "program"),
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": ("Token-2022", "program"),
    "11111111111111111111111111111111": ("System Program", "wallet_or_system"),
}

BURN_OWNERS = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
}


def get_account_infos(addresses: list[str]) -> list[Any]:
    if not addresses:
        return []
    out = []
    for i in range(0, len(addresses), 100):
        chunk = addresses[i:i+100]
        res = rpc_request("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}], timeout=30)
        out.extend((res or {}).get("value", []))
    return out


def classify_owner(owner: str | None, owner_info: dict[str, Any] | None) -> dict[str, Any]:
    if not owner:
        return {"holder_class": "unknown", "confidence": "low", "reason": "missing_owner"}
    if owner in BURN_OWNERS:
        return {"holder_class": "burn", "confidence": "high", "reason": "burn_or_dead_owner"}
    if not owner_info:
        return {"holder_class": "unknown", "confidence": "low", "reason": "owner_account_unresolved"}
    program = owner_info.get("owner")
    executable = bool(owner_info.get("executable"))
    label, cls = KNOWN_PROGRAMS.get(program, (None, None))
    if cls == "lp_pool":
        return {"holder_class": "lp_pool", "confidence": "high", "reason": f"owner_account_owned_by_{label}", "owner_program": program, "program_label": label}
    if executable or cls == "program":
        return {"holder_class": "program", "confidence": "high" if label else "medium", "reason": f"program_owned_or_executable_{label or program}", "owner_program": program, "program_label": label}
    # System-owned non-executable accounts are usually discretionary wallets, but PDAs can also be system-owned.
    if program == "11111111111111111111111111111111":
        return {"holder_class": "wallet", "confidence": "medium", "reason": "system_owned_non_executable", "owner_program": program}
    if program in KNOWN_PROGRAMS:
        return {"holder_class": "program", "confidence": "medium", "reason": f"known_program_owner_{KNOWN_PROGRAMS[program][0]}", "owner_program": program, "program_label": KNOWN_PROGRAMS[program][0]}
    return {"holder_class": "unknown", "confidence": "medium", "reason": f"owner_account_owned_by_unclassified_program_{program}", "owner_program": program}


def _failure_details(failure: SystemExit) -> dict[str, Any]:
    """The JSON body rpc_request raises with, or {} when the exit message is plain text."""
    try:
        details = json.loads(str(failure))
    except ValueError:
        return {}
    return details if isinstance(details, dict) else {}


def _not_served(failure: SystemExit) -> bool:
    """rpc_request's failure was a 429 saying this RPC never serves the method."""
    return method_not_served(_failure_details(failure).get("http_status"), getattr(failure, "headers", None))


def largest_accounts(mint: str) -> dict[str, Any]:
    """getTokenLargestAccounts, retried after 1, 2 and 4 seconds while the RPC answers 429.

    A 429 that says the method is not served at all stops at once. Any other transient failure (5xx,
    network) goes through rpc_request's own retry loop, as before."""
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return rpc_request("getTokenLargestAccounts", [mint], timeout=30, retries=0) or {}
        except SystemExit as exc:
            details = _failure_details(exc)
            if details.get("http_status") in TRANSIENT_STATUSES or "network_error" in details:
                return rpc_request("getTokenLargestAccounts", [mint], timeout=30) or {}
            if details.get("http_status") != 429 or wait is None or _not_served(exc):
                raise
        _sleep(wait)


def _cache_file(mint: str) -> Path:
    return HOLDER_CACHE / f"{mint}.json"


def write_cached_holders(result: dict[str, Any]) -> None:
    """Keep the sample with its UTC read time. Written to a temp file first, so a reader never sees half a file."""
    path = _cache_file(result["mint"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({**result, "fetched_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    os.replace(tmp, path)


def cached_holders(mint: str) -> dict[str, Any] | None:
    """The cached sample marked `cached <N>m` when it is younger than 15 minutes; otherwise None."""
    path = _cache_file(mint)
    if not path.exists():
        return None
    sample = json.loads(path.read_text(encoding="utf-8"))
    age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(sample.pop("fetched_at"))).total_seconds()
    if age_s >= CACHE_MAX_AGE_S:
        return None
    return {**sample, "holder_data": f"cached {int(age_s // 60)}m"}


def unavailable_holders(mint: str, limit: int, failure: SystemExit) -> dict[str, Any]:
    """An empty holder set that says why it is empty, so a rate-limited RPC does not kill the token read."""
    status = _failure_details(failure).get("http_status")
    if status == 429:
        reason = "not served by this RPC" if _not_served(failure) else "rate limited"
    else:
        reason = "rpc error"
    return {
        "ok": True,
        "mode": "holder_resolver",
        "mint": mint,
        "limit": limit,
        "holder_data": f"unavailable ({reason})",
        "supply": None,
        "raw_top_pct": None,
        "lp_pool_pct": None,
        "program_or_burn_pct": None,
        "custody_pct": None,
        "adjusted_discretionary_pct": None,
        "unknown_pct": None,
        "largest_discretionary": None,
        "holders": [],
        "risk_notes": ["Holder data was not read, so concentration is unknown. Read-only: no signing/sending/swapping."],
    }


def resolve_holders(mint: str, limit: int) -> dict[str, Any]:
    mint = require_address(mint, "mint")
    try:
        largest = largest_accounts(mint)
        supply_res = rpc_request("getTokenSupply", [mint], timeout=30) or {}
    except SystemExit as exc:
        return cached_holders(mint) or unavailable_holders(mint, limit, exc)
    supply = float(((supply_res.get("value") or {}).get("uiAmount")) or 0)
    accounts = (largest.get("value") or [])[:limit]
    token_account_addrs = [a.get("address") for a in accounts if a.get("address")]
    token_infos = get_account_infos(token_account_addrs)
    owners = []
    rows = []
    for item, info in zip(accounts, token_infos):
        parsed = (((info or {}).get("data") or {}).get("parsed") or {})
        token_info = parsed.get("info") or {}
        owner = token_info.get("owner")
        owners.append(owner)
        amount = float(item.get("uiAmountString") or item.get("uiAmount") or 0)
        rows.append({
            "rank": len(rows)+1,
            "token_account": item.get("address"),
            "owner": owner,
            "amount": amount,
            "pct_supply": (amount / supply * 100) if supply else None,
            "token_account_program": (info or {}).get("owner"),
        })
    owner_infos = get_account_infos([o for o in owners if o])
    owner_info_map = {owner: info for owner, info in zip([o for o in owners if o], owner_infos)}
    for row in rows:
        c = classify_owner(row.get("owner"), owner_info_map.get(row.get("owner")))
        row.update(c)

    raw_top_pct = sum((r.get("pct_supply") or 0) for r in rows)
    lp_pct = sum((r.get("pct_supply") or 0) for r in rows if r.get("holder_class") == "lp_pool")
    program_pct = sum((r.get("pct_supply") or 0) for r in rows if r.get("holder_class") in {"program", "burn"})
    custody_pct = sum((r.get("pct_supply") or 0) for r in rows if r.get("holder_class") == "custody")
    discretionary = [r for r in rows if r.get("holder_class") in {"wallet", "unknown"}]
    discretionary_pct = sum((r.get("pct_supply") or 0) for r in discretionary)
    unknown_pct = sum((r.get("pct_supply") or 0) for r in rows if r.get("holder_class") == "unknown")
    largest_discretionary = max(discretionary, key=lambda r: r.get("pct_supply") or 0, default=None)

    result = {
        "ok": True,
        "mode": "holder_resolver",
        "mint": mint,
        "limit": limit,
        "supply": supply,
        "raw_top_pct": round(raw_top_pct, 6),
        "lp_pool_pct": round(lp_pct, 6),
        "program_or_burn_pct": round(program_pct, 6),
        "custody_pct": round(custody_pct, 6),
        "adjusted_discretionary_pct": round(discretionary_pct, 6),
        "unknown_pct": round(unknown_pct, 6),
        "largest_discretionary": largest_discretionary,
        "holders": rows,
        "risk_notes": [
            "Raw top concentration includes LP/pool/program accounts and must not be treated as dump-risk concentration.",
            "Adjusted discretionary concentration includes normal wallets plus unknowns until classified.",
            "LP classification is heuristic from owner account program; verify high-stakes reads manually.",
            "Read-only: no signing/sending/swapping.",
        ],
    }
    write_cached_holders(result)
    return result


def render_md(r: dict[str, Any]) -> str:
    if str(r.get("holder_data") or "").startswith("unavailable"):
        return "\n".join(["## Holder Resolution", f"- Mint: `{r['mint']}`", f"- Holder data: {r['holder_data']}"]) + "\n"
    lines = [
        "## Holder Resolution",
        f"- Mint: `{r['mint']}`",
        *([f"- Holder data: {r['holder_data']}"] if r.get("holder_data") else []),
        f"- Raw top {r['limit']}: {r['raw_top_pct']}%",
        f"- LP / pool excluded: {r['lp_pool_pct']}%",
        f"- Program / burn excluded: {r['program_or_burn_pct']}%",
        f"- Adjusted discretionary + unknown: {r['adjusted_discretionary_pct']}%",
        f"- Unknown: {r['unknown_pct']}%",
        "",
        "| Rank | Owner | Class | Supply % | Reason |",
        "|---:|---|---|---:|---|",
    ]
    for h in r["holders"]:
        owner = h.get("owner") or ""
        lines.append(f"| {h['rank']} | `{owner[:6]}…{owner[-4:]}` | {h.get('holder_class')} | {round(h.get('pct_supply') or 0, 4)} | {h.get('reason')} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    load_env(PROFILE_HOME / ".env")
    p = argparse.ArgumentParser(description="Resolve token holder classes and adjusted concentration")
    p.add_argument("mint")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()
    result = resolve_holders(args.mint, max(1, min(args.limit, 100)))
    if args.raw:
        safe_print(result)
    else:
        print(render_md(result))


if __name__ == "__main__":
    main()
