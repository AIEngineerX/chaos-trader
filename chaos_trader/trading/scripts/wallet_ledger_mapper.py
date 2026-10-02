#!/usr/bin/env python3
"""Map Chaos wallet ledgers into strong-wallet and insider-candidate cohorts.

Read-only. Uses local SQLite ledgers. Does not call wallets, dApps, or execution surfaces.

Outputs:
- JSON with scored wallets/clusters
- CSV with top wallet candidates
- Markdown operator report
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
OUT_DIR = PROFILE_HOME / "trading" / "reports" / "wallet_maps"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jloads(s: Any) -> dict[str, Any]:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def load_wallets(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(r) for r in con.execute("SELECT * FROM wallet_scores")]
    by_wallet = {r["wallet"]: r for r in rows}
    # Include every wallet in the ledger, even if no wallet_score row exists yet.
    try:
        for r in con.execute("SELECT address AS wallet, label, handle, category, sol_balance, total_usd_value, first_seen_utc, last_seen_utc FROM wallets"):
            d = dict(r)
            by_wallet.setdefault(d["wallet"], d)
    except sqlite3.OperationalError:
        pass
    rows = list(by_wallet.values())
    pos = {r["wallet"]: dict(r) for r in con.execute(
        """
        SELECT wallet,
               COUNT(*) AS position_count,
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_positions,
               COUNT(DISTINCT mint) AS distinct_mints,
               SUM(COALESCE(sol_spent,0)) AS sol_spent,
               SUM(COALESCE(sol_received,0)) AS sol_received
        FROM positions GROUP BY wallet
        """
    )}
    ev = {r["wallet"]: dict(r) for r in con.execute(
        """
        SELECT wallet,
               SUM(CASE WHEN event_type='swap_buy' OR side='buy' THEN 1 ELSE 0 END) AS buys,
               SUM(CASE WHEN event_type='swap_sell' OR side='sell' THEN 1 ELSE 0 END) AS sells,
               SUM(CASE WHEN event_type LIKE '%transfer%' THEN 1 ELSE 0 END) AS transfers,
               COUNT(DISTINCT mint) AS event_mints
        FROM wallet_token_events GROUP BY wallet
        """
    )}
    out = []
    for r in rows:
        w = r["wallet"]
        rr = dict(r)
        rr.update({f"positions_{k}": v for k, v in pos.get(w, {}).items() if k != "wallet"})
        rr.update({f"events_{k}": v for k, v in ev.get(w, {}).items() if k != "wallet"})
        rr["reasons"] = jloads(rr.get("reasons_json"))
        out.append(rr)
    return out


def score_wallet(w: dict[str, Any]) -> dict[str, Any]:
    pnl = safe_float(w.get("realized_pnl_sol"))
    win = safe_float(w.get("win_rate"))
    if win > 1.0:
        win = win / 100.0
    buys = safe_float(w.get("buy_count") or w.get("events_buys"))
    sells = safe_float(w.get("sell_count") or w.get("events_sells"))
    transfer_contam = safe_float(w.get("transfer_contamination_score"), 1.0)
    reasons = w.get("reasons") or {}
    if not isinstance(reasons, dict):
        reasons = {}
    positions = safe_float(w.get("positions_position_count"), safe_float(reasons.get("positions")))
    closed = safe_float(w.get("positions_closed_positions"), safe_float(reasons.get("closed")))
    mints = safe_float(w.get("positions_distinct_mints"), safe_float(w.get("distinct_symbols")))

    # Strength rewards realized PnL, repeatability, closed-trade evidence, and win rate.
    pnl_score = clamp(math.log10(max(pnl, 0) + 1) * 18, 0, 45)
    win_score = clamp((win - 0.35) * 70, 0, 30)
    repeat_score = clamp(math.log10(max(positions, mints, 0) + 1) * 12, 0, 18)
    closure_score = clamp(closed / max(positions, 1) * 10, 0, 10)
    activity_penalty = 0
    if buys < 3:
        activity_penalty += 8
    if sells == 0 and buys > 5:
        activity_penalty += 8
    contam_penalty = clamp(transfer_contam * 12, 0, 12)
    strength = clamp(pnl_score + win_score + repeat_score + closure_score - activity_penalty - contam_penalty, 0, 100)

    # Insider suspicion is not copy-quality. It flags privileged/connected structure.
    reasons = w.get("reasons") or {}
    if not isinstance(reasons, dict):
        reasons = {}
    events = reasons.get("events") or {}
    if not isinstance(events, dict):
        events = {}
    token_in = safe_float(events.get("token_transfer_in"))
    sol_in = safe_float(events.get("sol_transfer_in"))
    failed = safe_float(events.get("failed"))
    insider = 0.0
    insider += clamp(token_in / 20 * 20, 0, 20)
    insider += clamp(sol_in / 5 * 10, 0, 10)
    insider += clamp(transfer_contam * 20, 0, 20)
    insider += 10 if pnl > 20 and buys < 10 else 0
    insider += 8 if failed > 5 else 0
    insider = clamp(insider, 0, 100)

    if strength >= 55 and transfer_contam < 0.55 and positions >= 8:
        label = "strong-study"
    elif insider >= 40:
        label = "insider-or-distributor-investigate"
    elif strength >= 35:
        label = "study"
    else:
        label = "ignore-or-low-confidence"

    return {
        "wallet": w.get("wallet"),
        "strength_score": round(strength, 3),
        "insider_score": round(insider, 3),
        "label": label,
        "pnl_sol": pnl,
        "win_rate": win,
        "buys": int(buys),
        "sells": int(sells),
        "positions": int(positions),
        "closed_positions": int(closed),
        "distinct_mints": int(mints),
        "transfer_contamination": transfer_contam,
        "source_score": safe_float(w.get("score")),
        "classification": w.get("classification"),
        "confidence": w.get("confidence"),
        "copyability": w.get("copyability"),
        "reasons": w.get("reasons"),
    }


def load_edges(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT * FROM wallet_edges")]


def cluster_wallets(scored: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored_by = {w["wallet"]: w for w in scored}
    graph: dict[str, set[str]] = defaultdict(set)
    edge_counts: Counter[tuple[str, str]] = Counter()
    # Cluster only stronger linkage. Broad token-transfer edges create giant noisy components.
    strong_types = {"funded_by", "shared_mint_overlap"}
    for e in edges:
        a, b = e.get("src_wallet"), e.get("dst_wallet")
        et = e.get("edge_type")
        if not a or not b or a == b or et not in strong_types:
            continue
        if et == "shared_mint_overlap" and safe_float(e.get("shared_mints")) < 3:
            continue
        if a not in scored_by and b not in scored_by:
            continue
        graph[a].add(b); graph[b].add(a)
        edge_counts[(a,b)] += 1
    seen = set(); clusters = []
    for node in graph:
        if node in seen:
            continue
        q = deque([node]); seen.add(node); comp=[]
        while q:
            n=q.popleft(); comp.append(n)
            for nb in graph[n]:
                if nb not in seen:
                    seen.add(nb); q.append(nb)
        if len(comp) < 2:
            continue
        members = [scored_by.get(w, {"wallet": w, "strength_score":0, "insider_score":0}) for w in comp]
        clusters.append({
            "cluster_id": f"cluster_{len(clusters)+1:04d}",
            "wallet_count": len(comp),
            "avg_strength": round(sum(safe_float(m.get("strength_score")) for m in members)/len(members), 3),
            "max_strength": max(safe_float(m.get("strength_score")) for m in members),
            "avg_insider": round(sum(safe_float(m.get("insider_score")) for m in members)/len(members), 3),
            "top_wallets": sorted(members, key=lambda x: (safe_float(x.get("strength_score")), safe_float(x.get("insider_score"))), reverse=True)[:8],
        })
    return sorted(clusters, key=lambda c: (c["max_strength"], c["wallet_count"]), reverse=True)


def cohort_token_top(con: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    # Wallets appearing in early/top cohorts across multiple tokens.
    q = """
    SELECT wallet,
           COUNT(DISTINCT mint) AS token_count,
           SUM(COALESCE(realized_pnl_sol,0)) AS cohort_pnl_sol,
           AVG(COALESCE(realized_pnl_sol,0)) AS avg_pnl_sol,
           SUM(CASE WHEN COALESCE(realized_pnl_sol,0)>0 THEN 1 ELSE 0 END) AS wins,
           MIN(rank) AS best_rank,
           AVG(rank) AS avg_rank
    FROM token_cohorts
    GROUP BY wallet
    HAVING token_count >= 2
    ORDER BY cohort_pnl_sol DESC, token_count DESC
    LIMIT ?
    """
    try:
        return [dict(r) for r in con.execute(q, (limit,))]
    except sqlite3.OperationalError:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    db = Path(args.db)
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    wallets_raw = load_wallets(con)
    scored = [score_wallet(w) for w in wallets_raw]
    scored.sort(key=lambda w: (w["strength_score"], -w["insider_score"], w["pnl_sol"]), reverse=True)
    insiders = sorted(scored, key=lambda w: (w["insider_score"], w["pnl_sol"]), reverse=True)
    edges = load_edges(con)
    clusters = cluster_wallets(scored, edges)
    cohorts = cohort_token_top(con, args.top)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = out_dir / f"wallet_ledger_map_{stamp}"
    payload = {
        "generated_at_utc": now_utc(),
        "source_db": str(db),
        "wallet_count": len(wallets_raw),
        "edge_count": len(edges),
        "top_strength": scored[:args.top],
        "top_insider_candidates": insiders[:args.top],
        "top_clusters": clusters[:25],
        "top_cohort_wallets": cohorts,
        "method": {
            "strength": "realized PnL + win rate + repeat positions + closure evidence - transfer contamination",
            "insider": "token/SOL transfers + contamination + concentrated large PnL/low buys + failed txs; investigative only",
            "labels": "study labels only, not smart-money proof or execution advice",
        }
    }
    (base.with_suffix(".json")).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["wallet","strength_score","insider_score","label","pnl_sol","win_rate","buys","sells","positions","closed_positions","distinct_mints","transfer_contamination","confidence","copyability"]
        wr = csv.DictWriter(f, fieldnames=fields); wr.writeheader()
        for w in scored[:args.top]: wr.writerow({k: w.get(k) for k in fields})
    md = []
    md.append(f"# Wallet Ledger Map\n\nGenerated: {payload['generated_at_utc']}\nSource DB: `{db}`\n")
    md.append(f"Wallets scored: **{len(wallets_raw)}**  \nEdges loaded: **{len(edges)}**\n")
    md.append("## Top strong-wallet candidates\n")
    md.append("| Wallet | Strength | Insider | PnL SOL | Win | Positions | Contam | Label |\n|---|---:|---:|---:|---:|---:|---:|---|\n")
    for w in scored[:15]:
        md.append(f"| `{w['wallet'][:6]}…{w['wallet'][-4:]}` | {w['strength_score']} | {w['insider_score']} | {w['pnl_sol']:.3f} | {w['win_rate']:.2f} | {w['positions']} | {w['transfer_contamination']:.2f} | {w['label']} |\n")
    md.append("\n## Insider / connected-wallet candidates\n")
    md.append("| Wallet | Insider | Strength | PnL SOL | Buys/Sells | Contam | Reason label |\n|---|---:|---:|---:|---:|---:|---|\n")
    for w in insiders[:15]:
        md.append(f"| `{w['wallet'][:6]}…{w['wallet'][-4:]}` | {w['insider_score']} | {w['strength_score']} | {w['pnl_sol']:.3f} | {w['buys']}/{w['sells']} | {w['transfer_contamination']:.2f} | {w['label']} |\n")
    md.append("\n## Top clusters\n")
    md.append("| Cluster | Wallets | Max strength | Avg insider | Top wallets |\n|---|---:|---:|---:|---|\n")
    for c in clusters[:10]:
        tops = ", ".join(f"`{m['wallet'][:5]}…{m['wallet'][-4:]}`" for m in c["top_wallets"][:4])
        md.append(f"| {c['cluster_id']} | {c['wallet_count']} | {c['max_strength']} | {c['avg_insider']} | {tops} |\n")
    md.append("\n## Helius MCP mapping for next expansion\n")
    md.append("- `heliusWallet.getWalletHistory/getWalletTransfers/getWalletFundedBy/getWalletIdentity`: wallet behavior, funding, exchange/identity hints.\n")
    md.append("- `heliusTransaction.getTransactionHistory/parseTransactions/getTransfersByAddress`: buy/sell vs transfer semantics, early-buyer timing, program decode.\n")
    md.append("- `heliusAsset.getTokenHolders/getAssetsByOwner`: current holders and wallet inventory.\n")
    md.append("- `heliusChain.getTokenAccounts`: raw token accounts by mint/owner for holder extraction fallback.\n")
    md.append("\n## Caveat\nScores are investigative. Connected/insider does not mean copyable. Require Helius funding/history confirmation before promotion.\n")
    base.with_suffix(".md").write_text("".join(md), encoding="utf-8")
    print(json.dumps({"ok": True, "wallets": len(wallets_raw), "edges": len(edges), "json": str(base.with_suffix('.json')), "csv": str(base.with_suffix('.csv')), "md": str(base.with_suffix('.md')), "top_strength": scored[:5], "top_insider": insiders[:5]}, indent=2))
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
