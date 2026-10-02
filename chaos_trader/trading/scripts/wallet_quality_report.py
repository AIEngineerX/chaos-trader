#!/usr/bin/env python3
"""Chaos Wallet Quality v1.

Scores wallets from local, transfer-aware position evidence. This is intentionally
stricter than "wallet touched token": realized exits and transfer hygiene drive
copyability; cluster/transfer-heavy wallets can still be sensors.

Read-only local DB/report generation. No wallet connection, signing, execution,
alerts, webhooks, or network calls.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
REPORT_DIR = PROFILE_HOME / "trading" / "reports"
BOUNDARY = "read-only wallet quality calibration; no execution, alerts, wallet connection, or network calls"


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def qrows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def median(values: list[Any]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(statistics.median(clean), 6) if clean else None


def safe_div(n: float, d: float) -> float | None:
    return round(n / d, 6) if d else None


def wallet_universe(con: sqlite3.Connection) -> list[str]:
    wallets: set[str] = set()
    if table_exists(con, "positions"):
        wallets.update(str(r[0]) for r in con.execute("SELECT DISTINCT wallet FROM positions WHERE wallet IS NOT NULL"))
    if table_exists(con, "wallet_scores"):
        wallets.update(str(r[0]) for r in con.execute("SELECT DISTINCT wallet FROM wallet_scores WHERE wallet IS NOT NULL"))
    if table_exists(con, "actors"):
        wallets.update(str(r[0]) for r in con.execute("SELECT DISTINCT primary_wallet FROM actors WHERE primary_wallet IS NOT NULL"))
    return sorted(wallets)


def wallet_positions(con: sqlite3.Connection, wallet: str) -> list[dict[str, Any]]:
    if not table_exists(con, "positions"):
        return []
    return qrows(con, "SELECT * FROM positions WHERE wallet=?", (wallet,))


def wallet_edges(con: sqlite3.Connection, wallet: str) -> list[dict[str, Any]]:
    if not table_exists(con, "wallet_edges"):
        return []
    return qrows(con, "SELECT * FROM wallet_edges WHERE src_wallet=? OR dst_wallet=?", (wallet, wallet))


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", [], {}):
            return default
        return int(float(value))
    except Exception:
        return default


def position_is_trade(pos: dict[str, Any]) -> bool:
    return as_int(pos.get("buy_count")) > 0 or as_int(pos.get("sell_count")) > 0 or as_float(pos.get("sol_spent")) > 0 or as_float(pos.get("sol_received")) > 0


def compute_metrics(con: sqlite3.Connection, wallet: str) -> dict[str, Any]:
    positions = wallet_positions(con, wallet)
    trade = [p for p in positions if position_is_trade(p)]
    clean = [p for p in trade if not bool(p.get("transfer_contaminated"))]
    closed_clean = [p for p in clean if str(p.get("status") or "").lower() == "closed" or as_int(p.get("sell_count")) > 0]
    wins = [p for p in closed_clean if as_float(p.get("realized_pnl_sol")) > 0]
    losses = [p for p in closed_clean if as_float(p.get("realized_pnl_sol")) < 0]
    contaminated = [p for p in trade if bool(p.get("transfer_contaminated"))]
    edges = wallet_edges(con, wallet)
    transfer_edges = [e for e in edges if "transfer" in str(e.get("edge_type") or e.get("source_id") or "").lower()]
    shared_edges = [e for e in edges if str(e.get("edge_type") or "") == "shared_mint_overlap"]
    spent = sum(as_float(p.get("sol_spent")) for p in clean)
    received = sum(as_float(p.get("sol_received")) for p in clean)
    clean_pnl = sum(as_float(p.get("realized_pnl_sol")) for p in clean)
    raw_pnl = sum(as_float(p.get("realized_pnl_sol")) for p in trade)
    hold = median([p.get("hold_seconds") for p in closed_clean if p.get("hold_seconds") is not None])
    closed_count = len(closed_clean)
    win_rate = safe_div(len(wins), closed_count)
    contamination_rate = safe_div(len(contaminated), len(trade))
    repeat_win_score = min(100.0, round((win_rate or 0) * 60 + min(len(wins), 10) * 4, 3)) if closed_count else 0.0
    exit_ratio = safe_div(sum(as_int(p.get("sell_count")) for p in clean), max(1, sum(as_int(p.get("buy_count")) for p in clean)))
    return {
        "wallet": wallet,
        "positions": len(positions),
        "trade_positions": len(trade),
        "clean_trade_positions": len(clean),
        "closed_clean_positions": closed_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "raw_realized_pnl_sol": round(raw_pnl, 9),
        "transfer_adjusted_pnl_sol": round(clean_pnl, 9),
        "sol_spent_clean": round(spent, 9),
        "sol_received_clean": round(received, 9),
        "roi_sol": safe_div(clean_pnl, spent),
        "transfer_contaminated_positions": len(contaminated),
        "transfer_contamination_rate": contamination_rate,
        "median_hold_seconds": hold,
        "exit_ratio": exit_ratio,
        "transfer_edge_count": len(transfer_edges),
        "shared_overlap_edges": len(shared_edges),
        "max_shared_mints": max([as_int(e.get("shared_mints")) for e in shared_edges], default=0),
        "repeat_win_score": repeat_win_score,
    }


def classify(metrics: dict[str, Any]) -> dict[str, Any]:
    score = 0.0
    positives: list[str] = []
    negatives: list[str] = []
    hard_flags: list[str] = []
    clean_closed = metrics["closed_clean_positions"]
    win_rate = metrics.get("win_rate") or 0.0
    clean_pnl = metrics["transfer_adjusted_pnl_sol"]
    contam = metrics.get("transfer_contamination_rate")

    if clean_closed >= 5:
        score += 18; positives.append("repeat_closed_sample")
    elif clean_closed >= 3:
        score += 10; positives.append("minimum_closed_sample")
    else:
        score -= 18; negatives.append("insufficient_realized_exits")

    if clean_pnl > 5:
        score += min(24, clean_pnl * 2); positives.append("strong_transfer_adjusted_pnl")
    elif clean_pnl > 0:
        score += min(12, clean_pnl * 3); positives.append("positive_transfer_adjusted_pnl")
    elif metrics["trade_positions"] > 0:
        score -= min(20, abs(clean_pnl) * 3 + 8); negatives.append("nonpositive_transfer_adjusted_pnl")

    if win_rate >= 0.65 and clean_closed >= 3:
        score += 18; positives.append("repeat_win_rate")
    elif win_rate >= 0.5 and clean_closed >= 3:
        score += 10; positives.append("acceptable_win_rate")
    elif clean_closed >= 3:
        score -= 10; negatives.append("weak_win_rate")

    if contam is None:
        negatives.append("contamination_unknown")
    elif contam <= 0.25:
        score += 15; positives.append("low_transfer_contamination")
    elif contam >= 0.75:
        score -= 24; negatives.append("high_transfer_contamination"); hard_flags.append("transfer_contaminated")
    elif contam >= 0.5:
        score -= 12; negatives.append("medium_transfer_contamination")

    if metrics.get("exit_ratio") is not None:
        if 0.25 <= metrics["exit_ratio"] <= 3.0:
            score += 8; positives.append("exits_visible")
        elif metrics["exit_ratio"] < 0.1:
            score -= 8; negatives.append("buys_without_exits")

    if metrics["max_shared_mints"] >= 40:
        score += 14; positives.append("strong_cluster_sensor")
    elif metrics["max_shared_mints"] >= 20:
        score += 7; positives.append("moderate_cluster_sensor")

    if metrics["transfer_edge_count"] >= 3 and clean_closed < 3:
        score -= 10; negatives.append("transfer_surface_not_trade_wallet")

    score = round(max(0.0, min(100.0, score)), 3)
    if clean_closed >= 3 and clean_pnl > 0 and win_rate >= 0.5 and (contam is not None and contam <= 0.25) and score >= 60 and not hard_flags:
        verdict = "copyable-candidate"
    elif metrics["max_shared_mints"] >= 40 and (contam is None or contam <= 0.5):
        verdict = "cluster-sensor"
    elif metrics["max_shared_mints"] >= 20 and score >= 38:
        verdict = "cluster-sensor"
    elif clean_closed >= 2 and score >= 35:
        verdict = "study"
    elif score >= 18:
        verdict = "low-priority"
    else:
        verdict = "avoid"

    return {**metrics, "score": score, "verdict": verdict, "positives": positives, "negatives": negatives, "hard_flags": hard_flags}


def run(db: Path, *, limit: int = 100) -> dict[str, Any]:
    if not db.exists():
        return {"ok": False, "error": f"missing db: {db}", "rows": [], "summary": {}, "boundary": BOUNDARY}
    con = connect(db)
    try:
        wallets = wallet_universe(con)
        rows = [classify(compute_metrics(con, wallet)) for wallet in wallets]
    finally:
        con.close()
    rows.sort(key=lambda r: (r["score"], r["transfer_adjusted_pnl_sol"], r["wins"]), reverse=True)
    clipped = rows[: max(1, min(limit, 1000))]
    summary = {
        "wallets_scored": len(rows),
        "copyable_candidates": sum(1 for r in rows if r["verdict"] == "copyable-candidate"),
        "cluster_sensors": sum(1 for r in rows if r["verdict"] == "cluster-sensor"),
        "study": sum(1 for r in rows if r["verdict"] == "study"),
        "avoid": sum(1 for r in rows if r["verdict"] == "avoid"),
    }
    return {"ok": True, "mode": "chaos_wallet_quality_v1", "db": str(db), "summary": summary, "rows": clipped, "boundary": BOUNDARY}


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Chaos Wallet Quality v1",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Boundary: {BOUNDARY}.",
        "",
        "## Summary",
        "",
    ]
    for k, v in (payload.get("summary") or {}).items():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "## Ranked wallets",
        "",
        "| Verdict | Score | Wallet | Clean closed | Win rate | Transfer-adjusted PnL | Contam | Why |",
        "|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for row in payload.get("rows") or []:
        why = ", ".join((row.get("positives") or row.get("negatives") or [])[:3])
        wallet = str(row.get("wallet") or "")
        lines.append(f"| {row['verdict']} | {row['score']} | `{wallet[:6]}…{wallet[-4:]}` | {row['closed_clean_positions']} | {row.get('win_rate')} | {row['transfer_adjusted_pnl_sol']} | {row.get('transfer_contamination_rate')} | {why} |")
    if not payload.get("rows"):
        lines.append("| no wallets | 0 | - | 0 | - | 0 | - | no local position evidence |")
    lines += [
        "",
        "## Discipline",
        "",
        "- Copyability requires realized exits, positive transfer-adjusted PnL, repeat wins, and low contamination.",
        "- Transfer-heavy wallets can be sensors, not copy wallets.",
        "- Historical/secondary priors do not override local transfer-aware evidence.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Score Chaos wallets from local transfer-aware position evidence")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--write", action="store_true")
    p.add_argument("--raw", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    payload = run(Path(args.db).expanduser(), limit=args.limit)
    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return 0 if payload.get("ok") else 2
    md = render_md(payload)
    if args.write and payload.get("ok"):
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"wallet_quality_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", encoding="utf-8")
        print(f"☄️ Wallet quality report written: {path}")
    print(md)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
