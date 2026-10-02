#!/usr/bin/env python3
"""Chaos read-only alpha paper-trade simulator.

This converts historical local alpha-tape signals into virtual paper entries so
Chaos can measure policy quality before any human interrupt is trusted. It never
places, prepares, signs, routes, or recommends an order.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DB_PATH = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
REPORT_DIR = PROFILE_HOME / "trading" / "reports"
BOUNDARY = "paper-trade research simulation; no wallet, signing, routing, live execution, alerts, or webhooks"


def connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    quoted = urllib.parse.quote(str(path.resolve()), safe="/:")
    con = None
    try:
        con = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1")
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
    # A mode=ro open cannot create the missing -shm/-wal sidecars of a WAL
    # database; fall back to a normal open locked to reads via query_only.
    con = None
    try:
        con = sqlite3.connect(path, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("SELECT 1")
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
        return None


def as_float(value: Any, default: float | None = None) -> float | None:
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


def pct(now_value: float | None, start_value: float | None) -> float | None:
    if now_value is None or start_value in (None, 0):
        return None
    return round(((now_value / start_value) - 1.0) * 100.0, 6)


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 6) if values else None


def table_names(con: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_signals(con: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    required = {"token_signals", "tokens"}
    missing = sorted(required - table_names(con))
    if missing:
        raise sqlite3.OperationalError("missing tables: " + ", ".join(missing))
    rows = con.execute(
        """
        SELECT * FROM (
            SELECT s.mint, s.signal_type, s.wallet_count, s.tg_channel_count,
                   s.total_sol_amount, s.call_market_cap_usd, s.current_market_cap_usd,
                   s.ath_market_cap_usd, s.ath_multiplier, s.is_hit,
                   s.first_buy_utc, s.created_at_utc, s.captured_at_utc,
                   t.symbol, t.name
            FROM token_signals s
            LEFT JOIN tokens t ON t.mint=s.mint
            ORDER BY COALESCE(s.created_at_utc, s.first_buy_utc, s.captured_at_utc) DESC
            LIMIT ?
        )
        ORDER BY COALESCE(created_at_utc, first_buy_utc, captured_at_utc) ASC
        """,
        (max(1, min(limit, 5000)),),
    ).fetchall()
    return [dict(r) for r in rows]


def qualify(row: dict[str, Any], *, min_wallets: int, min_tg: int, min_call_mc: float, max_call_mc: float, require_hit: bool) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    wallet_count = as_int(row.get("wallet_count"), 0)
    tg_count = as_int(row.get("tg_channel_count"), 0)
    call_mc = as_float(row.get("call_market_cap_usd"))
    if wallet_count >= min_wallets:
        reasons.append(f"wallet_cluster>={min_wallets}")
    if tg_count >= min_tg:
        reasons.append(f"tg_confirm>={min_tg}")
    mc_ok = call_mc is not None and min_call_mc <= call_mc <= max_call_mc
    if mc_ok:
        reasons.append("call_mc_in_band")
    hit_ok = bool(as_int(row.get("is_hit"), 0)) or not require_hit
    return wallet_count >= min_wallets and tg_count >= min_tg and mc_ok and hit_ok, reasons


def simulate(signals: list[dict[str, Any]], *, min_wallets: int = 3, min_tg: int = 1, min_call_mc: float = 25_000, max_call_mc: float = 750_000, require_hit: bool = False, take_profit_x: float = 2.0, stop_loss_pct: float = -60.0) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    for row in signals:
        ok, reasons = qualify(row, min_wallets=min_wallets, min_tg=min_tg, min_call_mc=min_call_mc, max_call_mc=max_call_mc, require_hit=require_hit)
        if not ok:
            skipped += 1
            continue
        call_mc = as_float(row.get("call_market_cap_usd"))
        current_mc = as_float(row.get("current_market_cap_usd"))
        ath_x = as_float(row.get("ath_multiplier"))
        current_return = pct(current_mc, call_mc)
        max_return = round((ath_x - 1.0) * 100.0, 6) if ath_x is not None else None
        if max_return is not None and max_return >= (take_profit_x - 1.0) * 100.0:
            paper_result = "take_profit_hit"
            result_r = 1.0
        elif current_return is not None and current_return <= stop_loss_pct:
            paper_result = "stop_loss_hit_or_failed"
            result_r = -1.0
        elif current_return is not None and current_return > 0:
            paper_result = "open_positive"
            result_r = 0.25
        else:
            paper_result = "unresolved_or_flat"
            result_r = 0.0
        rows.append({
            "mint": row.get("mint"),
            "symbol": row.get("symbol"),
            "signal_type": row.get("signal_type"),
            "created_at_utc": row.get("created_at_utc") or row.get("first_buy_utc") or row.get("captured_at_utc"),
            "wallet_count": as_int(row.get("wallet_count"), 0),
            "tg_channel_count": as_int(row.get("tg_channel_count"), 0),
            "call_market_cap_usd": call_mc,
            "current_market_cap_usd": current_mc,
            "ath_multiplier": ath_x,
            "current_return_pct": current_return,
            "max_return_pct": max_return,
            "paper_result": paper_result,
            "result_r": result_r,
            "reasons": reasons,
        })
    result_values = [float(r["result_r"]) for r in rows]
    winners = sum(1 for r in rows if r["paper_result"] == "take_profit_hit")
    losers = sum(1 for r in rows if r["paper_result"] == "stop_loss_hit_or_failed")
    current_positive = sum(1 for r in rows if (r.get("current_return_pct") or 0) > 0)
    max_returns = [float(r["max_return_pct"]) for r in rows if r.get("max_return_pct") is not None]
    return {
        "ok": True,
        "mode": "chaos_alpha_paper_trade_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {
            "min_wallets": min_wallets,
            "min_tg": min_tg,
            "min_call_mc": min_call_mc,
            "max_call_mc": max_call_mc,
            "require_hit": require_hit,
            "take_profit_x": take_profit_x,
            "stop_loss_pct": stop_loss_pct,
        },
        "signals_seen": len(signals),
        "signals_skipped": skipped,
        "paper_trades": len(rows),
        "summary": {
            "take_profit_hits": winners,
            "stop_loss_hits": losers,
            "current_positive": current_positive,
            "win_rate_pct": round((winners / len(rows)) * 100.0, 2) if rows else None,
            "current_positive_rate_pct": round((current_positive / len(rows)) * 100.0, 2) if rows else None,
            "expectancy_r": round(sum(result_values) / len(result_values), 6) if result_values else None,
            "median_max_return_pct": median(max_returns),
        },
        "rows": rows,
        "caveat": "Historical local tape simulation. Max-return fields are not causal entry proof; use only for policy screening until live chronological ingestion is running.",
        "boundary": BOUNDARY,
    }


def run(db: Path = DB_PATH, **kwargs: Any) -> dict[str, Any]:
    kwargs["min_wallets"] = max(1, int(kwargs.get("min_wallets", 3)))
    kwargs["min_tg"] = max(0, int(kwargs.get("min_tg", 1)))
    kwargs["take_profit_x"] = max(1.01, float(kwargs.get("take_profit_x", 2.0)))
    kwargs["stop_loss_pct"] = min(0.0, float(kwargs.get("stop_loss_pct", -60.0)))
    con = connect_ro(db)
    if con is None:
        return {"ok": False, "error": f"missing or unreadable db: {db}", "rows": [], "summary": {}, "boundary": BOUNDARY}
    try:
        signals = load_signals(con, limit=int(kwargs.pop("limit", 500)))
    except sqlite3.Error as exc:
        con.close()
        return {"ok": False, "error": str(exc), "rows": [], "summary": {}, "boundary": BOUNDARY}
    finally:
        try:
            con.close()
        except Exception:
            pass
    return simulate(signals, **kwargs)


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Chaos Alpha Paper Trade Simulation",
        "",
        f"Generated: {payload.get('generated_at_utc')}",
        f"Boundary: {BOUNDARY}.",
        f"Caveat: {payload.get('caveat')}",
        "",
        "## Summary",
        "",
    ]
    if not payload.get("ok"):
        lines.append(f"- error: {payload.get('error')}")
        return "\n".join(lines)
    lines.extend([
        f"- signals_seen: {payload.get('signals_seen')}",
        f"- paper_trades: {payload.get('paper_trades')}",
        f"- signals_skipped: {payload.get('signals_skipped')}",
    ])
    for k, v in (payload.get("summary") or {}).items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Paper entries", "", "| Result | Symbol | CA | Wallets | TG | Call MC | ATH x | Current return |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in (payload.get("rows") or [])[:50]:
        mint = str(row.get("mint") or "")
        lines.append(f"| {row.get('paper_result')} | {row.get('symbol') or 'UNKNOWN'} | `{mint[:6]}…{mint[-4:]}` | {row.get('wallet_count')} | {row.get('tg_channel_count')} | {row.get('call_market_cap_usd')} | {row.get('ath_multiplier')} | {row.get('current_return_pct')}% |")
    if not payload.get("rows"):
        lines.append("| none | - | - | - | - | - | - | - |")
    lines += ["", "## Discipline", "", "- This is paper-only policy evaluation.", "- No alert/paper policy graduates until live freshness and out-of-sample outcomes are proven.", ""]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Simulate read-only Chaos alpha paper trades from local token_signals")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--min-wallets", type=int, default=3)
    p.add_argument("--min-tg", type=int, default=1)
    p.add_argument("--min-call-mc", type=float, default=25_000)
    p.add_argument("--max-call-mc", type=float, default=750_000)
    p.add_argument("--require-hit", action="store_true")
    p.add_argument("--take-profit-x", type=float, default=2.0)
    p.add_argument("--stop-loss-pct", type=float, default=-60.0)
    p.add_argument("--write", action="store_true")
    p.add_argument("--raw", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    payload = run(
        Path(args.db).expanduser(),
        limit=args.limit,
        min_wallets=max(1, args.min_wallets),
        min_tg=max(0, args.min_tg),
        min_call_mc=args.min_call_mc,
        max_call_mc=args.max_call_mc,
        require_hit=args.require_hit,
        take_profit_x=max(1.01, args.take_profit_x),
        stop_loss_pct=min(0.0, args.stop_loss_pct),
    )
    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return 0 if payload.get("ok") else 2
    md = render_md(payload)
    if args.write and payload.get("ok"):
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"alpha_paper_trade_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", encoding="utf-8")
        print(f"☄️ Alpha paper-trade report written: {path}")
    print(md)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
