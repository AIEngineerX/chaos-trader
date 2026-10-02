#!/usr/bin/env python3
"""Promote/demote smart-wallet candidates using multi-source evidence.

Secondary evidence is historical prior only. Promotion requires live/Helius-derived evidence,
position hygiene, transfer contamination checks, exits, and cluster utility.
Read-only local DB scoring; no execution, alerts, posting, cron, or signing.
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from statistics import median
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from smart_wallet_tracker import ONCHAIN_SOURCE_IDS, ONCHAIN_SOURCE_SQL, QUOTE_MINTS, discovered_wallets, ensure_db  # noqa: E402

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
OUT_JSON = PROFILE_HOME / "trading" / "alpha" / "smart_wallet_promotions.json"
OUT_MD = PROFILE_HOME / "trading" / "alpha" / "smart_wallet_promotions.md"


def qrows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def q1(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def jload(s: Any, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def latest_score(con: sqlite3.Connection, wallet: str, *sources: str) -> dict[str, Any] | None:
    marks = ",".join("?" for _ in sources)
    return q1(con, f"SELECT * FROM wallet_scores WHERE wallet=? AND source_id IN ({marks}) ORDER BY scored_at DESC, id DESC LIMIT 1", (wallet, *sources))


def secondary_aggregate(con: sqlite3.Connection, wallet: str) -> dict[str, Any] | None:
    """Collapse multiple secondary-export rows without letting actor-map rows hide import stats."""
    rows = qrows(con, "SELECT * FROM wallet_scores WHERE wallet=? AND source_id='secondary_export'", (wallet,))
    if not rows:
        return None
    out: dict[str, Any] = {"wallet": wallet, "source_id": "secondary_export"}
    for key in ["pnl_all", "win_rate", "buy_count", "sell_count", "airdrop_count", "sell_buy_ratio", "distinct_symbols", "trade_usd_sum", "actor_score", "score"]:
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        out[key] = max(vals) if vals else None
    # Preserve most useful label from actor rows.
    labels = [r.get("classification") or r.get("copyability") or r.get("tier") for r in rows if r.get("classification") or r.get("copyability") or r.get("tier")]
    out["classification"] = labels[0] if labels else None
    out["reasons_json"] = json.dumps([jload(r.get("reasons_json"), r.get("reasons_json")) for r in rows if r.get("reasons_json")][:3], default=str)
    return out


def wallet_metrics(con: sqlite3.Connection, wallet: str) -> dict[str, Any]:
    w = q1(con, "SELECT * FROM wallets WHERE address=?", (wallet,)) or {"address": wallet}
    actor = q1(con, "SELECT * FROM actors WHERE primary_wallet=?", (wallet,))
    secondary = secondary_aggregate(con, wallet)
    helius = latest_score(con, wallet, *ONCHAIN_SOURCE_IDS)
    # Scores produced before matched-cost-basis accounting are historical noise,
    # not live qualification evidence. Position metrics below remain usable.
    if helius and helius.get("classification") != "helius_matched_cost_basis_v2_net_wallet_delta":
        helius = None
    pos = qrows(con, "SELECT * FROM positions WHERE wallet=?", (wallet,))
    trade_pos = [p for p in pos if p.get("mint") not in QUOTE_MINTS and ((p.get("buy_count") or 0) > 0 or (p.get("sell_count") or 0) > 0)]
    clean_trade = [p for p in trade_pos if not p.get("transfer_contaminated")]
    closed = [p for p in clean_trade if p.get("status") == "closed" and p.get("realized_pnl_sol") is not None]
    wins = [p for p in closed if p["realized_pnl_sol"] > 0]
    losses = [p for p in closed if p["realized_pnl_sol"] < 0]
    pnl = sum(p["realized_pnl_sol"] for p in closed)
    closed_holds = [float(p["hold_seconds"]) for p in closed if p.get("hold_seconds") is not None]
    med_hold = median(closed_holds) if closed_holds else None
    edges = qrows(con, "SELECT * FROM wallet_edges WHERE (src_wallet=? OR dst_wallet=?) AND edge_type='shared_mint_overlap'", (wallet, wallet))
    transfer_edges = qrows(con, f"SELECT * FROM wallet_edges WHERE (src_wallet=? OR dst_wallet=?) AND source_id IN ({ONCHAIN_SOURCE_SQL})", (wallet, wallet))
    return {
        "wallet": wallet,
        "handle": w.get("handle") or w.get("display_name") or (actor or {}).get("handle"),
        "actor_label": (actor or {}).get("classification"),
        "actor_confidence": (actor or {}).get("confidence"),
        "secondary": secondary,
        "helius": helius,
        "positions": len(pos),
        "trade_positions": len(trade_pos),
        "clean_trade_positions": len(clean_trade),
        "closed_positions": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "sample_realized_pnl_sol": round(pnl, 9),
        "median_hold_seconds": med_hold,
        "contamination_ratio": round(1 - (len(clean_trade) / len(trade_pos)), 3) if trade_pos else None,
        "shared_overlap_edges": len(edges),
        "max_shared_mints": max([e.get("shared_mints") or 0 for e in edges], default=0),
        "helius_transfer_edges": len(transfer_edges),
    }


def classify(m: dict[str, Any]) -> dict[str, Any]:
    score = 0.0
    positives: list[str] = []
    negatives: list[str] = []
    hard_flags: list[str] = []

    o = m.get("secondary") or {}
    h = m.get("helius") or {}

    # Historical prior: useful, never sufficient.
    if m.get("actor_label") in {"copyable-candidate", "deep-watch", "high-churn-scout"}:
        score += 12; positives.append("secondary_actor_prior")
    if (o.get("pnl_all") or 0) and (o.get("pnl_all") or 0) > 100_000:
        score += 8; positives.append("large_historical_secondary_pnl")
    if (o.get("buy_count") or 0) >= 50 and (o.get("sell_count") or 0) >= 20:
        score += 8; positives.append("historical_exits_visible")
    elif (o.get("buy_count") or 0) >= 50 and (o.get("sell_count") or 0) < 5:
        score -= 10; negatives.append("historical_buys_without_exits")

    # Live/Helius sample is stronger evidence.
    if h:
        if (h.get("buy_count") or 0) + (h.get("sell_count") or 0) >= 10:
            score += 10; positives.append("live_trade_activity")
        elif (h.get("buy_count") or 0) + (h.get("sell_count") or 0) == 0:
            score -= 12; negatives.append("no_live_trade_activity")
        if (h.get("realized_pnl_sol") or 0) > 2:
            score += min(18, (h.get("realized_pnl_sol") or 0) * 2); positives.append("positive_live_sample_pnl")
        elif (h.get("realized_pnl_sol") or 0) < -1:
            score -= min(18, abs(h.get("realized_pnl_sol") or 0) * 2); negatives.append("negative_live_sample_pnl")
        cont = h.get("transfer_contamination_score")
        if cont is not None:
            if cont <= 0.25:
                score += 15; positives.append("low_transfer_contamination")
            elif cont >= 0.75:
                score -= 20; negatives.append("high_transfer_contamination")
                hard_flags.append("transfer_contaminated")
        if h.get("copyability") in {"candidate", "watch"}:
            score += 12; positives.append("helius_copyability_positive")
        elif h.get("copyability") == "avoid":
            score -= 14; negatives.append("helius_copyability_avoid")
    else:
        score -= 15; negatives.append("no_helius_confirmation")

    # Position structure.
    if m["clean_trade_positions"] >= 3 and m["closed_positions"] >= 2:
        score += 18; positives.append("clean_closed_positions")
    elif m["trade_positions"] >= 3 and m["clean_trade_positions"] == 0:
        score -= 12; negatives.append("no_clean_trade_positions")
    if m["wins"] >= 3:
        score += 8; positives.append("multiple_live_wins")
    if m["losses"] >= m["wins"] * 2 and m["losses"] >= 3:
        score -= 8; negatives.append("loss_heavy_live_sample")

    # Cluster value can make a wallet useful as a sensor even if not copyable.
    strong_sensor = False
    if m["max_shared_mints"] >= 40:
        score += 18; positives.append("strong_cluster_sensor"); strong_sensor = True
    elif m["max_shared_mints"] >= 20:
        score += 8; positives.append("moderate_cluster_sensor")

    # A transfer-heavy wallet can still be useful as a discovery rail; it just cannot be copied blindly.
    if strong_sensor and m.get("actor_label") in {"copyable-candidate", "deep-watch", "high-churn-scout"}:
        score = max(score, 48)
        positives.append("usable_as_sensor_not_copy_wallet")

    # Demote routing wallets / pure transfer surfaces for copyability, but don't erase sensor value.
    if m["trade_positions"] == 0 and (m["helius_transfer_edges"] or 0) > 0:
        score -= 10; negatives.append("transfer_surface_not_trade_wallet")

    score = round(max(0.0, min(100.0, score)), 3)
    if score >= 70 and not hard_flags and m["closed_positions"] >= 3:
        verdict = "smart-wallet-candidate"
    elif strong_sensor and score >= 42:
        verdict = "cluster-sensor"
    elif score >= 52:
        verdict = "cluster-sensor"
    elif score >= 35:
        verdict = "study"
    elif score >= 18:
        verdict = "low-priority"
    else:
        verdict = "avoid"
    if "transfer_contaminated" in hard_flags and verdict == "smart-wallet-candidate":
        verdict = "cluster-sensor"

    return {**m, "score": score, "verdict": verdict, "positives": positives, "negatives": negatives, "hard_flags": hard_flags}


def run(db: Path, limit: int, write: bool) -> dict[str, Any]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    ensure_db(con)
    wallets = [r["wallet"] for r in qrows(con, """
        SELECT DISTINCT wallet FROM wallet_scores WHERE wallet IS NOT NULL
        UNION
        SELECT DISTINCT primary_wallet FROM actors WHERE primary_wallet IS NOT NULL
        ORDER BY 1
    """)]
    rows = [classify(wallet_metrics(con, w)) for w in wallets]
    rows.sort(key=lambda r: (r["score"], r["max_shared_mints"], r["sample_realized_pnl_sol"]), reverse=True)
    discovery_queue = discovered_wallets(con, 25)
    payload = {
        "ok": True,
        "mode": "multi_source_smart_wallet_promoter",
        "count": len(rows),
        "summary": {
            "smart_wallet_candidates": sum(1 for r in rows if r["verdict"] == "smart-wallet-candidate"),
            "cluster_sensors": sum(1 for r in rows if r["verdict"] == "cluster-sensor"),
            "study": sum(1 for r in rows if r["verdict"] == "study"),
            "avoid": sum(1 for r in rows if r["verdict"] == "avoid"),
            "discovery_queue": len(discovery_queue),
        },
        "rows": rows[:limit],
        "discovery_queue": discovery_queue,
        "discovery_queue_note": "never-scored wallets found via funding/transfer edges; enrich with smart_wallet_tracker.py --discovered N",
        "boundary": "research only; secondary evidence is historical prior, not source of truth",
    }
    con.close()
    if write:
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        lines = ["# Smart Wallet Promotions", "", "Secondary evidence is a historical prior only. Helius/live transfer-aware evidence can veto.", "", "## Summary", ""]
        for k, v in payload["summary"].items():
            lines.append(f"- {k}: {v}")
        lines += ["", "## Ranked wallets", "", "| Verdict | Score | Handle | Wallet | Helius PnL | Contam | Cluster | Why |", "|---|---:|---|---|---:|---:|---:|---|"]
        for r in payload["rows"]:
            lines.append(f"| {r['verdict']} | {r['score']} | {r.get('handle') or ''} | `{r['wallet'][:6]}…{r['wallet'][-4:]}` | {r.get('sample_realized_pnl_sol')} | {r.get('contamination_ratio')} | {r.get('max_shared_mints')} | {', '.join(r['positives'][:3]) or ', '.join(r['negatives'][:3])} |")
        OUT_MD.write_text("\n".join(lines) + "\n")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Promote/demote smart wallet candidates using multi-source evidence")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    payload = run(Path(args.db).expanduser(), args.limit, args.write)
    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        for r in payload["rows"][:20]:
            print(f"{r['verdict']:22} {r['score']:6.1f} {r.get('handle') or '':18} {r['wallet']} cluster={r['max_shared_mints']} contam={r.get('contamination_ratio')} pnl={r['sample_realized_pnl_sol']}")


if __name__ == "__main__":
    main()
