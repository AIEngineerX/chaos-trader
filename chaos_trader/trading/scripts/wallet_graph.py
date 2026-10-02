#!/usr/bin/env python3
"""Read-only wallet graph scaffold for Chaos.

Builds a compact relationship view from Helius RPC without signing, sending,
swapping, streaming, or creating alerts.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from helius_common import require_address, rpc_request, safe_print

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
JUPITER_PROGRAMS = {"JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"}
RAYDIUM_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
}


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def pubkey_of(account_key: Any) -> str | None:
    if isinstance(account_key, dict):
        return account_key.get("pubkey")
    if account_key is None:
        return None
    return str(account_key)


def account_keys(tx: dict[str, Any]) -> list[Any]:
    return (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])


def tx_signature(tx: dict[str, Any]) -> str | None:
    sig = tx.get("signature")
    if sig:
        return sig
    sigs = ((tx.get("transaction") or {}).get("signatures") or [])
    return sigs[0] if sigs else None


def resolve_program(keys: list[Any], inst: dict[str, Any]) -> str | None:
    if inst.get("programId"):
        return str(inst.get("programId"))
    if inst.get("program"):
        return str(inst.get("program"))
    if "programIdIndex" in inst:
        try:
            return pubkey_of(keys[int(inst["programIdIndex"])])
        except Exception:
            return None
    return None


def iter_instructions(tx: dict[str, Any]):
    keys = account_keys(tx)
    message = ((tx.get("transaction") or {}).get("message") or {})
    for inst in message.get("instructions") or []:
        if isinstance(inst, dict):
            yield inst, resolve_program(keys, inst)
    for inst in tx.get("instructions") or []:
        if isinstance(inst, dict):
            yield inst, resolve_program(keys, inst)
    meta = tx.get("meta") or {}
    for inner in meta.get("innerInstructions") or []:
        for inst in inner.get("instructions") or []:
            if isinstance(inst, dict):
                yield inst, resolve_program(keys, inst)


def token_accounts(address: str, limit: int) -> list[dict[str, Any]]:
    result = rpc_request(
        "getTokenAccountsByOwner",
        [address, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}],
    )
    rows = ((result or {}).get("value") or [])
    out = []
    for row in rows[:limit]:
        info = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        amount = (info.get("tokenAmount") or {})
        ui = amount.get("uiAmount")
        ui_str = amount.get("uiAmountString")
        mint = info.get("mint")
        if mint:
            out.append({
                "token_account": row.get("pubkey"),
                "mint": mint,
                "ui_amount": ui_str if ui_str is not None else ui,
                "decimals": amount.get("decimals"),
                "nonzero": bool(float(ui or 0)),
            })
    return out


def tx_window(address: str, limit: int, sort: str) -> list[dict[str, Any]]:
    result = rpc_request(
        "getTransactionsForAddress",
        [
            address,
            {
                "transactionDetails": "full",
                "limit": limit,
                "sortOrder": sort,
                "maxSupportedTransactionVersion": 0,
                "filters": {"tokenAccounts": "balanceChanged"},
            },
        ],
    )
    return (result or {}).get("data", []) if isinstance(result, dict) else []


def summarize_txs(address: str, txs: list[dict[str, Any]]) -> dict[str, Any]:
    programs = Counter()
    signers = Counter()
    counterparties = Counter()
    mints = Counter()
    signatures = []
    status = Counter()
    program_hits = Counter()

    for tx in txs:
        signatures.append(tx_signature(tx))
        err = tx.get("err") or ((tx.get("meta") or {}).get("err"))
        status["failed" if err else "succeeded"] += 1
        keys = account_keys(tx)
        for acct in keys:
            pk = pubkey_of(acct)
            if not pk:
                continue
            if isinstance(acct, dict) and acct.get("signer"):
                signers[pk] += 1
            if pk != address and pk not in {SYSTEM_PROGRAM, TOKEN_PROGRAM, ASSOCIATED_TOKEN_PROGRAM}:
                counterparties[pk] += 1
        for _inst, pid in iter_instructions(tx):
            if pid:
                programs[pid] += 1
                if pid == PUMPFUN_PROGRAM:
                    program_hits["pumpfun"] += 1
                if pid in JUPITER_PROGRAMS:
                    program_hits["jupiter"] += 1
                if pid in RAYDIUM_PROGRAMS:
                    program_hits["raydium"] += 1
        meta = tx.get("meta") or {}
        for balance_key in ("preTokenBalances", "postTokenBalances"):
            for bal in meta.get(balance_key) or []:
                mint = bal.get("mint")
                owner = bal.get("owner")
                if mint and owner == address:
                    mints[mint] += 1

    return {
        "count": len(txs),
        "status_counts": dict(status),
        "first_seen_utc": iso(min((tx.get("blockTime") for tx in txs if tx.get("blockTime")), default=None)),
        "last_seen_utc": iso(max((tx.get("blockTime") for tx in txs if tx.get("blockTime")), default=None)),
        "signature_sample": [s for s in signatures if s][:5],
        "top_programs": programs.most_common(12),
        "top_program_labels": dict(program_hits),
        "top_signers": signers.most_common(8),
        "top_counterparties": counterparties.most_common(12),
        "owner_mints_seen": mints.most_common(12),
    }


def classify(balance_sol: float, token_rows: list[dict[str, Any]], recent: dict[str, Any], oldest: dict[str, Any]) -> dict[str, Any]:
    labels = []
    risk = []
    confidence = "low"
    recent_count = recent.get("count") or 0
    status = recent.get("status_counts") or {}
    failed = status.get("failed", 0)
    succeeded = status.get("succeeded", 0)
    program_labels = recent.get("top_program_labels") or {}
    nonzero = [row for row in token_rows if row.get("nonzero")]

    if failed and recent_count and failed / recent_count >= 0.35:
        labels.append("high-failure-churn")
        risk.append("High failed-transaction ratio; could be bot/scalper noise or execution friction.")
    if program_labels.get("pumpfun"):
        labels.append("pumpfun-active")
    if program_labels.get("jupiter"):
        labels.append("jupiter-flow")
    if program_labels.get("raydium"):
        labels.append("raydium-flow")
    if nonzero and len(nonzero) <= 3:
        labels.append("concentrated-current-holdings")
    if balance_sol < 0.05:
        risk.append("Low SOL balance; wallet may be disposable or underfunded.")
    if not oldest.get("count"):
        risk.append("Funding/origin unresolved from sampled transaction window.")
    if succeeded >= 10 or nonzero:
        confidence = "medium"
    if succeeded >= 25 and oldest.get("count"):
        confidence = "medium-high"

    if not labels:
        labels.append("unclassified")
    return {
        "classification": ", ".join(labels),
        "confidence": confidence,
        "risk_flags": risk or ["No strong risk flags from sampled data; still verify funding and realized exits."],
        "copyability_note": "Do not copy without realized exits, funding quality, liquidity/slippage, and repeat edge.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only wallet relationship graph scaffold. No execution or alerts.")
    parser.add_argument("address", help="Wallet address")
    parser.add_argument("--limit", type=int, default=50, help="1-100 recent txs and oldest txs to sample")
    parser.add_argument("--token-limit", type=int, default=50, help="1-200 token accounts to sample")
    parser.add_argument("--raw", action="store_true", help="Print JSON envelope")
    args = parser.parse_args()

    address = require_address(args.address)
    limit = max(1, min(args.limit, 100))
    token_limit = max(1, min(args.token_limit, 200))

    balance = rpc_request("getBalance", [address, {"commitment": "finalized"}])
    balance_sol = ((balance or {}).get("value") or 0) / 1_000_000_000
    tokens = token_accounts(address, token_limit)
    recent_txs = tx_window(address, limit, "desc")
    oldest_txs = tx_window(address, min(limit, 25), "asc")
    recent_summary = summarize_txs(address, recent_txs)
    oldest_summary = summarize_txs(address, oldest_txs)
    current_mints = [row for row in tokens if row.get("nonzero")]
    classification = classify(balance_sol, tokens, recent_summary, oldest_summary)

    result = {
        "ok": True,
        "mode": "read_only_wallet_graph",
        "address": address,
        "balance_sol": balance_sol,
        "token_accounts_sampled": len(tokens),
        "current_nonzero_tokens": current_mints[:20],
        "recent_window": recent_summary,
        "oldest_window": oldest_summary,
        "classification": classification,
        "graph_edges": {
            "programs": recent_summary["top_programs"],
            "counterparties": recent_summary["top_counterparties"],
            "signers": recent_summary["top_signers"],
            "mints": recent_summary["owner_mints_seen"],
        },
        "next_queries": [
            "lookup top counterparties if recurring",
            "scan nonzero token mints",
            "resolve earliest/funding transactions before assigning edge",
            "record wallet score only after outcomes exist",
        ],
        "boundary": "read-only wallet graph; no signing, sending, swapping, alerts, or platform collection",
    }

    if args.raw:
        safe_print(result)
        return
    print("## Wallet Graph")
    print(f"- Wallet: `{address}`")
    print(f"- SOL: {balance_sol}")
    print(f"- Classification: {classification['classification']}")
    print(f"- Confidence: {classification['confidence']}")
    print(f"- Current nonzero tokens: {len(current_mints)}")
    print(f"- Recent tx sample: {recent_summary['count']} / {recent_summary['status_counts']}")
    print(f"- Oldest tx sample: {oldest_summary['count']}")
    print("\n## Risk")
    for flag in classification["risk_flags"]:
        print(f"- {flag}")
    print("\n## Next")
    print("Trace funder/counterparties and record outcomes before assigning copy edge.")


if __name__ == "__main__":
    main()
