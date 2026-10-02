#!/usr/bin/env python3
"""Build token-event case-study candidates from wallet position samples.

Read-only. Combines selected wallet position data with Dexscreener market/social
context. Does not trade, alert, post, or scrape gated X data.
"""
from __future__ import annotations
import os

import argparse
import csv
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
ALPHA = PROFILE_HOME / "trading" / "alpha"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_final_wallets() -> dict[str, str]:
    paths = [
        ALPHA / "secondary" / "wallet_study_set.json",
        ALPHA / "secondary" / "actor_cluster_deep_pass.json",
    ]
    out: dict[str, str] = {}
    for p in paths:
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        rows = data.get("wallets") or data.get("results") or []
        for r in rows:
            if r.get("wallet") and r.get("label"):
                out[r["wallet"]] = r["label"]
    return out


def fetch_dex(mint: str) -> dict[str, Any] | None:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{urllib.parse.quote(mint)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ChaosReadOnly/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def best_pair(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    pairs = payload.get("pairs") or []
    sol = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
    if not sol:
        return None
    def score(p: dict[str, Any]) -> float:
        liq = ((p.get("liquidity") or {}).get("usd") or 0) or 0
        vol = ((p.get("volume") or {}).get("h24") or 0) or 0
        tx = (((p.get("txns") or {}).get("h24") or {}).get("buys") or 0) + (((p.get("txns") or {}).get("h24") or {}).get("sells") or 0)
        return float(liq) * 0.25 + float(vol) + float(tx) * 10
    return sorted(sol, key=score, reverse=True)[0]


def pair_context(pair: dict[str, Any] | None) -> dict[str, Any]:
    if not pair:
        return {"dex_found": False}
    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    socials = info.get("socials") or []
    websites = info.get("websites") or []
    return {
        "dex_found": True,
        "dex_id": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "url": pair.get("url"),
        "symbol": base.get("symbol"),
        "name": base.get("name"),
        "price_usd": pair.get("priceUsd"),
        "market_cap": pair.get("marketCap") or pair.get("fdv"),
        "fdv": pair.get("fdv"),
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
        "volume_h24": (pair.get("volume") or {}).get("h24"),
        "txns_h24": (pair.get("txns") or {}).get("h24"),
        "price_change": pair.get("priceChange"),
        "pair_created_at": pair.get("pairCreatedAt"),
        "socials": socials,
        "websites": websites,
        "image_url": info.get("imageUrl"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build token event study candidates")
    ap.add_argument("--db", action="append", required=True, help="SQLite DB produced by wallet deep pass")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--out-prefix", default="token_event_cases")
    args = ap.parse_args()

    labels = load_final_wallets()
    token_rows: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "mint": None, "wallets": {}, "sample_pnl_sol": 0.0, "clean_pnl_sol": 0.0,
        "sol_spent": 0.0, "positions": 0, "contaminated_positions": 0,
        "wins": 0, "losses": 0, "first_seen": None, "last_seen": None, "examples": []
    })
    for db_path in args.db:
        con = sqlite3.connect(Path(db_path).expanduser())
        con.row_factory = sqlite3.Row
        for p in con.execute("select wallet,mint,opened_at_utc,closed_at_utc,status,buy_count,sell_count,sol_spent,sol_received,realized_pnl_sol,hold_seconds,transfer_contaminated from positions where mint is not null"):
            d = dict(p)
            w = d["wallet"]
            # Keep selected final/nilla wallets only when labels are known.
            if w not in labels:
                continue
            m = d["mint"]
            t = token_rows[m]
            t["mint"] = m
            pnl = float(d.get("realized_pnl_sol") or 0)
            spent = float(d.get("sol_spent") or 0)
            contaminated = bool(d.get("transfer_contaminated"))
            t["sample_pnl_sol"] += pnl
            if not contaminated:
                t["clean_pnl_sol"] += pnl
            t["sol_spent"] += spent
            t["positions"] += 1
            t["contaminated_positions"] += int(contaminated)
            if pnl > 0:
                t["wins"] += 1
            elif pnl < 0:
                t["losses"] += 1
            if d.get("opened_at_utc"):
                t["first_seen"] = d["opened_at_utc"] if t["first_seen"] is None or d["opened_at_utc"] < t["first_seen"] else t["first_seen"]
                t["last_seen"] = d["opened_at_utc"] if t["last_seen"] is None or d["opened_at_utc"] > t["last_seen"] else t["last_seen"]
            t["wallets"].setdefault(w, {"wallet": w, "label": labels[w], "pnl_sol": 0.0, "positions": 0, "contaminated": 0, "first_seen": None})
            ww = t["wallets"][w]
            ww["pnl_sol"] += pnl; ww["positions"] += 1; ww["contaminated"] += int(contaminated)
            if d.get("opened_at_utc"):
                ww["first_seen"] = d["opened_at_utc"] if ww["first_seen"] is None or d["opened_at_utc"] < ww["first_seen"] else ww["first_seen"]
            if len(t["examples"]) < 8:
                t["examples"].append({"wallet": w, "label": labels[w], **d})
        con.close()

    cases = []
    for m, t in token_rows.items():
        wallets = list(t["wallets"].values())
        for w in wallets:
            w["pnl_sol"] = round(w["pnl_sol"], 6)
        wallet_count = len(wallets)
        contam_ratio = t["contaminated_positions"] / max(1, t["positions"])
        # Token case priority: shared wallets, clean PnL, or large outcome; penalize all-contaminated noise.
        priority = 0.0
        priority += wallet_count * 8
        priority += max(-20, min(40, t["sample_pnl_sol"] * 0.6))
        priority += max(-20, min(35, t["clean_pnl_sol"] * 1.0))
        priority += t["wins"] * 2 - t["losses"] * 1.5
        priority -= contam_ratio * 18
        if wallet_count >= 2:
            priority += 12
        cases.append({
            "mint": m,
            "priority": round(priority, 3),
            "wallet_count": wallet_count,
            "wallets": sorted(wallets, key=lambda x: x.get("first_seen") or "9999"),
            "positions": t["positions"],
            "wins": t["wins"],
            "losses": t["losses"],
            "sample_pnl_sol": round(t["sample_pnl_sol"], 6),
            "clean_pnl_sol": round(t["clean_pnl_sol"], 6),
            "sol_spent": round(t["sol_spent"], 6),
            "contaminated_positions": t["contaminated_positions"],
            "contam_ratio": round(contam_ratio, 3),
            "first_seen": t["first_seen"],
            "last_seen": t["last_seen"],
            "examples": t["examples"],
        })
    cases.sort(key=lambda x: x["priority"], reverse=True)

    for c in cases[: args.top]:
        payload = fetch_dex(c["mint"])
        c["market"] = pair_context(best_pair(payload))
        time.sleep(0.12)

    out_json = ALPHA / f"{args.out_prefix}.json"
    out_csv = ALPHA / f"{args.out_prefix}.csv"
    out_md = ALPHA / f"{args.out_prefix}.md"
    result = {
        "generated_at": now_utc(),
        "dbs": args.db,
        "case_count": len(cases),
        "top_context_count": min(args.top, len(cases)),
        "cases": cases,
        "notes": [
            "Case priority uses wallet overlap + PnL + clean PnL + contamination penalty.",
            "Dexscreener context is current/available market context, not full historical chart at entry time.",
            "X/tweet event reconstruction requires X search/API access; this script records available Dex socials only.",
        ],
    }
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        cols = ["priority", "mint", "symbol", "name", "wallet_count", "wallet_labels", "sample_pnl_sol", "clean_pnl_sol", "positions", "contam_ratio", "liquidity_usd", "market_cap", "volume_h24", "url"]
        wr = csv.DictWriter(fh, fieldnames=cols); wr.writeheader()
        for c in cases:
            m = c.get("market") or {}
            wr.writerow({
                "priority": c["priority"], "mint": c["mint"], "symbol": m.get("symbol"), "name": m.get("name"),
                "wallet_count": c["wallet_count"], "wallet_labels": ";".join(w["label"] for w in c["wallets"]),
                "sample_pnl_sol": c["sample_pnl_sol"], "clean_pnl_sol": c["clean_pnl_sol"], "positions": c["positions"], "contam_ratio": c["contam_ratio"],
                "liquidity_usd": m.get("liquidity_usd"), "market_cap": m.get("market_cap"), "volume_h24": m.get("volume_h24"), "url": m.get("url"),
            })
    lines = [
        "# Token Event Case Candidates",
        "",
        f"Generated: {result['generated_at']}",
        "",
        "## Gate",
        "",
        "These are token-event candidates surfaced by wallet behavior. Market context is Dexscreener-current, not historical entry-time truth. X/tweet ignition still needs a search/API layer.",
        "",
        "## Top Cases",
        "",
        "| Priority | Token | Mint | Wallets | PnL | Clean PnL | Contam | Liquidity | MCap | Link |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for c in cases[: args.top]:
        m = c.get("market") or {}
        token = m.get("symbol") or "unknown"
        link = m.get("url") or ""
        lines.append(f"| {c['priority']} | {token} | `{c['mint']}` | {', '.join(w['label'] for w in c['wallets'])} | {c['sample_pnl_sol']} | {c['clean_pnl_sol']} | {c['contaminated_positions']}/{c['positions']} | {m.get('liquidity_usd')} | {m.get('market_cap')} | {link} |")
    lines += [
        "",
        "## Read Rules",
        "",
        "- Shared wallet touch is useful only if timing shows a leader before attention.",
        "- Clean solo wins teach wallet style; contaminated shared wins teach actor-cluster routing risk.",
        "- Missing Dex pair means token may be dead/unindexed or not currently liquid.",
        "",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ok": True, "json": str(out_json), "csv": str(out_csv), "md": str(out_md), "case_count": len(cases), "top": [{k: c[k] for k in ["priority", "mint", "wallet_count", "sample_pnl_sol", "clean_pnl_sol", "contam_ratio"]} | {"symbol": (c.get("market") or {}).get("symbol"), "wallets": [w["label"] for w in c["wallets"]]} for c in cases[:10]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
