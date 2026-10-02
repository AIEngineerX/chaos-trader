#!/usr/bin/env python3
"""Smart wallet tracker enrichment for Chaos; the wallet history read works on any Solana RPC, and Helius adds the Wallet API parts (identity, balances, transfer edges).

Read-only. No signing, sending, swapping, alerts, posting, or wallet connection.
Fetches wallet txs/transfers, normalizes transfer-aware events, and writes
approximate per-mint positions into smart_wallets.sqlite.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helius_common import WALLET_API_BASE as HELIUS_WALLET_API, is_helius_endpoint, require_address, rpc_request, wallet_api_key

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
DEFAULT_SCHEMA = PROFILE_HOME / "trading" / "db" / "smart_wallets_schema.sql"
SHIPPED_SCHEMA = Path(__file__).resolve().parent.parent / "schemas" / "smart_wallets_schema.sql"
SOL_NATIVE = "So11111111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {
    SOL_NATIVE,
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
LAMPORTS = 1_000_000_000
# Wallet events come from one of two on-chain fetch paths; every reader accepts both.
ONCHAIN_SOURCE_IDS = ("helius_rpc", "solana_rpc")
# SQL list for `source_id IN (...)`, built from the constant tuple above, never from input.
ONCHAIN_SOURCE_SQL = ",".join(f"'{s}'" for s in ONCHAIN_SOURCE_IDS)
# Public mainnet-beta allows about 40 calls per 10 s per method; one getTransaction per signature stays under it.
STANDARD_RPC_CALL_GAP_S = 0.25


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ts_utc(block_time: int | float | None) -> str | None:
    if not block_time:
        return None
    return datetime.fromtimestamp(int(block_time), timezone.utc).isoformat(timespec="seconds")


def jdump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def short(addr: str | None) -> str:
    if not addr:
        return ""
    return addr[:6] + "…" + addr[-4:]


def wallet_get(path: str, params: dict[str, Any] | None = None, timeout: int = 30, errors: list[str] | None = None) -> Any | None:
    query = {"api-key": wallet_api_key()}
    if params:
        query.update({k: v for k, v in params.items() if v is not None})
    url = f"{HELIUS_WALLET_API}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ChaosReadOnly/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if errors is not None:
            errors.append(f"{path}: {type(exc).__name__}: {str(exc)[:200]}")
        return None


def first_list(payload: Any, *names: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for n in names:
            v = payload.get(n)
            if isinstance(v, list):
                return v
    return []


def pk(k: Any) -> str | None:
    if isinstance(k, dict):
        return k.get("pubkey")
    if isinstance(k, str):
        return k
    return None


def tx_sig(tx: dict[str, Any]) -> str | None:
    return tx.get("signature") or (((tx.get("transaction") or {}).get("signatures") or [None])[0])


def account_keys(tx: dict[str, Any]) -> list[Any]:
    return (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])


def owner_index(tx: dict[str, Any], wallet: str) -> int | None:
    for i, k in enumerate(account_keys(tx)):
        if pk(k) == wallet:
            return i
    return None


def native_sol_delta(tx: dict[str, Any], wallet: str) -> float:
    idx = owner_index(tx, wallet)
    if idx is None:
        return 0.0
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return 0.0
    return (post[idx] - pre[idx]) / LAMPORTS


def owner_token_deltas(tx: dict[str, Any], wallet: str) -> dict[str, float]:
    meta = tx.get("meta") or {}
    pre: dict[tuple[int | None, str], tuple[int, int]] = {}
    post: dict[tuple[int | None, str], tuple[int, int]] = {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == wallet and b.get("mint"):
            ui = b.get("uiTokenAmount") or {}
            pre[(b.get("accountIndex"), b["mint"])] = (int(ui.get("amount") or 0), int(ui.get("decimals") or 0))
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == wallet and b.get("mint"):
            ui = b.get("uiTokenAmount") or {}
            post[(b.get("accountIndex"), b["mint"])] = (int(ui.get("amount") or 0), int(ui.get("decimals") or 0))
    out: dict[str, float] = defaultdict(float)
    for key in set(pre) | set(post):
        pr, dec = pre.get(key, (0, post.get(key, (0, 0))[1]))
        po, dec2 = post.get(key, (0, dec))
        dec = dec2
        delta_raw = po - pr
        if delta_raw:
            out[key[1]] += delta_raw / (10 ** dec)
    return {m: d for m, d in out.items() if abs(d) > 0}


def fee_sol(tx: dict[str, Any], wallet: str) -> float:
    meta = tx.get("meta") or {}
    # Fee is paid by first signer, usually the wallet. Count only when wallet is key 0.
    keys = account_keys(tx)
    if keys and pk(keys[0]) == wallet:
        return float(meta.get("fee") or 0) / LAMPORTS
    return 0.0


def classify_tx(wallet: str, tx: dict[str, Any]) -> list[dict[str, Any]]:
    sig = tx_sig(tx)
    bt = tx.get("blockTime")
    t_utc = ts_utc(bt)
    sd = native_sol_delta(tx, wallet)
    fee = fee_sol(tx, wallet)
    td = owner_token_deltas(tx, wallet)
    non_sol = {m: d for m, d in td.items() if m not in {SOL_NATIVE, WSOL}}
    wsol = td.get(WSOL, 0.0)
    effective_sol = sd + wsol
    rows: list[dict[str, Any]] = []
    # fetch_txs_standard stamps its transactions so each derived row says how it was read.
    fetch = {"fetch": tx["_fetch"]} if tx.get("_fetch") else {}

    if tx.get("err") or ((tx.get("meta") or {}).get("err")):
        return [{"wallet": wallet, "signature": sig, "block_time_utc": t_utc, "event_type": "failed", "sol_delta": sd, "fee_sol": fee, "metadata": {"err": tx.get("err") or (tx.get("meta") or {}).get("err"), **fetch}}]

    if not non_sol and abs(sd) > 0.000001:
        event_type = "sol_transfer_in" if sd > 0 else "sol_transfer_out"
        rows.append({"wallet": wallet, "signature": sig, "block_time_utc": t_utc, "event_type": event_type, "sol_delta": sd, "fee_sol": fee, "metadata": {"pure_native_sol": True, **fetch}})
        return rows

    # One row per non-SOL mint delta. Approximate buy/sell by token delta and effective SOL delta.
    for mint, delta in non_sol.items():
        event_type = "buy" if delta > 0 and effective_sol < 0 else "sell" if delta < 0 and effective_sol > 0 else "token_transfer_in" if delta > 0 else "token_transfer_out"
        rows.append({
            "wallet": wallet,
            "mint": mint,
            "signature": sig,
            "block_time_utc": t_utc,
            "event_type": event_type,
            "token_delta": delta,
            "sol_delta": effective_sol,
            "fee_sol": fee,
            "confidence": "medium" if event_type in {"buy", "sell"} and len(non_sol) == 1 else "low",
            "metadata": {"native_sol_delta": sd, "wsol_delta": wsol, "non_sol_mint_count": len(non_sol), **fetch},
        })
    return rows


def ensure_db(con: sqlite3.Connection) -> None:
    if SHIPPED_SCHEMA.exists():
        con.executescript(SHIPPED_SCHEMA.read_text(encoding="utf-8"))
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_notes_fts'").fetchone():
            fts = SHIPPED_SCHEMA.with_name("smart_wallets_notes_fts.sql")
            if fts.exists():
                con.executescript(fts.read_text(encoding="utf-8"))
    else:
        con.executescript(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT OR REPLACE INTO sources(source_id,source_type,display_name,status,metadata_json,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", ("helius_rpc", "helius", "Helius RPC / Wallet API", "active", jdump({"boundary": "read_only"})))
    con.execute("INSERT OR REPLACE INTO sources(source_id,source_type,display_name,status,metadata_json,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)", ("solana_rpc", "rpc", "Standard Solana JSON-RPC", "active", jdump({"boundary": "read_only", "fetch": "standard_rpc"})))
    con.commit()


def current_source_id() -> str:
    """Source id for rows written now: `helius_rpc` on a Helius endpoint, `solana_rpc` on any other RPC."""
    return "helius_rpc" if is_helius_endpoint() else "solana_rpc"


def upsert_wallet(con: sqlite3.Connection, wallet: str, **kw: Any) -> None:
    con.execute(
        """
        INSERT INTO wallets(address,handle,display_name,identity_name,identity_source,identity_type,category,sol_balance,total_usd_value,token_count,top_holding_symbol,top_holding_usd,metadata_json,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(address) DO UPDATE SET
          handle=COALESCE(excluded.handle,wallets.handle), display_name=COALESCE(excluded.display_name,wallets.display_name),
          identity_name=COALESCE(excluded.identity_name,wallets.identity_name), identity_source=COALESCE(excluded.identity_source,wallets.identity_source),
          identity_type=COALESCE(excluded.identity_type,wallets.identity_type), category=COALESCE(excluded.category,wallets.category),
          sol_balance=COALESCE(excluded.sol_balance,wallets.sol_balance), total_usd_value=COALESCE(excluded.total_usd_value,wallets.total_usd_value),
          token_count=COALESCE(excluded.token_count,wallets.token_count), top_holding_symbol=COALESCE(excluded.top_holding_symbol,wallets.top_holding_symbol),
          top_holding_usd=COALESCE(excluded.top_holding_usd,wallets.top_holding_usd), metadata_json=COALESCE(excluded.metadata_json,wallets.metadata_json), updated_at=CURRENT_TIMESTAMP
        """,
        (wallet, kw.get("handle"), kw.get("display_name"), kw.get("identity_name"), kw.get("identity_source"), kw.get("identity_type"), kw.get("category"), kw.get("sol_balance"), kw.get("total_usd_value"), kw.get("token_count"), kw.get("top_holding_symbol"), kw.get("top_holding_usd"), jdump(kw.get("metadata", {}))),
    )


def upsert_token(con: sqlite3.Connection, mint: str, **kw: Any) -> None:
    if not mint or mint in {SOL_NATIVE, WSOL}:
        return
    con.execute(
        """
        INSERT INTO tokens(mint,symbol,name,latest_market_cap_usd,metadata_json,updated_at)
        VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(mint) DO UPDATE SET symbol=COALESCE(excluded.symbol,tokens.symbol), name=COALESCE(excluded.name,tokens.name), latest_market_cap_usd=COALESCE(excluded.latest_market_cap_usd,tokens.latest_market_cap_usd), updated_at=CURRENT_TIMESTAMP
        """,
        (mint, kw.get("symbol"), kw.get("name"), kw.get("market_cap"), jdump(kw.get("metadata", {}))),
    )


def insert_event(con: sqlite3.Connection, run_id: str, e: dict[str, Any], *, source_id: str) -> None:
    mint = e.get("mint")
    if mint:
        upsert_token(con, mint)
    sig = e.get("signature")
    if sig:
        con.execute("INSERT OR IGNORE INTO transactions(signature,block_time_utc,fee_lamports,err_json,source_id,raw_json) VALUES(?,?,?,?,?,?)", (sig, e.get("block_time_utc"), int((e.get("fee_sol") or 0) * LAMPORTS), jdump(e.get("metadata", {}).get("err")) if e.get("event_type") == "failed" else None, source_id, jdump({"smart_wallet_tracker": True})))
    con.execute(
        """
        INSERT INTO wallet_token_events(wallet,mint,signature,block_time_utc,event_type,side,token_delta,sol_delta,fee_sol,source_id,run_id,confidence,metadata_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (e["wallet"], mint, sig, e.get("block_time_utc"), e.get("event_type"), e.get("event_type"), e.get("token_delta"), e.get("sol_delta"), e.get("fee_sol"), source_id, run_id, e.get("confidence", "medium"), jdump(e.get("metadata", {}))),
    )


def ingest_wallet_api(con: sqlite3.Connection, wallet: str, run_id: str, limit: int, *, source_id: str) -> dict[str, Any]:
    api_errors: list[str] = []
    ident = wallet_get(f"/v1/wallet/{wallet}/identity", errors=api_errors)
    funded = wallet_get(f"/v1/wallet/{wallet}/funded-by", errors=api_errors)
    balances = wallet_get(f"/v1/wallet/{wallet}/balances", {"limit": min(limit, 100)}, errors=api_errors)
    transfers = wallet_get(f"/v1/wallet/{wallet}/transfers", {"limit": min(limit, 100)}, errors=api_errors)
    meta: dict[str, Any] = {"identity_found": isinstance(ident, dict), "funded_found": isinstance(funded, dict)}
    if api_errors:
        meta["api_errors"] = api_errors
    kw: dict[str, Any] = {"metadata": {}}
    if isinstance(ident, dict):
        kw.update({"identity_name": ident.get("name"), "display_name": ident.get("name"), "identity_source": "helius_wallet_api", "identity_type": ident.get("type"), "category": ident.get("category"), "handle": (ident.get("website") or "").replace("https://x.com/", "") if ident.get("website") else None})
        kw["metadata"]["identity"] = {k: ident.get(k) for k in ("name", "category", "website", "domainNames")}
    if isinstance(balances, dict):
        toks = first_list(balances, "tokens", "tokenBalances", "balances", "items")
        top = None
        total_usd = balances.get("totalValueUsd") or balances.get("totalUsd") or balances.get("portfolioValueUsd")
        max_usd = -1.0
        for item in toks:
            if not isinstance(item, dict):
                continue
            usd = item.get("valueUsd") or item.get("usdValue") or item.get("totalPrice")
            try:
                usd_f = float(usd) if usd is not None else -1.0
            except Exception:
                usd_f = -1.0
            mint = item.get("mint") or item.get("id") or item.get("tokenAddress") or item.get("address")
            upsert_token(con, mint or "", symbol=item.get("symbol") or item.get("tokenSymbol"), name=item.get("name"), metadata={"wallet_api_balance": True})
            if usd_f > max_usd:
                max_usd = usd_f; top = item
        kw.update({"total_usd_value": total_usd, "token_count": len(toks)})
        if top:
            kw.update({"top_holding_symbol": top.get("symbol") or top.get("tokenSymbol") or top.get("name"), "top_holding_usd": max_usd if max_usd >= 0 else None})
    upsert_wallet(con, wallet, **kw)
    if isinstance(funded, dict):
        funder = funded.get("funder")
        if funder:
            upsert_wallet(con, funder, display_name=funded.get("funderName"), category=funded.get("funderType"), identity_source="helius_wallet_api")
            con.execute("INSERT INTO wallet_edges(src_wallet,dst_wallet,edge_type,mint,signature,amount,symbol,observed_at_utc,confidence,source_id,run_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (funder, wallet, "funded_by", funded.get("mint"), funded.get("signature"), funded.get("amount"), funded.get("symbol"), funded.get("date"), "high", source_id, run_id, jdump({"funderName": funded.get("funderName"), "funderType": funded.get("funderType")})))
    # Direct transfer edges from wallet API where counterparty exists.
    transfer_rows = first_list(transfers, "transfers", "items", "data")
    edges = 0
    for r in transfer_rows[:limit]:
        if not isinstance(r, dict):
            continue
        cp = r.get("counterparty") or r.get("fromUserAccount") or r.get("toUserAccount") or r.get("from") or r.get("to")
        if not cp or cp == wallet:
            continue
        direction = r.get("direction")
        mint = r.get("mint")
        if direction == "in":
            src, dst, typ = cp, wallet, "received_token_from" if mint not in {SOL_NATIVE, WSOL} else "received_sol_from"
        else:
            src, dst, typ = wallet, cp, "sent_token_to" if mint not in {SOL_NATIVE, WSOL} else "sent_sol_to"
        upsert_wallet(con, cp)
        con.execute("INSERT INTO wallet_edges(src_wallet,dst_wallet,edge_type,mint,signature,amount,symbol,observed_at_utc,confidence,source_id,run_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (src, dst, typ, mint, r.get("signature"), r.get("amount"), r.get("symbol"), ts_utc(r.get("timestamp")) or r.get("date"), "medium", source_id, run_id, jdump({"direction": direction})))
        edges += 1
    meta["transfer_edges"] = edges
    return meta


def fetch_txs_standard(wallet: str, limit: int, pages: int, *, rpc=rpc_request, stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Wallet history on any JSON-RPC: page getSignaturesForAddress, then one jsonParsed getTransaction per signature.

    Returns getTransaction results, the shape classify_tx reads, newest first, at most `limit` of them.
    Only transactions that name the wallet are listed. Helius's balanceChanged filter also finds
    token-account-only touches, so incoming SPL transfers can be missing here.
    When `stats` is given, it receives `null_transactions`: signatures the node listed but returned no transaction for.
    """
    txs: list[dict[str, Any]] = []
    if stats is not None:
        stats["null_transactions"] = 0
    before = None
    try:
        for _ in range(max(1, pages)):
            opts: dict[str, Any] = {"limit": min(limit, 100)}
            if before:
                opts["before"] = before
            sigs = rpc("getSignaturesForAddress", [wallet, opts], timeout=45)
            if not sigs:
                break
            for row in sigs:
                if len(txs) >= limit:
                    break
                tx = rpc("getTransaction", [row["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}], timeout=45)
                time.sleep(STANDARD_RPC_CALL_GAP_S)
                if tx is None:
                    if stats is not None:
                        stats["null_transactions"] += 1
                    continue
                tx["_fetch"] = "standard_rpc"
                txs.append(tx)
            if len(txs) >= limit:
                break
            before = sigs[-1]["signature"]
    except SystemExit as exc:
        raise RuntimeError(f"RPC transaction fetch failed for {wallet}: {exc}") from exc
    return txs


def fetch_txs(wallet: str, limit: int, pages: int, stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    if not is_helius_endpoint():
        return fetch_txs_standard(wallet, limit, pages, rpc=rpc_request, stats=stats)
    txs: list[dict[str, Any]] = []
    pagination_token = None
    for _ in range(max(1, pages)):
        params: dict[str, Any] = {"transactionDetails": "full", "limit": min(limit, 100), "sortOrder": "desc", "maxSupportedTransactionVersion": 0, "filters": {"tokenAccounts": "balanceChanged"}}
        if pagination_token:
            params["paginationToken"] = pagination_token
        try:
            res = rpc_request("getTransactionsForAddress", [wallet, params], timeout=45, retries=1)
        except SystemExit as exc:
            raise RuntimeError(f"Helius transaction fetch failed for {wallet}: {exc}") from exc
        rows = res.get("data", []) if isinstance(res, dict) else []
        if not rows:
            break
        txs.extend(rows)
        pagination_token = res.get("paginationToken") if isinstance(res, dict) else None
        if not pagination_token or len(rows) < min(limit, 100):
            break
        time.sleep(0.15)
    return txs


def rebuild_positions(con: sqlite3.Connection, wallet: str) -> list[dict[str, Any]]:
    # Use all on-chain RPC events currently stored for this wallet. Secondary-export rows stay separate.
    rows = con.execute(
        f"""
        SELECT mint,event_type,token_delta,sol_delta,fee_sol,block_time_utc,confidence,metadata_json
        FROM wallet_token_events
        WHERE wallet=? AND source_id IN ({ONCHAIN_SOURCE_SQL})
        ORDER BY block_time_utc ASC, id ASC
        """,
        (wallet,),
    ).fetchall()
    pos: dict[str, dict[str, Any]] = defaultdict(lambda: {"wallet": wallet, "mint": None, "opened": None, "closed": None, "buy_count": 0, "sell_count": 0, "sol_spent": 0.0, "sol_received": 0.0, "transfer_in_sol": 0.0, "transfer_out_sol": 0.0, "transfer_in_tokens": 0.0, "transfer_out_tokens": 0.0, "fees_sol": 0.0, "remaining": 0.0, "cost_qty": 0.0, "open_cost_sol": 0.0, "matched_sell_qty": 0.0, "unmatched_sell_qty": 0.0, "realized_pnl_sol": 0.0, "realized_matches": 0, "max_position_sol": 0.0, "contaminated": 0, "first_ts": None, "last_ts": None})
    pure_in = pure_out = 0.0
    sol_move_times: list[str] = []
    sol_move_untimed = False
    for mint, et, td, sd, fee, t, conf, meta_s in rows:
        sd = float(sd or 0.0); td = float(td or 0.0); fee = float(fee or 0.0)
        if et in ("sol_transfer_in", "sol_transfer_out"):
            if et == "sol_transfer_in":
                pure_in += sd
            else:
                pure_out += abs(sd)
            if t:
                sol_move_times.append(t)
            else:
                sol_move_untimed = True
            continue
        if not mint or mint in QUOTE_MINTS:
            continue
        p = pos[mint]; p["mint"] = mint
        p["opened"] = p["opened"] or t; p["closed"] = t; p["last_ts"] = t; p["first_ts"] = p["first_ts"] or t
        p["fees_sol"] += fee
        p["remaining"] += td
        if et == "buy":
            bought_qty = max(0.0, td)
            spent = max(0.0, -sd)
            p["buy_count"] += 1; p["sol_spent"] += spent
            p["cost_qty"] += bought_qty
            # effective_sol comes from the wallet's post-minus-pre balance and is
            # already net of network/priority fees. Track fee separately for
            # attribution, but do not add it to cost basis a second time.
            p["open_cost_sol"] += spent
            p["max_position_sol"] = max(p["max_position_sol"], p["open_cost_sol"])
        elif et == "sell":
            sold_qty = max(0.0, -td)
            proceeds = max(0.0, sd)
            p["sell_count"] += 1; p["sol_received"] += proceeds
            matched_qty = min(sold_qty, p["cost_qty"])
            if sold_qty > 0 and matched_qty > 0 and p["cost_qty"] > 0:
                matched_fraction = matched_qty / sold_qty
                cost_out = p["open_cost_sol"] * (matched_qty / p["cost_qty"])
                matched_proceeds = proceeds * matched_fraction
                p["realized_pnl_sol"] += matched_proceeds - cost_out
                p["open_cost_sol"] -= cost_out
                p["cost_qty"] -= matched_qty
                p["matched_sell_qty"] += matched_qty
                p["realized_matches"] += 1
            if sold_qty > matched_qty:
                p["unmatched_sell_qty"] += sold_qty - matched_qty
        elif et == "token_transfer_in":
            p["transfer_in_tokens"] += max(0.0, td); p["contaminated"] = 1
        elif et == "token_transfer_out":
            p["transfer_out_tokens"] += max(0.0, -td); p["contaminated"] = 1
        if conf == "low":
            p["contaminated"] = 1
    # Pure SOL movement only contaminates positions whose trade window it falls inside;
    # an untimed SOL move keeps the old conservative all-positions behavior.
    if pure_in or pure_out:
        for p in pos.values():
            p["transfer_in_sol"] = pure_in; p["transfer_out_sol"] = pure_out
            if sol_move_untimed or any(
                p["first_ts"] and p["last_ts"] and p["first_ts"] <= t <= p["last_ts"]
                for t in sol_move_times
            ):
                p["contaminated"] = 1
    out = []
    for p in pos.values():
        has_cost_basis = p["buy_count"] > 0 and p["realized_matches"] > 0
        fully_matched = p["buy_count"] > 0 and p["sell_count"] > 0 and p["cost_qty"] <= 1e-9 and p["unmatched_sell_qty"] <= 1e-9
        status = "closed" if fully_matched else "open" if p["buy_count"] > 0 and p["cost_qty"] > 1e-9 else "unresolved"
        pnl = p["realized_pnl_sol"] if has_cost_basis else None
        hold = None
        try:
            if p["opened"] and p["closed"]:
                dt1 = datetime.fromisoformat(p["opened"].replace("Z", "+00:00")); dt2 = datetime.fromisoformat(p["closed"].replace("Z", "+00:00"))
                hold = int((dt2 - dt1).total_seconds())
        except Exception:
            hold = None
        p.update({"realized_pnl_sol": pnl, "status": status, "hold_seconds": hold})
        out.append(p)
    # Replace Helius-derived positions for wallet.
    con.execute("DELETE FROM positions WHERE wallet=?", (wallet,))
    for p in out:
        con.execute(
            """
            INSERT INTO positions(wallet,mint,opened_at_utc,closed_at_utc,status,buy_count,sell_count,sol_spent,sol_received,transfer_in_sol,transfer_out_sol,transfer_in_tokens,transfer_out_tokens,fees_sol,realized_pnl_sol,remaining_tokens,max_position_sol,hold_seconds,transfer_contaminated,confidence,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (wallet, p["mint"], p["opened"], p["closed"], p["status"], p["buy_count"], p["sell_count"], p["sol_spent"], p["sol_received"], p["transfer_in_sol"], p["transfer_out_sol"], p["transfer_in_tokens"], p["transfer_out_tokens"], p["fees_sol"], p["realized_pnl_sol"], p["remaining"], p["max_position_sol"], p["hold_seconds"], p["contaminated"], "low" if p["contaminated"] else "medium", jdump({"source": "helius_rpc_sample", "accounting": "matched_average_cost_v1", "open_cost_sol": round(p["open_cost_sol"], 12), "matched_sell_qty": p["matched_sell_qty"], "unmatched_sell_qty": p["unmatched_sell_qty"]})),
        )
    return sorted(out, key=lambda x: x.get("realized_pnl_sol") or 0, reverse=True)


def enrich_wallet(con: sqlite3.Connection, wallet: str, limit: int, pages: int, include_api: bool) -> dict[str, Any]:
    wallet = require_address(wallet, "wallet")
    source_id = current_source_id()
    if include_api and source_id == "solana_rpc":
        print("smart_wallet_tracker: Wallet API skipped; it needs a Helius endpoint, so identity, balances and transfer edges stay empty", file=sys.stderr)
        include_api = False
    rid = "helius-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    con.execute("INSERT OR REPLACE INTO ingestion_runs(run_id,source_id,started_at,status,notes) VALUES(?,?,?,?,?)", (rid, source_id, now_utc(), "running", f"wallet={wallet}"))
    # Idempotent wallet enrichment: remove prior on-chain-derived volatile rows for this wallet,
    # from either fetch path, so a provider switch cannot double-count an event.
    # Keep secondary source rows and raw transaction cache intact.
    con.execute(f"DELETE FROM wallet_token_events WHERE wallet=? AND source_id IN ({ONCHAIN_SOURCE_SQL})", (wallet,))
    con.execute("DELETE FROM positions WHERE wallet=?", (wallet,))
    con.execute(f"DELETE FROM wallet_scores WHERE wallet=? AND source_id IN ({ONCHAIN_SOURCE_SQL})", (wallet,))
    con.execute(f"DELETE FROM wallet_edges WHERE source_id IN ({ONCHAIN_SOURCE_SQL}) AND (src_wallet=? OR dst_wallet=?)", (wallet, wallet))
    upsert_wallet(con, wallet)
    api_meta = ingest_wallet_api(con, wallet, rid, limit, source_id=source_id) if include_api else {}
    fetch_stats = {"null_transactions": 0}
    txs = fetch_txs(wallet, limit, pages, fetch_stats)
    event_counts = Counter()
    for tx in txs:
        for e in classify_tx(wallet, tx):
            insert_event(con, rid, e, source_id=source_id)
            event_counts[e.get("event_type")] += 1
    positions = rebuild_positions(con, wallet)
    closed = [p for p in positions if p["status"] == "closed"]
    clean_closed = [p for p in closed if not p.get("contaminated") and p.get("realized_pnl_sol") is not None]
    wins = [p for p in clean_closed if p["realized_pnl_sol"] > 0]
    losses = [p for p in clean_closed if p["realized_pnl_sol"] < 0]
    total_pnl = sum(p["realized_pnl_sol"] for p in clean_closed)
    contaminated = sum(1 for p in positions if p.get("contaminated"))
    win_rate = (len(wins) / len(clean_closed)) if clean_closed else None
    score = 0.0
    if positions:
        score += min(40, max(-40, total_pnl * 4))
        score += (win_rate or 0) * 25
        score += min(15, len(clean_closed) * 2)
        score -= contaminated * 2
    copyability = "study"
    if score >= 45 and contaminated <= max(1, len(positions)//2): copyability = "watch"
    if score >= 65 and len(clean_closed) >= 3 and contaminated == 0: copyability = "candidate"
    if score < 15: copyability = "avoid" if positions else "unknown"
    con.execute(
        "INSERT INTO wallet_scores(wallet,score,classification,confidence,realized_pnl_sol,win_rate,buy_count,sell_count,transfer_contamination_score,copyability,reasons_json,source_id,scored_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (wallet, round(score, 3), "helius_matched_cost_basis_v2_net_wallet_delta", "low" if contaminated else "medium", round(total_pnl, 9), None if win_rate is None else round(win_rate, 3), event_counts.get("buy", 0), event_counts.get("sell", 0), contaminated / max(1, len(positions)), copyability, jdump({"events": dict(event_counts), "positions": len(positions), "closed": len(closed), "clean_closed": len(clean_closed), "wins": len(wins), "losses": len(losses), "api": api_meta}), source_id, now_utc()),
    )
    con.execute("UPDATE ingestion_runs SET completed_at=?, status=?, row_counts_json=? WHERE run_id=?", (now_utc(), "completed", jdump({"txs": len(txs), "null_transactions": fetch_stats["null_transactions"], "events": sum(event_counts.values()), "positions": len(positions)}), rid))
    con.commit()
    return {"wallet": wallet, "run_id": rid, "txs": len(txs), "null_transactions": fetch_stats["null_transactions"], "events": dict(event_counts), "positions": len(positions), "closed_positions": len(closed), "wins": len(wins), "losses": len(losses), "sample_realized_pnl_sol": round(total_pnl, 9), "contaminated_positions": contaminated, "score": round(score, 3), "copyability": copyability, "top_positions": [{"mint": p["mint"], "pnl": None if p["realized_pnl_sol"] is None else round(p["realized_pnl_sol"], 6), "spent": round(p["sol_spent"], 6), "received": round(p["sol_received"], 6), "status": p["status"], "contaminated": p["contaminated"]} for p in positions[:8]]}


def candidate_wallets(con: sqlite3.Connection, top: int) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT ws.wallet
        FROM wallet_scores ws
        LEFT JOIN wallets w ON w.address=ws.wallet
        WHERE ws.wallet IS NOT NULL
        ORDER BY COALESCE(ws.actor_score, ws.score, ws.pnl_all, ws.trade_usd_sum, 0) DESC
        LIMIT ?
        """,
        (top,),
    ).fetchall()
    return [r[0] for r in rows]


def discovered_wallets(con: sqlite3.Connection, top: int) -> list[str]:
    """Edge-connected wallets (funders, counterparties) that have never been scored.

    These are the tracker's own on-chain finds; enriching them is what turns
    funding-graph expansion into scoreable discovery candidates.
    """
    rows = con.execute(
        """
        SELECT w.address, COUNT(*) AS edge_count
        FROM wallets w
        JOIN wallet_edges e ON e.src_wallet = w.address OR e.dst_wallet = w.address
        LEFT JOIN wallet_scores ws ON ws.wallet = w.address
        WHERE ws.wallet IS NULL
          AND LOWER(COALESCE(w.category, '')) NOT IN ('exchange', 'cex', 'program', 'bridge')
        GROUP BY w.address
        ORDER BY edge_count DESC, w.address
        LIMIT ?
        """,
        (top,),
    ).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="Transfer-aware Helius enrichment for smart-wallet SQLite ledger")
    ap.add_argument("wallet", nargs="?", help="Wallet to enrich")
    ap.add_argument("--top-db", type=int, default=0, help="Enrich top N wallets from DB scoring")
    ap.add_argument("--discovered", type=int, default=0, help="Enrich top N never-scored wallets found via funding/transfer edges")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--no-wallet-api", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db).expanduser())
    con.execute("PRAGMA foreign_keys=ON")
    ensure_db(con)
    wallets: list[str] = []
    if args.wallet:
        wallets.append(args.wallet)
    if args.top_db:
        wallets.extend(candidate_wallets(con, args.top_db))
    if args.discovered:
        wallets.extend(discovered_wallets(con, args.discovered))
    seen = []
    for w in wallets:
        if w not in seen:
            seen.append(w)
    if not seen:
        raise SystemExit("provide a wallet, --top-db N, or --discovered N")
    results = []
    for w in seen:
        try:
            results.append(enrich_wallet(con, w, max(1, min(args.limit, 100)), max(1, args.pages), not args.no_wallet_api))
        except Exception as exc:
            # Discard the failed wallet's pending deletes; without this the next
            # successful wallet's commit() would seal them.
            try:
                con.rollback()
            except sqlite3.Error:
                pass
            results.append({"wallet": w, "ok": False, "error": str(exc)[:500]})
    con.close()
    failures = sum(1 for r in results if r.get("ok") is False)
    print(json.dumps({"ok": failures == 0, "mode": "smart_wallet_tracker", "count": len(results), "failed": failures, "results": results}, indent=2, sort_keys=True))
    if failures:
        # A partially failed batch must not report success to cron/health.
        raise SystemExit(1)


if __name__ == "__main__":
    main()
