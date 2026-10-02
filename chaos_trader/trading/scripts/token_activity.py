#!/usr/bin/env python3
"""Read-only Helius token/mint activity probe for Chaos."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from helius_common import require_address, rpc_request, safe_print


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def rpc_optional(method: str, params: list[Any]) -> tuple[Any | None, str | None]:
    """Return (result, error) so one noisy RPC method does not kill the whole read."""
    try:
        return rpc_request(method, params), None
    except SystemExit as exc:
        return None, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Helius token/mint probe. No swaps, no buys, no sells.")
    parser.add_argument("mint", help="SPL token mint address")
    parser.add_argument("--limit", type=int, default=25, help="1-100 recent mint-account txs")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON envelope")
    args = parser.parse_args()
    mint = require_address(args.mint, "mint")
    limit = max(1, min(args.limit, 100))

    supply = rpc_request("getTokenSupply", [mint, {"commitment": "finalized"}])
    largest, largest_error = rpc_optional("getTokenLargestAccounts", [mint, {"commitment": "finalized"}])
    tx_result = rpc_request(
        "getTransactionsForAddress",
        [mint, {"transactionDetails": "signatures", "limit": limit, "sortOrder": "desc"}],
    )
    tx_data = tx_result.get("data", []) if isinstance(tx_result, dict) else []
    holders = []
    for h in ((largest or {}).get("value") or [])[:10]:
        holders.append({"address": h.get("address"), "amount": h.get("uiAmountString") or h.get("uiAmount")})
    summary = {
        "ok": True,
        "mode": "read_only_token_activity",
        "mint": mint,
        "supply": (supply.get("value") or {}) if isinstance(supply, dict) else supply,
        "largest_accounts_sample": holders,
        "largest_accounts_error": largest_error,
        "recent_mint_account_tx_count": len(tx_data),
        "recent_first_seen_utc": iso(min((tx.get("blockTime") for tx in tx_data if tx.get("blockTime")), default=None)),
        "recent_last_seen_utc": iso(max((tx.get("blockTime") for tx in tx_data if tx.get("blockTime")), default=None)),
        "risk_notes": [
            "Top holders are token accounts, not always owner wallets; resolve owners before drawing concentration conclusions.",
            "Mint-account txs are not a complete holder-flow model; use wallet/deployer traces for conviction.",
            "This script is read-only and cannot buy/sell/swap.",
        ],
    }
    if args.raw:
        safe_print(summary)
        return
    print("## Token Read")
    print(f"- Mint: `{mint}`")
    print(f"- Supply: {summary['supply']}")
    print(f"- Largest accounts sampled: {len(holders)}")
    if largest_error:
        print("- Largest accounts: unavailable from RPC for this mint/sample")
    print(f"- Recent mint-account tx count: {summary['recent_mint_account_tx_count']}")
    print("\n## Risk Flags")
    for note in summary["risk_notes"]:
        print(f"- {note}")
    print("\n## Action\nStudy / classify. No execution authority.")


if __name__ == "__main__":
    main()
