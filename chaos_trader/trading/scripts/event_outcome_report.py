#!/usr/bin/env python3
"""Chaos Event Tape outcome report.

Aggregates measured Event Tape outcomes so event classes can be promoted,
downgraded, or kept unproven. No execution or alerting.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from event_outcome_tracker import DEFAULT_DB, ensure_schema  # noqa: E402
from signal_ledger import connect  # noqa: E402

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
REPORT_DIR = PROFILE_HOME / "trading" / "reports"
BOUNDARY = "read-only event outcome calibration report; no execution or alerts"


def median(values: list[Any]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(statistics.median(clean), 6) if clean else None


def pct(n: int, d: int) -> float | None:
    return round((n / d) * 100, 2) if d else None


def load_rows(con: sqlite3.Connection, *, window: str | None = None, include_late_baseline: bool = False) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if window:
        where.append("o.window_label = ?")
        params.append(window)
    if not include_late_baseline:
        where.append("o.calibration_valid = 1")
    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = con.execute(
        f"""
        SELECT e.event_id,e.event_type,e.source,e.source_key,e.source_endpoint,e.chain_id,e.token_address,
               e.observed_at,e.event_time,e.outcome_start_at,e.baseline_status,e.baseline_age_seconds,
               o.window_label,o.checked_at_utc,o.age_seconds,o.return_pct,o.liquidity_change_pct,
               o.market_cap_change_pct,o.max_return_pct,o.max_drawdown_pct,o.dead_or_alive,
               o.outcome_status,o.calibration_valid
        FROM event_outcomes o JOIN event_outcome_events e ON e.event_id=o.event_id
        {clause}
        ORDER BY e.event_type,o.window_label,e.event_id
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def late_baseline_count(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COUNT(*) FROM event_outcome_events WHERE baseline_status != 'valid_baseline'").fetchone()
    return int(row[0] or 0)


def group_key(row: dict[str, Any], group_by: str) -> str:
    if group_by == "source_key":
        return str(row.get("source_key") or "unknown")
    if group_by == "source_endpoint":
        return str(row.get("source_endpoint") or "unknown")
    if group_by == "event_type_source":
        return f"{row.get('event_type') or 'unknown'}::{row.get('source_key') or 'unknown'}"
    return str(row.get("event_type") or "unknown")


def edge_state(count: int, median_return: float | None, runner_rate: float | None, severe_rate: float | None, *, min_count: int) -> str:
    if count < min_count:
        return "unproven-small-sample"
    med = median_return or 0.0
    rr = runner_rate or 0.0
    sr = severe_rate or 0.0
    if med >= 25 and rr >= 15 and sr <= 25:
        return "positive-watch"
    if med <= -25 or sr >= 40:
        return "negative-avoid"
    return "neutral-watch-more"


def aggregate(rows: list[dict[str, Any]], *, group_by: str = "event_type", min_count: int = 20) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(group_key(row, group_by), str(row.get("window_label") or "unknown"))].append(row)
    out: list[dict[str, Any]] = []
    for (group, window), items in sorted(groups.items()):
        returns = [r.get("return_pct") for r in items]
        max_returns = [r.get("max_return_pct") for r in items]
        drawdowns = [r.get("max_drawdown_pct") for r in items]
        runners = sum(1 for r in items if r.get("outcome_status") == "runner" or ((r.get("return_pct") or 0) >= 100))
        positive = sum(1 for r in items if r.get("return_pct") is not None and float(r.get("return_pct") or 0) > 0)
        severe = sum(1 for r in items if r.get("outcome_status") == "severe_drawdown" or ((r.get("return_pct") or 0) <= -70))
        dead = sum(1 for r in items if r.get("dead_or_alive") == "dead")
        valid = sum(1 for r in items if int(r.get("calibration_valid") or 0) == 1)
        med = median(returns)
        runner_rate = pct(runners, len(items))
        severe_rate = pct(severe, len(items))
        out.append({
            "group": group,
            "window": window,
            "count": len(items),
            "valid_count": valid,
            "median_return_pct": med,
            "median_max_return_pct": median(max_returns),
            "median_max_drawdown_pct": median(drawdowns),
            "positive_rate_pct": pct(positive, len(items)),
            "runner_rate_pct": runner_rate,
            "severe_drawdown_rate_pct": severe_rate,
            "dead_rate_pct": pct(dead, len(items)),
            "edge_state": edge_state(len(items), med, runner_rate, severe_rate, min_count=min_count),
        })
    return out


