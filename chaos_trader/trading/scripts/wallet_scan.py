#!/usr/bin/env python3
"""Read-only Helius wallet scanner for Chaos."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from helius_common import require_address, rpc_request, safe_print

LAMPORTS_PER_SOL = 1_000_000_000


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def summarize_transactions(data: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter("failed" if tx.get("err") else "succeeded" for tx in data)
    programs = Counter()
    token_mints = Counter()
    for tx in data:
        for inst in tx.get("instructions") or []:
            pid = inst.get("programId") or inst.get("program")
            if pid:
                programs[str(pid)] += 1
        meta = tx.get("meta") or {}
        for bal in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
            mint = bal.get("mint")
            if mint:
                token_mints[mint] += 1
    return {
        "count": len(data),
        "status_counts": dict(statuses),
        "first_seen_utc": iso(min((tx.get("blockTime") for tx in data if tx.get("blockTime")), default=None)),
        "last_seen_utc": iso(max((tx.get("blockTime") for tx in data if tx.get("blockTime")), default=None)),
        "top_programs": programs.most_common(8),
        "top_token_mints_seen": token_mints.most_common(8),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Helius wallet scan. No signing, no sending.")
    parser.add_argument("address", help="Wallet/account address to scan")
    parser.add_argument("--limit", type=int, default=25, help="1-100 recent transactions")
    parser.add_argument("--full", action="store_true", help="Request full tx details instead of signatures")
    parser.add_argument("--raw", action="store_true", help="Print JSON envelope")
    args = parser.parse_args()
    address = require_address(args.address)
    limit = max(1, min(args.limit, 100))

    balance = rpc_request("getBalance", [address, {"commitment": "finalized"}])
    token_accounts = rpc_request(
        "getTokenAccountsByOwner",
        [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}],
    )
    tx_result = rpc_request(
        "getTransactionsForAddress",
        [
            address,
            {
                "transactionDetails": "full" if args.full else "signatures",
                "limit": limit,
                "sortOrder": "desc",
                "filters": {"tokenAccounts": "balanceChanged"},
            },
        ],
    )
    tx_data = tx_result.get("data", []) if isinstance(tx_result, dict) else []
    token_values = []
    for entry in (token_accounts.get("value") or [])[:25]:
        info = (((entry.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        amount = (info.get("tokenAmount") or {})
        ui = amount.get("uiAmountString") or amount.get("uiAmount")
        mint = info.get("mint")
        if mint and ui not in (None, "0", 0):
            token_values.append({"mint": mint, "amount": str(ui)})

    summary = {
        "ok": True,
        "mode": "read_only_wallet_scan",
        "address": address,
        "sol_balance": (balance.get("value", 0) / LAMPORTS_PER_SOL) if isinstance(balance, dict) else None,
        "token_account_count": len(token_accounts.get("value") or []) if isinstance(token_accounts, dict) else None,
        "nonzero_token_accounts_sample": token_values[:10],
        "transaction_summary": summarize_transactions(tx_data),
        "pagination_token": tx_result.get("paginationToken") if isinstance(tx_result, dict) else None,
        "risk_notes": [
            "Classify wallet before calling it smart money.",
            "Funding source and realized exits require deeper transaction expansion.",
            "This script is read-only and cannot sign/send/swap.",
        ],
    }
    if args.raw:
        safe_print(summary)
        return
    print(f"## Wallet Read\n- Address: `{address}`")
    print(f"- SOL balance: {summary['sol_balance']}")
    print(f"- Token accounts: {summary['token_account_count']}")
    print(f"- Recent tx count scanned: {summary['transaction_summary']['count']}")
    print(f"- First/last in sample: {summary['transaction_summary']['first_seen_utc']} → {summary['transaction_summary']['last_seen_utc']}")
    print("\n## Risk Flags")
    for note in summary["risk_notes"]:
        print(f"- {note}")
    print("\n## Action\nStudy / classify. No execution authority.")


if __name__ == "__main__":
    main()
