#!/usr/bin/env python3
"""Read-only wallet edge audit.

Builds a token-level ledger from Helius getTransactionsForAddress full history.
No signing. No trading. No wallet connection.
"""
from __future__ import annotations

import argparse
import csv
import json
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helius_common import require_address, rpc_request, safe_print

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
IGNORE = {WSOL, USDC}
ROOT = Path(__file__).resolve().parents[1]


def iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def ui_amount(token_balance: dict[str, Any]) -> float:
    amt = token_balance.get("uiTokenAmount") or {}
    if amt.get("uiAmount") is not None:
        return float(amt.get("uiAmount") or 0)
    return float(amt.get("uiAmountString") or 0)


def owner_token_balances(meta: dict[str, Any], side: str, owner: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for tb in meta.get(side + "TokenBalances") or []:
        if tb.get("owner") == owner:
            mint = tb.get("mint")
            if mint:
                out[mint] += ui_amount(tb)
    return out


def fetch_transactions(address: str, pages: int) -> tuple[list[dict[str, Any]], str | None]:
    all_txs: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(max(1, pages)):
        opts: dict[str, Any] = {
            "transactionDetails": "full",
            "limit": 100,
            "sortOrder": "desc",
            "filters": {"tokenAccounts": "balanceChanged"},
        }
        if token:
            opts["paginationToken"] = token
        res = rpc_request("getTransactionsForAddress", [address, opts], timeout=90, retries=3)
        data = res.get("data") or [] if isinstance(res, dict) else []
        all_txs.extend(data)
        token = res.get("paginationToken") if isinstance(res, dict) else None
        if not data or not token:
            break
    return all_txs, token


def extract_events(address: str, txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tx in txs:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        tr = tx.get("transaction") or {}
        msg = tr.get("message") or {}
        keys = msg.get("accountKeys") or []
        sig = (tr.get("signatures") or ["?"])[0]
        bt = tx.get("blockTime")
        pre = owner_token_balances(meta, "pre", address)
        post = owner_token_balances(meta, "post", address)
        deltas = {
            m: post.get(m, 0.0) - pre.get(m, 0.0)
            for m in set(pre) | set(post)
            if abs(post.get(m, 0.0) - pre.get(m, 0.0)) > 1e-12
        }
        native_sol = 0.0
        # signer is usually account 0. If not, skip native attribution rather than hallucinate.
        if keys and keys[0] == address and meta.get("preBalances") and meta.get("postBalances"):
            native_sol = (meta["postBalances"][0] - meta["preBalances"][0]) / 1_000_000_000
        quote_sol = deltas.get(WSOL, 0.0) + native_sol
        quote_usdc = deltas.get(USDC, 0.0)
        traded = [(m, d) for m, d in deltas.items() if m not in IGNORE]
        traded.sort(key=lambda x: abs(x[1]), reverse=True)
        for mint, token_delta in traded[:2]:
            rows.append(
                {
                    "ts": bt,
                    "iso": iso(bt),
                    "signature": sig,
                    "mint": mint,
                    "token_delta": token_delta,
                    "quote_sol": quote_sol,
                    "quote_usdc": quote_usdc,
                    "native_sol": native_sol,
                }
            )
    return rows


def build_ledger(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positions: dict[str, dict[str, Any]] = {}
    lots: list[dict[str, Any]] = []
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "mint": None,
            "first_seen": None,
            "last_seen": None,
            "tx_events": 0,
            "buys": 0,
            "sells": 0,
            "buy_sol": 0.0,
            "sell_sol": 0.0,
            "buy_usdc": 0.0,
            "sell_usdc": 0.0,
            "clean_lots": 0,
            "clean_wins": 0,
            "clean_losses": 0,
            "clean_realized_pnl_sol": 0.0,
            "open_qty": 0.0,
            "open_cost_sol": 0.0,
            "max_single_buy_sol": 0.0,
            "max_single_sell_sol": 0.0,
        }
    )

    for ev in sorted(events, key=lambda r: r.get("ts") or 0):
        mint = ev["mint"]
        td = float(ev["token_delta"])
        q = float(ev.get("quote_sol") or 0.0)
        u = float(ev.get("quote_usdc") or 0.0)
        s = stats[mint]
        s["mint"] = mint
        s["first_seen"] = s["first_seen"] or ev["iso"]
        s["last_seen"] = ev["iso"]
        s["tx_events"] += 1

        p = positions.setdefault(mint, {"qty": 0.0, "cost_sol": 0.0})
        if td > 0 and q < -1e-7:
            cost = -q
            p["qty"] += td
            p["cost_sol"] += cost
            s["buys"] += 1
            s["buy_sol"] += cost
            s["max_single_buy_sol"] = max(s["max_single_buy_sol"], cost)
        elif td < 0 and q > 1e-7:
            proceeds = q
            sell_qty = -td
            s["sells"] += 1
            s["sell_sol"] += proceeds
            s["max_single_sell_sol"] = max(s["max_single_sell_sol"], proceeds)
            if p["qty"] > 0 and p["cost_sol"] > 0:
                matched_qty = min(sell_qty, p["qty"])
                clean = sell_qty <= p["qty"] * 1.001
                frac_inventory = matched_qty / p["qty"] if p["qty"] else 0.0
                frac_sale = matched_qty / sell_qty if sell_qty else 0.0
                cost_basis = p["cost_sol"] * frac_inventory
                matched_proceeds = proceeds * frac_sale
                pnl = matched_proceeds - cost_basis
                lot = {
                    "mint": mint,
                    "closed_at": ev["iso"],
                    "signature": ev["signature"],
                    "clean_window": clean,
                    "sell_qty": sell_qty,
                    "matched_qty": matched_qty,
                    "proceeds_sol": matched_proceeds,
                    "raw_sale_proceeds_sol": proceeds,
                    "cost_basis_sol": cost_basis,
                    "realized_pnl_sol": pnl,
                    "roi_pct": (pnl / cost_basis * 100.0) if cost_basis else None,
                }
                lots.append(lot)
                if clean:
                    s["clean_lots"] += 1
                    s["clean_realized_pnl_sol"] += pnl
                    if pnl > 0:
                        s["clean_wins"] += 1
                    elif pnl < 0:
                        s["clean_losses"] += 1
                p["qty"] -= matched_qty
                p["cost_sol"] -= cost_basis
                if p["qty"] < 1e-9:
                    p["qty"] = 0.0
                    p["cost_sol"] = 0.0
        elif td > 0 and u < -1e-7:
            s["buys"] += 1
            s["buy_usdc"] += -u
        elif td < 0 and u > 1e-7:
            s["sells"] += 1
            s["sell_usdc"] += u

    ledger: list[dict[str, Any]] = []
    for mint, s in stats.items():
        p = positions.get(mint) or {"qty": 0.0, "cost_sol": 0.0}
        s["open_qty"] = p["qty"]
        s["open_cost_sol"] = p["cost_sol"]
        s["net_realized_flow_sol"] = s["sell_sol"] - s["buy_sol"]
        s["avg_buy_sol"] = s["buy_sol"] / s["buys"] if s["buys"] else 0.0
        s["win_rate_clean"] = s["clean_wins"] / s["clean_lots"] if s["clean_lots"] else None
        # Discovery score: large meaningful interaction + clean PnL. Not a buy signal.
        score = 0.0
        score += min(25.0, s["buy_sol"] * 1.5)
        score += min(20.0, s["max_single_buy_sol"] * 4.0)
        score += max(-30.0, min(30.0, s["clean_realized_pnl_sol"] * 3.0))
        if s["clean_lots"] and s["win_rate_clean"] is not None:
            score += (s["win_rate_clean"] - 0.5) * 20.0
        if s["open_cost_sol"] > 5 and s["clean_realized_pnl_sol"] < 0:
            score -= 10.0
        s["study_score"] = round(score, 3)
        for k, v in list(s.items()):
            if isinstance(v, float):
                s[k] = round(v, 9)
        ledger.append(dict(s))
    ledger.sort(key=lambda r: (r["study_score"], r["buy_sol"], r["sell_sol"]), reverse=True)
    return ledger, lots


def dex_lookup(mint: str) -> dict[str, Any]:
    url = "https://api.dexscreener.com/latest/dex/tokens/" + urllib.parse.quote(mint)
    req = urllib.request.Request(url, headers={"User-Agent": "ChaosWalletStudy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        pairs = data.get("pairs") or []
        if not pairs:
            return {"symbol": None, "name": None, "liquidity_usd": None, "market_cap": None, "volume_h24": None, "url": None}
        pair = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
        return {
            "symbol": (pair.get("baseToken") or {}).get("symbol"),
            "name": (pair.get("baseToken") or {}).get("name"),
            "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
            "market_cap": pair.get("marketCap") or pair.get("fdv"),
            "volume_h24": (pair.get("volume") or {}).get("h24"),
            "price_change_h24": (pair.get("priceChange") or {}).get("h24"),
            "url": pair.get("url"),
        }
    except Exception as exc:
        return {"dex_error": str(exc)[:160]}


def write_outputs(address: str, ledger: list[dict[str, Any]], lots: list[dict[str, Any]], txs: list[dict[str, Any]], events: list[dict[str, Any]], pages: int, enrich: int) -> dict[str, str]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outdir = ROOT / "alpha" / "wallet_studies" / day
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"wallet_edge_{address[:8]}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    csv_path = outdir / f"{stem}.csv"
    json_path = outdir / f"{stem}.json"
    md_path = outdir / f"{stem}.md"

    enriched: dict[str, Any] = {}
    for row in ledger[: max(0, enrich)]:
        enriched[row["mint"]] = dex_lookup(row["mint"])

    clean_lots = [x for x in lots if x.get("clean_window")]
    summary = {
        "wallet": address,
        "pages_requested": pages,
        "txs_fetched": len(txs),
        "events": len(events),
        "window_first": min((e["iso"] for e in events if e.get("iso")), default=None),
        "window_last": max((e["iso"] for e in events if e.get("iso")), default=None),
        "tokens_touched": len(ledger),
        "clean_closed_lots": len(clean_lots),
        "clean_wins": sum(1 for x in clean_lots if x["realized_pnl_sol"] > 0),
        "clean_losses": sum(1 for x in clean_lots if x["realized_pnl_sol"] < 0),
        "clean_realized_pnl_sol": round(sum(x["realized_pnl_sol"] for x in clean_lots), 9),
        "method": "Rough FIFO using owner token deltas and SOL/WSOL quote deltas from Helius full tx history. Open inventory is unmarked; price path/copyability requires time-series market enrichment.",
    }
    payload = {"summary": summary, "ledger": ledger, "closed_lots": lots, "dex_enrichment": enriched}
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    fieldnames = list(ledger[0].keys()) if ledger else ["mint"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ledger)

    def short(m: str) -> str:
        return f"{m[:8]}…{m[-4:]}"

    lines = [
        f"# Wallet Edge Study — {address}",
        "",
        "Read-only wallet study; advisory and paper decisions remain separately authorized. No live execution.",
        "",
        "## Summary",
        "",
        f"- Window: `{summary['window_first']}` → `{summary['window_last']}`",
        f"- Transactions fetched: **{summary['txs_fetched']}**",
        f"- Token events: **{summary['events']}**",
        f"- Tokens touched: **{summary['tokens_touched']}**",
        f"- Clean closed lots: **{summary['clean_closed_lots']}**",
        f"- Clean wins/losses: **{summary['clean_wins']} / {summary['clean_losses']}**",
        f"- Clean rough realized PnL: **{summary['clean_realized_pnl_sol']} SOL**",
        "",
        "## Top Study Candidates",
        "",
        "| Score | Token | Buy SOL | Sell SOL | Clean PnL | Wins/Losses | Open Cost | Market |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in ledger[:25]:
        dex = enriched.get(row["mint"], {})
        market = dex.get("symbol") or "?"
        if dex.get("liquidity_usd") is not None:
            market += f" liq=${dex.get('liquidity_usd'):,.0f}"
        lines.append(
            f"| {row['study_score']} | `{short(row['mint'])}` | {row['buy_sol']} | {row['sell_sol']} | {row['clean_realized_pnl_sol']} | {row['clean_wins']}/{row['clean_losses']} | {row['open_cost_sol']} | {market} |"
        )
    lines += ["", "## Copy Blocks", "", "Top candidate mints:", "", "```text"]
    lines += [row["mint"] for row in ledger[:25]]
    lines += ["```", "", "Worst clean lots:", "", "```text"]
    for lot in sorted(clean_lots, key=lambda x: x["realized_pnl_sol"])[:15]:
        lines.append(f"{lot['mint']}  pnl={lot['realized_pnl_sol']:.6f} roi={lot['roi_pct']:.2f}%")
    lines += ["```", "", "## Method", "", summary["method"]]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "md": str(md_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only wallet edge study")
    parser.add_argument("address")
    parser.add_argument("--pages", type=int, default=12, help="100 tx per page")
    parser.add_argument("--enrich", type=int, default=20, help="Dexscreener enrich top N rows")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()
    address = require_address(args.address)
    txs, _ = fetch_transactions(address, max(1, args.pages))
    events = extract_events(address, txs)
    ledger, lots = build_ledger(events)
    paths = write_outputs(address, ledger, lots, txs, events, args.pages, args.enrich)
    clean_lots = [x for x in lots if x.get("clean_window")]
    result = {
        "ok": True,
        "wallet": address,
        "txs_fetched": len(txs),
        "events": len(events),
        "tokens_touched": len(ledger),
        "clean_closed_lots": len(clean_lots),
        "clean_wins": sum(1 for x in clean_lots if x["realized_pnl_sol"] > 0),
        "clean_losses": sum(1 for x in clean_lots if x["realized_pnl_sol"] < 0),
        "clean_realized_pnl_sol": round(sum(x["realized_pnl_sol"] for x in clean_lots), 6),
        "top": ledger[:10],
        "paths": paths,
    }
    if args.raw:
        safe_print(result)
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