def examples(rows: list[dict[str, Any]], *, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    eligible = [r for r in rows if r.get("return_pct") is not None]
    best = sorted(eligible, key=lambda r: float(r.get("return_pct") or 0), reverse=True)[:limit]
    worst = sorted(eligible, key=lambda r: float(r.get("return_pct") or 0))[:limit]
    def slim(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": row.get("event_id"),
            "event_type": row.get("event_type"),
            "source_key": row.get("source_key"),
            "window": row.get("window_label"),
            "token_address": row.get("token_address"),
            "return_pct": row.get("return_pct"),
            "status": row.get("outcome_status"),
        }
    return {"best": [slim(r) for r in best], "worst": [slim(r) for r in worst]}


def render_md(rows: list[dict[str, Any]], agg: list[dict[str, Any]], *, window: str | None, group_by: str, min_count: int, late_count: int) -> str:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Chaos Event Outcome Calibration",
        "",
        f"Generated: {generated}",
        f"Window filter: {window or 'all'}",
        f"Group by: {group_by}",
        f"Outcome rows: {len(rows)}",
        f"Minimum count for edge label: {min_count}",
        f"Late baselines excluded: {late_count}",
        f"Boundary: {BOUNDARY}.",
        "",
        "## Event performance",
        "",
        "| Group | Window | Count | Median return | Median max up | Median drawdown | Positive | Runner | Severe DD | Dead | Edge state |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    if not agg:
        lines.append("| no calibrated rows yet | - | 0 | - | - | - | - | - | - | - | unproven |")
    for r in agg:
        lines.append(
            f"| {r['group']} | {r['window']} | {r['count']} | {r['median_return_pct']}% | {r['median_max_return_pct']}% | {r['median_max_drawdown_pct']}% | {r['positive_rate_pct']}% | {r['runner_rate_pct']}% | {r['severe_drawdown_rate_pct']}% | {r['dead_rate_pct']}% | {r['edge_state']} |"
        )
    ex = examples(rows)
    lines += ["", "## Best examples", ""]
    for row in ex["best"] or []:
        lines.append(f"- `{row['token_address']}` {row['event_type']} {row['window']} return={row['return_pct']}% status={row['status']}")
    if not ex["best"]:
        lines.append("- none yet")
    lines += ["", "## Worst examples", ""]
    for row in ex["worst"] or []:
        lines.append(f"- `{row['token_address']}` {row['event_type']} {row['window']} return={row['return_pct']}% status={row['status']}")
    if not ex["worst"]:
        lines.append("- none yet")
    lines += [
        "",
        "## Read discipline",
        "",
        "- Small samples are not edge. `unproven-small-sample` must not trigger alerts.",
        "- Late baselines are excluded by default because they cannot prove pre-move edge.",
        "- DEX paid/boost/profile/ad are attention events until outcome data proves otherwise.",
        "- Promote a class only after repeatable positive expectancy and liquidity survival.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build Chaos Event Tape outcome calibration report")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--window", default=None, help="Filter one window, e.g. 1h or 24h")
    p.add_argument("--group-by", choices=["event_type", "source_key", "source_endpoint", "event_type_source"], default="event_type")
    p.add_argument("--min-count", type=int, default=20)
    p.add_argument("--include-late-baseline", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--raw", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    try:
        ensure_schema(con)
        rows = load_rows(con, window=args.window, include_late_baseline=args.include_late_baseline)
        late_count = late_baseline_count(con)
    finally:
        con.close()
    agg = aggregate(rows, group_by=args.group_by, min_count=max(1, int(args.min_count)))
    if args.raw:
        print(json.dumps({
            "ok": True,
            "db": str(db),
            "window": args.window,
            "group_by": args.group_by,
            "rows": len(rows),
            "late_baselines": late_count,
            "aggregate": agg,
            "examples": examples(rows),
            "boundary": BOUNDARY,
        }, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return 0
    md = render_md(rows, agg, window=args.window, group_by=args.group_by, min_count=max(1, int(args.min_count)), late_count=late_count)
    if args.write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        suffix = args.window or "all"
        path = REPORT_DIR / f"event_outcome_calibration_{suffix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", encoding="utf-8")
        print(f"☄️ Event outcome report written: {path}")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
