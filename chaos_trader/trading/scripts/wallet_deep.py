#!/usr/bin/env python3
"""Read-only Helius Wallet API deep wallet probe for Chaos.

Uses Helius Wallet API Beta REST endpoints plus no signing/sending/swapping.
Returns a compact intelligence envelope suitable for a short chat or CLI summary.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

import no_redirect
from helius_common import WALLET_API_BASE as BASE_URL, require_address, safe_print, wallet_api_key

try:
    from chaos_trader import __version__ as _VERSION
except ImportError:  # scripts also run flat from the chaos home, where the package may not be importable
    _VERSION = "unknown"


def wallet_get(path: str, params: dict[str, Any] | None = None, *, timeout: int = 30, retries: int = 2) -> tuple[Any | None, str | None, int | None]:
    """GET a Wallet API endpoint. Returns (payload, error, http_status)."""
    query = {"api-key": wallet_api_key()}
    if params:
        query.update({k: v for k, v in params.items() if v is not None})
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            # Helius Wallet API currently sits behind Cloudflare; avoid the
            # default Python-urllib signature where possible.
            "User-Agent": f"chaos-trader/{_VERSION}",
        },
        method="GET",
    )
    last_error: str | None = None
    last_status: int | None = None
    for attempt in range(retries + 1):
        try:
            # The URL carries the key, so no redirect is followed: a 3xx raises HTTPError.
            with no_redirect.open_no_redirect(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), None, resp.status
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            if 300 <= exc.code < 400:
                exc.close()
                return None, "Wallet API unavailable: redirect refused", exc.code
            try:
                body = exc.read().decode("utf-8", "replace")[:1000]
            except Exception:
                body = str(exc)
            # 404 identity/funded-by can simply mean unknown; do not retry it.
            if exc.code == 404:
                return None, "not_found", exc.code
            last_error = body or str(exc)
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                return None, last_error, exc.code
        except urllib.error.URLError as exc:
            last_error = str(exc)
            if attempt >= retries:
                return None, last_error, None
        time.sleep(0.5 * (2**attempt))
    return None, last_error or "unknown Wallet API error", last_status


def first_list(payload: Any, *names: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if isinstance(value, list):
                return value
    return []


def summarize_balances(payload: Any, limit: int) -> dict[str, Any]:
    tokens = first_list(payload, "tokens", "tokenBalances", "balances", "items")
    nfts = first_list(payload, "nfts", "nftBalances")
    top = []
    for item in tokens[:limit] if isinstance(tokens, list) else []:
        if not isinstance(item, dict):
            continue
        mint = item.get("mint") or item.get("id") or item.get("tokenAddress") or item.get("address")
        symbol = item.get("symbol") or item.get("tokenSymbol") or item.get("name")
        bal = item.get("balance") or item.get("amount") or item.get("uiAmount") or item.get("uiAmountString")
        usd = item.get("valueUsd") or item.get("usdValue") or item.get("totalPrice")
        top.append({"mint": mint, "symbol": symbol, "balance": bal, "usd_value": usd})
    total_usd = None
    if isinstance(payload, dict):
        total_usd = payload.get("totalValueUsd") or payload.get("totalUsd") or payload.get("portfolioValueUsd")
    if total_usd is None:
        usd_values = []
        for item in top:
            try:
                if item.get("usd_value") is not None:
                    usd_values.append(float(item["usd_value"]))
            except (TypeError, ValueError):
                pass
        total_usd = round(sum(usd_values), 6) if usd_values else None
    return {"token_count_sampled": len(tokens), "nft_count_sampled": len(nfts), "portfolio_usd": total_usd, "top_tokens": top}


def summarize_history(payload: Any, limit: int) -> dict[str, Any]:
    rows = first_list(payload, "history", "transactions", "items", "data")
    status = Counter()
    sources = Counter()
    sigs = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        if row.get("signature"):
            sigs.append(row.get("signature"))
        s = row.get("status") or row.get("type") or ("failed" if row.get("error") or row.get("err") else "unknown")
        status[str(s)] += 1
        src = row.get("source") or row.get("program") or row.get("platform")
        if src:
            sources[str(src)] += 1
    return {"sample_count": len(rows), "status_or_type_counts": dict(status), "top_sources": sources.most_common(8), "signature_sample": sigs[:5]}


def summarize_transfers(payload: Any, limit: int) -> dict[str, Any]:
    rows = first_list(payload, "transfers", "items", "data")
    mints = Counter()
    counterparties = Counter()
    directions = Counter()
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        mint = row.get("mint") or row.get("token") or row.get("tokenAddress")
        if mint:
            mints[str(mint)] += 1
        for key in ("counterparty", "fromUserAccount", "toUserAccount", "from", "to"):
            if row.get(key):
                counterparties[str(row[key])] += 1
        direction = row.get("direction") or row.get("type")
        if direction:
            directions[str(direction)] += 1
    return {"sample_count": len(rows), "top_mints": mints.most_common(10), "top_counterparties": counterparties.most_common(10), "direction_counts": dict(directions)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Helius Wallet API deep probe. No signing/sending/swapping.")
    parser.add_argument("address", help="Wallet address")
    parser.add_argument("--limit", type=int, default=25, help="1-100 rows per endpoint to summarize")
    parser.add_argument("--raw", action="store_true", help="Print JSON envelope")
    args = parser.parse_args()
    address = require_address(args.address)
    limit = max(1, min(args.limit, 100))

    endpoints = {
        "identity": (f"/v1/wallet/{address}/identity", None),
        "funded_by": (f"/v1/wallet/{address}/funded-by", None),
        "balances": (f"/v1/wallet/{address}/balances", {"limit": limit}),
        "history": (f"/v1/wallet/{address}/history", {"limit": limit}),
        "transfers": (f"/v1/wallet/{address}/transfers", {"limit": limit}),
    }
    raw: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    statuses: dict[str, Any] = {}
    for name, (path, params) in endpoints.items():
        payload, error, status = wallet_get(path, params)
        statuses[name] = status
        if error:
            errors[name] = error
        raw[name] = payload

    result = {
        "ok": True,
        "mode": "read_only_wallet_deep",
        "address": address,
        "api": "Helius Wallet API Beta",
        "http_statuses": statuses,
        "endpoint_errors": errors,
        "identity": raw.get("identity"),
        "funded_by": raw.get("funded_by"),
        "balances_summary": summarize_balances(raw.get("balances"), limit),
        "history_summary": summarize_history(raw.get("history"), limit),
        "transfers_summary": summarize_transfers(raw.get("transfers"), limit),
        "risk_notes": [
            "Wallet API is beta; treat response fields as useful but schema-unstable.",
            "Identity/funding labels are attribution aids, not proof of smart-money edge.",
            "Use realized exits and repeat follow-through before copying a wallet.",
            "Read-only: no signing/sending/swapping.",
        ],
    }

    if args.raw:
        safe_print(result)
        return
    print("## Wallet Deep Read")
    print(f"- Wallet: `{address}`")
    ident = result["identity"] if isinstance(result["identity"], dict) else None
    print(f"- Identity: {ident.get('name') if ident else 'unlabeled'}")
    print(f"- Funding: {'available' if result['funded_by'] else 'unresolved'}")
    print(f"- Token sample: {result['balances_summary']['token_count_sampled']}")
    print(f"- History sample: {result['history_summary']['sample_count']}")
    print(f"- Transfer sample: {result['transfers_summary']['sample_count']}")
    if errors:
        print(f"- Endpoint caveats: {sorted(errors)}")
    print("\n## Next")
    print("Classify by realized exits, funding quality, and repeat edge. No execution authority.")


if __name__ == "__main__":
    main()
