#!/usr/bin/env python3
"""Chaos interruption simulator v1.

Simulates whether calibrated event/source classes would deserve a human interrupt.
It never sends alerts. This is a policy/backtest scaffold, not trading advice and
not a production signal bot.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_outcome_report import aggregate as aggregate_events
from event_outcome_report import load_rows as load_event_rows
from event_outcome_tracker import DEFAULT_DB, ensure_schema
from signal_ledger import connect
from source_quality_report import aggregate_sources, load_source_rows, source_id

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
REPORT_DIR = PROFILE_HOME / "trading" / "reports"
BOUNDARY = "read-only interruption simulation; no alerts, posting, execution, wallets, scraping, or webhooks"


def event_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("event_type") or "unknown"), str(row.get("window_label") or "unknown"))


def source_key(row: dict[str, Any]) -> str:
    return source_id(row)


def build_event_verdicts(rows: list[dict[str, Any]], *, min_count: int) -> dict[tuple[str, str], dict[str, Any]]:
    agg = aggregate_events(rows, group_by="event_type", min_count=min_count)
    return {(str(r["group"]), str(r["window"])): r for r in agg}


def build_source_verdicts(rows: list[dict[str, Any]], *, min_count: int) -> dict[str, dict[str, Any]]:
    agg = aggregate_sources(rows, min_count=min_count)
    return {str(r["source_id"]): r for r in agg}


def would_interrupt(row: dict[str, Any], event_v: dict[str, Any] | None, source_v: dict[str, Any] | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not event_v or event_v.get("edge_state") != "positive-watch":
        reasons.append("event_class_not_positive")
    else:
        reasons.append("event_class_positive")
    if not source_v or source_v.get("verdict") != "positive-source-watch":
        reasons.append("source_not_positive")
    else:
        reasons.append("source_positive")
    if int(row.get("calibration_valid") or 0) != 1:
        reasons.append("invalid_or_late_baseline")
    if row.get("dead_or_alive") == "dead":
        reasons.append("dead_liquidity_profile")
    ok = "event_class_positive" in reasons and "source_positive" in reasons and "invalid_or_late_baseline" not in reasons and "dead_liquidity_profile" not in reasons
    return ok, reasons


def simulate(rows: list[dict[str, Any]], source_rows: list[dict[str, Any]], *, min_count: int = 20, limit: int = 100) -> dict[str, Any]:
    event_verdicts = build_event_verdicts(rows, min_count=min_count)
    source_verdicts = build_source_verdicts(source_rows, min_count=min_count)
    simulated: list[dict[str, Any]] = []
    for row in rows:
        ev = event_verdicts.get(event_key(row))
        sv = source_verdicts.get(source_key(row))
        alert, reasons = would_interrupt(row, ev, sv)
        if not alert:
            continue
        simulated.append({
            "event_id": row.get("event_id"),
            "event_type": row.get("event_type"),
            "source_id": source_key(row),
            "window": row.get("window_label"),
            "token_address": row.get("token_address"),
            "return_pct": row.get("return_pct"),
            "outcome_status": row.get("outcome_status"),
            "reasons": reasons,
        })
    runners = sum(1 for r in simulated if r.get("outcome_status") == "runner" or ((r.get("return_pct") or 0) >= 100))
    positive = sum(1 for r in simulated if r.get("return_pct") is not None and float(r.get("return_pct") or 0) > 0)
    severe = sum(1 for r in simulated if r.get("outcome_status") == "severe_drawdown" or ((r.get("return_pct") or 0) <= -70))
    n = len(simulated)
    return {
        "ok": True,
        "mode": "chaos_interruption_simulator_v1",
        "rows_seen": len(rows),
        "simulated_interrupts": n,
        "summary": {
            "positive_rate_pct": round((positive / n) * 100, 2) if n else None,
            "runner_rate_pct": round((runners / n) * 100, 2) if n else None,
            "severe_drawdown_rate_pct": round((severe / n) * 100, 2) if n else None,
        },
        "rows": simulated[: max(1, min(limit, 1000))],
        "lookahead_caveat": "v1 uses completed calibration aggregates; use as threshold policy simulation, not causal historical alert proof",
        "boundary": BOUNDARY,
    }


def run(db: Path, *, window: str | None = None, min_count: int = 20, include_late_baseline: bool = False, limit: int = 100) -> dict[str, Any]:
    if not db.exists():
        return {"ok": False, "error": f"missing db: {db}", "rows": [], "summary": {}, "boundary": BOUNDARY}
    con = connect(db)
    try:
        ensure_schema(con)
        rows = load_event_rows(con, window=window, include_late_baseline=include_late_baseline)
        source_rows = load_source_rows(con, window=window, include_late_baseline=include_late_baseline)
    finally:
        con.close()
    return simulate(rows, source_rows, min_count=max(1, min_count), limit=limit)


def render_md(payload: dict[str, Any], *, window: str | None, min_count: int) -> str:
    lines = [
        "# Chaos Interruption Simulator v1",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Window: {window or 'all'}",
        f"Minimum count: {min_count}",
        f"Boundary: {BOUNDARY}.",
        f"Caveat: {payload.get('lookahead_caveat')}",
        "",
        "## Summary",
        "",
    ]
    for k, v in (payload.get("summary") or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append(f"- simulated_interrupts: {payload.get('simulated_interrupts')}")
    lines += [
        "",
        "## Simulated interrupts",
        "",
        "| Event | Source | Window | Token | Return | Outcome |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in payload.get("rows") or []:
        token = str(row.get("token_address") or "")
        lines.append(f"| {row.get('event_type')} | `{row.get('source_id')}` | {row.get('window')} | `{token[:6]}…{token[-4:]}` | {row.get('return_pct')}% | {row.get('outcome_status')} |")
    if not payload.get("rows"):
        lines.append("| none | - | - | - | - | no calibrated class cleared interrupt gate |")
    lines += [
        "",
        "## Discipline",
        "",
        "- This does not send Telegram alerts.",
        "- A real interrupt gate still needs chronological/out-of-sample validation.",
        "- Small samples and late baselines are blocked by default.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Simulate Chaos human-interrupt gates from calibrated event/source outcomes")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--window", default="1h")
    p.add_argument("--min-count", type=int, default=20)
    p.add_argument("--include-late-baseline", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--write", action="store_true")
    p.add_argument("--raw", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    payload = run(Path(args.db).expanduser(), window=args.window, min_count=args.min_count, include_late_baseline=args.include_late_baseline, limit=args.limit)
    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return 0 if payload.get("ok") else 2
    md = render_md(payload, window=args.window, min_count=max(1, args.min_count))
    if args.write and payload.get("ok"):
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        suffix = args.window or "all"
        path = REPORT_DIR / f"interruption_simulation_{suffix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", encoding="utf-8")
        print(f"☄️ Interruption simulation written: {path}")
    print(md)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
