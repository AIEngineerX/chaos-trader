#!/usr/bin/env python3
"""Read-only pump.fun launch probe for Chaos.

Uses Helius JSON-RPC through helius_common. No buy/sell/create/swap/signing.
This is a classifier scaffold: it gathers mint-account transaction context and
flags whether pump.fun program activity is visible in the sampled data.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from helius_common import require_address, rpc_request, rpc_tx_request, safe_print
from holder_resolver import resolve_holders

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_PROGRAM_HINTS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
}


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def rpc_optional(method: str, params: list[Any], request=rpc_request) -> tuple[Any | None, str | None]:
    """Return (result, error) so one noisy RPC method does not kill the whole probe."""
    try:
        return request(method, params), None
    except SystemExit as exc:
        return None, str(exc)


def account_keys(tx: dict[str, Any]) -> list[Any]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    return message.get("accountKeys") or []


def resolve_account_key(keys: list[Any], index: Any) -> str | None:
    try:
        key = keys[int(index)]
    except (TypeError, ValueError, IndexError):
        return None
    if isinstance(key, dict):
        return key.get("pubkey")
    return str(key) if key is not None else None


def iter_instructions(tx: dict[str, Any]):
    """Yield instructions across common Helius/Solana response shapes.

    Raw Solana JSON often stores only programIdIndex, so attach resolved
    `_resolvedProgramId` when account keys are available.
    """
    keys = account_keys(tx)
    for inst in tx.get("instructions") or []:
        if isinstance(inst, dict):
            if "programId" not in inst and "programIdIndex" in inst:
                inst = {**inst, "_resolvedProgramId": resolve_account_key(keys, inst.get("programIdIndex"))}
            yield inst
    message = ((tx.get("transaction") or {}).get("message") or {})
    for inst in message.get("instructions") or []:
        if isinstance(inst, dict):
            if "programId" not in inst and "programIdIndex" in inst:
                inst = {**inst, "_resolvedProgramId": resolve_account_key(keys, inst.get("programIdIndex"))}
            yield inst
    meta = tx.get("meta") or {}
    for inner in meta.get("innerInstructions") or []:
        for inst in inner.get("instructions") or []:
            if isinstance(inst, dict):
                if "programId" not in inst and "programIdIndex" in inst:
                    inst = {**inst, "_resolvedProgramId": resolve_account_key(keys, inst.get("programIdIndex"))}
                yield inst


def program_id(inst: dict[str, Any]) -> str | None:
    return inst.get("programId") or inst.get("_resolvedProgramId") or inst.get("program")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only pump.fun mint launch probe.")
    parser.add_argument("mint", help="SPL token mint address")
    parser.add_argument("--limit", type=int, default=40, help="1-100 recent mint-account transactions to sample")
    parser.add_argument("--raw", action="store_true", help="Print JSON envelope")
    args = parser.parse_args()

    mint = require_address(args.mint, "mint")
    limit = max(1, min(args.limit, 100))

    supply, supply_error = rpc_optional("getTokenSupply", [mint, {"commitment": "finalized"}])
    largest, largest_error = rpc_optional("getTokenLargestAccounts", [mint, {"commitment": "finalized"}])
    tx_result, tx_error = rpc_optional(
        "getTransactionsForAddress",
        [mint, {"transactionDetails": "full", "limit": limit, "sortOrder": "asc"}],
        request=rpc_tx_request,
    )
    tx_data = tx_result.get("data", []) if isinstance(tx_result, dict) else []
    try:
        holder_resolution = resolve_holders(mint, 20)
        holder_error = None
    except Exception as exc:
        holder_resolution = None
        holder_error = str(exc)

    programs = Counter()
    pump_txs = []
    raydium_txs = []
    signers = Counter()
    for tx in tx_data:
        sig = tx.get("signature") or tx.get("transaction", {}).get("signatures", [None])[0]
        message = ((tx.get("transaction") or {}).get("message") or {})
        for acct in message.get("accountKeys") or []:
            if isinstance(acct, dict) and acct.get("signer"):
                signers[acct.get("pubkey")] += 1
        tx_programs = set()
        for inst in iter_instructions(tx):
            pid = program_id(inst)
            if pid:
                programs[str(pid)] += 1
                tx_programs.add(str(pid))
        if PUMPFUN_PROGRAM in tx_programs:
            pump_txs.append({"signature": sig, "slot": tx.get("slot"), "block_time_utc": iso(tx.get("blockTime"))})
        if tx_programs & RAYDIUM_PROGRAM_HINTS:
            raydium_txs.append({"signature": sig, "slot": tx.get("slot"), "block_time_utc": iso(tx.get("blockTime"))})

    largest_accounts = []
    for acct in ((largest or {}).get("value") or [])[:10]:
        largest_accounts.append({
            "token_account": acct.get("address"),
            "amount": acct.get("uiAmountString") or acct.get("uiAmount"),
            "decimals": acct.get("decimals"),
        })

    pump_visible = bool(pump_txs)
    raydium_visible = bool(raydium_txs)
    risk_flags = []
    if not pump_visible:
        risk_flags.append("No pump.fun program interaction visible in sampled mint-account transactions; do not classify as pump.fun from this sample alone.")
    if raydium_visible:
        risk_flags.append("Raydium program activity appears in sample; token may have graduated or traded off-curve.")
    if len(tx_data) < 3:
        risk_flags.append("Very thin mint-account sample; insufficient for deployer/early-buyer conviction.")
    if supply_error:
        risk_flags.append("Token supply unavailable from RPC; supply unread.")
    if largest_error:
        risk_flags.append("Largest token accounts unavailable from RPC for this mint/sample; holder concentration unresolved.")
    if tx_error:
        risk_flags.append("Mint transaction history unavailable from this RPC (getTransactionsForAddress is Helius-only); launch activity unread.")
    if holder_error:
        risk_flags.append(f"Holder resolver failed; concentration is raw/unclassified: {holder_error}")
    risk_flags.extend([
        "Raw largest token accounts are not dump-risk concentration; use holder_resolution adjusted concentration.",
        "Use Helius wallet scans on deployer/early buyers before treating flow as alpha.",
        "Read-only: no pump.fun buy/sell/create or transaction construction exists here.",
    ])

    summary = {
        "ok": True,
        "mode": "read_only_pumpfun_launch_probe",
        "mint": mint,
        "pumpfun_program": PUMPFUN_PROGRAM,
        "sampled_transactions": len(tx_data),
        "sample_first_seen_utc": iso(min((tx.get("blockTime") for tx in tx_data if tx.get("blockTime")), default=None)),
        "sample_last_seen_utc": iso(max((tx.get("blockTime") for tx in tx_data if tx.get("blockTime")), default=None)),
        "pumpfun_activity_visible": pump_visible,
        "pumpfun_txs_sample": pump_txs[:5],
        "raydium_activity_visible": raydium_visible,
        "raydium_txs_sample": raydium_txs[:5],
        "top_programs": programs.most_common(12),
        "top_signers_sample": signers.most_common(8),
        "supply": (supply.get("value") or {}) if isinstance(supply, dict) else supply,
        "largest_accounts_sample": largest_accounts,
        "holder_resolution": holder_resolution,
        "holder_resolution_error": holder_error,
        "supply_error": supply_error,
        "largest_accounts_error": largest_error,
        "transactions_error": tx_error,
        "risk_flags": risk_flags,
        "classification": "pump.fun-visible" if pump_visible else "unconfirmed-pumpfun",
        "action": "study" if pump_visible else "verify_source_first",
    }
    if args.raw:
        safe_print(summary)
        return
    print("## pump.fun Read")
    print(f"- Mint: `{mint}`")
    print(f"- Classification: {summary['classification']}")
    print(f"- Sampled transactions: {summary['sampled_transactions']}")
    print(f"- pump.fun activity visible: {summary['pumpfun_activity_visible']}")
    print(f"- Raydium activity visible: {summary['raydium_activity_visible']}")
    hr = summary.get("holder_resolution") or {}
    if hr:
        print(f"- Raw top {hr.get('limit')}: {hr.get('raw_top_pct')}%")
        print(f"- LP / pool excluded: {hr.get('lp_pool_pct')}%")
        print(f"- Adjusted discretionary + unknown: {hr.get('adjusted_discretionary_pct')}%")
    else:
        print("- Holder concentration: unresolved; raw largest accounts are not dump-risk concentration.")
    print("\n## Risk Flags")
    for flag in risk_flags:
        print(f"- {flag}")
    print("\n## Action")
    print(summary["action"])


if __name__ == "__main__":
    main()
