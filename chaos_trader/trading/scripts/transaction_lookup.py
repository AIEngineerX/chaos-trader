#!/usr/bin/env python3
"""Read-only Helius transaction lookup for Chaos."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from helius_common import require_signature, rpc_tx_request, safe_print


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Helius transaction lookup. No signing, no sending.")
    parser.add_argument("signature", help="Solana transaction signature")
    parser.add_argument("--raw", action="store_true", help="Print raw summarized JSON envelope")
    args = parser.parse_args()
    sig = require_signature(args.signature)
    tx = rpc_tx_request("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "finalized"}])
    if tx is None:
        result = {"ok": False, "signature": sig, "error": "Transaction not found at finalized commitment"}
        safe_print(result)
        return
    meta = tx.get("meta") or {}
    message = ((tx.get("transaction") or {}).get("message") or {})
    accounts = message.get("accountKeys") or []
    account_pubkeys = [a.get("pubkey") if isinstance(a, dict) else str(a) for a in accounts]
    instructions = message.get("instructions") or []
    token_mints = []
    for bal in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        mint = bal.get("mint")
        if mint and mint not in token_mints:
            token_mints.append(mint)
    summary = {
        "ok": True,
        "mode": "read_only_transaction_lookup",
        "signature": sig,
        "slot": tx.get("slot"),
        "block_time_utc": iso(tx.get("blockTime")),
        "failed": meta.get("err") is not None,
        "error": meta.get("err"),
        "fee_lamports": meta.get("fee"),
        "account_count": len(account_pubkeys),
        "accounts_sample": account_pubkeys[:20],
        "instruction_count": len(instructions),
        "programs_sample": [i.get("programId") or i.get("program") for i in instructions[:20] if isinstance(i, dict)],
        "token_mints_seen": token_mints[:20],
        "authority_boundary": "read-only parse only; no signing/sending/swap authority",
    }
    if args.raw:
        safe_print(summary)
        return
    print("## Transaction Read")
    print(f"- Signature: `{sig}`")
    print(f"- Slot: {summary['slot']}")
    print(f"- Block time: {summary['block_time_utc']}")
    print(f"- Failed: {summary['failed']}")
    print(f"- Fee lamports: {summary['fee_lamports']}")
    print(f"- Instruction count: {summary['instruction_count']}")
    print(f"- Token mints seen: {', '.join(summary['token_mints_seen']) if summary['token_mints_seen'] else 'none in parsed balances'}")
    print("\n## Action\nClassify relevance. No execution authority.")


if __name__ == "__main__":
    main()
