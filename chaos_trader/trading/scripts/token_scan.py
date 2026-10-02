#!/usr/bin/env python3
"""Read-only Helius token scanner for Chaos.

Combines DAS metadata, token-account holder sample, supply/largest accounts, and
mint transaction history. No buy/sell/swap/signing.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from helius_common import is_helius_endpoint, require_address, rpc_request, safe_print
from holder_resolver import resolve_holders


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def rpc_optional(method: str, params: Any) -> tuple[Any | None, str | None]:
    try:
        # Helius mixes JSON-RPC param shapes: classic RPC methods use arrays;
        # DAS methods such as getAsset/getTokenAccounts use an object.
        return rpc_request(method, params), None
    except SystemExit as exc:
        return None, str(exc)


def asset_summary(asset: Any) -> dict[str, Any] | None:
    if not isinstance(asset, dict):
        return None
    content = asset.get("content") or {}
    metadata = content.get("metadata") or {}
    token_info = asset.get("token_info") or asset.get("tokenInfo") or {}
    return {
        "interface": asset.get("interface"),
        "id": asset.get("id"),
        "name": metadata.get("name") or content.get("name"),
        "symbol": metadata.get("symbol") or token_info.get("symbol"),
        "description": metadata.get("description"),
        "image": ((content.get("links") or {}).get("image")),
        "decimals": token_info.get("decimals"),
        "price_info": token_info.get("price_info") or token_info.get("priceInfo"),
        "supply": token_info.get("supply") or token_info.get("token_supply"),
        "mint_authority": token_info.get("mint_authority"),
        "freeze_authority": token_info.get("freeze_authority"),
        "ownership": asset.get("ownership"),
        "authorities": asset.get("authorities"),
        "creators": asset.get("creators"),
        "grouping": asset.get("grouping"),
        "compression": asset.get("compression"),
        "last_indexed_slot": asset.get("last_indexed_slot"),
    }


def token_accounts_summary(payload: Any, limit: int) -> dict[str, Any]:
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("token_accounts") or payload.get("items") or []
    holders = []
    owner_counter = Counter()
    total_raw = 0
    for row in rows[:limit] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        owner = row.get("owner")
        amount = row.get("amount") or row.get("balance")
        if isinstance(amount, int):
            total_raw += amount
        if owner:
            owner_counter[str(owner)] += 1
        holders.append({
            "token_account": row.get("address"),
            "owner": owner,
            "amount": amount,
            "frozen": row.get("frozen"),
            "delegated_amount": row.get("delegated_amount"),
        })
    return {
        "reported_total": payload.get("total") if isinstance(payload, dict) else None,
        "sample_count": len(rows) if isinstance(rows, list) else 0,
        "sampled_raw_amount_sum": total_raw or None,
        "owners_seen": len(owner_counter),
        "holder_sample": holders[:10],
    }


def tx_summary(data: list[dict[str, Any]]) -> dict[str, Any]:
    status = Counter("failed" if tx.get("err") else "succeeded" for tx in data)
    programs = Counter()
    signers = Counter()
    for tx in data:
        msg = ((tx.get("transaction") or {}).get("message") or {})
        keys = msg.get("accountKeys") or []
        for acct in keys:
            if isinstance(acct, dict) and acct.get("signer"):
                signers[str(acct.get("pubkey"))] += 1
        for inst in (msg.get("instructions") or []) + (tx.get("instructions") or []):
            if not isinstance(inst, dict):
                continue
            pid = inst.get("programId") or inst.get("program")
            if not pid and "programIdIndex" in inst:
                try:
                    key = keys[int(inst["programIdIndex"])]
                    pid = key.get("pubkey") if isinstance(key, dict) else str(key)
                except Exception:
                    pid = None
            if pid:
                programs[str(pid)] += 1
    return {
        "count": len(data),
        "status_counts": dict(status),
        "first_seen_utc": iso(min((tx.get("blockTime") for tx in data if tx.get("blockTime")), default=None)),
        "last_seen_utc": iso(max((tx.get("blockTime") for tx in data if tx.get("blockTime")), default=None)),
        "top_programs": programs.most_common(10),
        "top_signers": signers.most_common(10),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only token scan. No buys/sells/swaps/signing.")
    parser.add_argument("mint", help="SPL/token mint address")
    parser.add_argument("--limit", type=int, default=50, help="1-100 txs/token accounts to sample")
    parser.add_argument("--raw", action="store_true", help="Print JSON envelope")
    args = parser.parse_args()
    mint = require_address(args.mint, "mint")
    limit = max(1, min(args.limit, 100))

    asset, asset_error = rpc_optional("getAsset", {"id": mint, "options": {"showFungible": True, "showCollectionMetadata": True}})
    # Helius DAS names the field `mint`; the public RPC getTokenAccounts takes `mintAddress` and answers with a `token_accounts` list.
    mint_field = "mint" if is_helius_endpoint() else "mintAddress"
    token_accounts, token_accounts_error = rpc_optional("getTokenAccounts", {mint_field: mint, "limit": limit, "options": {"showZeroBalance": False}})
    supply, supply_error = rpc_optional("getTokenSupply", [mint, {"commitment": "finalized"}])
    largest, largest_error = rpc_optional("getTokenLargestAccounts", [mint, {"commitment": "finalized"}])
    tx_result, tx_error = rpc_optional("getTransactionsForAddress", [mint, {"transactionDetails": "full", "limit": limit, "sortOrder": "desc"}])
    tx_data = (tx_result or {}).get("data", []) if isinstance(tx_result, dict) else []
    try:
        holder_resolution = resolve_holders(mint, min(20, limit))
        holder_error = None
    except Exception as exc:
        holder_resolution = None
        holder_error = str(exc)

    largest_accounts = []
    for acct in ((largest or {}).get("value") or [])[:10]:
        largest_accounts.append({
            "token_account": acct.get("address"),
            "amount": acct.get("uiAmountString") or acct.get("uiAmount"),
            "decimals": acct.get("decimals"),
        })

    errors = {k: v for k, v in {
        "asset": asset_error,
        "token_accounts": token_accounts_error,
        "supply": supply_error,
        "largest_accounts": largest_error,
        "transactions": tx_error,
        "holder_resolution": holder_error,
    }.items() if v}

    result = {
        "ok": True,
        "mode": "read_only_token_scan",
        "mint": mint,
        "asset": asset_summary(asset),
        "supply": (supply.get("value") if isinstance(supply, dict) else supply),
        "token_accounts_summary": token_accounts_summary(token_accounts, limit),
        "largest_accounts_sample": largest_accounts,
        "holder_resolution": holder_resolution,
        "transaction_summary": tx_summary(tx_data),
        "endpoint_errors": errors,
        "risk_notes": [
            "Token-account holders are not always owner wallets; resolve owners before concentration claims.",
            "DAS/Wallet API metadata and prices can lag; verify with liquidity/source context.",
            "For pump.fun/memecoin reads, missing mint/freeze authority is baseline hygiene, not alpha; prioritize curve stage, viable size, slippage/exit path, and early-wallet quality.",
            "Read-only: no buy/sell/swap/signing.",
        ],
    }

    if args.raw:
        safe_print(result)
        return
    print("## Token Scan")
    print(f"- Mint: `{mint}`")
    asset_s = result["asset"] or {}
    print(f"- Name/Symbol: {asset_s.get('name') or 'unknown'} / {asset_s.get('symbol') or 'unknown'}")
    print(f"- Supply: {result['supply']}")
    print(f"- Holder sample: {result['token_accounts_summary']['sample_count']}")
    print(f"- Recent tx sample: {result['transaction_summary']['count']}")
    hr = result.get("holder_resolution") or {}
    if hr:
        print(f"- Raw top {hr.get('limit')} concentration: {hr.get('raw_top_pct')}%")
        print(f"- LP / pool excluded: {hr.get('lp_pool_pct')}%")
        print(f"- Adjusted discretionary + unknown: {hr.get('adjusted_discretionary_pct')}%")
        ld = hr.get("largest_discretionary") or {}
        if ld:
            print(f"- Largest discretionary/unknown holder: `{(ld.get('owner') or '')[:6]}…{(ld.get('owner') or '')[-4:]}` at {round(ld.get('pct_supply') or 0, 4)}% ({ld.get('holder_class')})")
    else:
        print("- Holder concentration: raw/unclassified only; do not treat largest account as whale without resolver.")
    if errors:
        print(f"- Endpoint caveats: {sorted(errors)}")
    print("\n## Next")
    print("Classify deployer, top holders, liquidity route, viable size/slippage, and recent wallet flow before treating as alpha.")


if __name__ == "__main__":
    main()
