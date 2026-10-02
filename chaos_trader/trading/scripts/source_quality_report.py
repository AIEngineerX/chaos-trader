#!/usr/bin/env python3
"""Chaos Source Quality v1.

Scores source surfaces from measured Event Tape outcomes. This keeps source edge
separate from token confidence: X/TG/DEX/source claims are not alpha until their
own outcome history is positive and repeatable.

Read-only local DB/report generation. No scraping, alerts, webhooks, execution,
posting, or network calls.
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

from event_outcome_tracker import DEFAULT_DB, ensure_schema
from signal_ledger import connect

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
REPORT_DIR = PROFILE_HOME / "trading" / "reports"
BOUNDARY = "read-only source quality calibration; no scraping, alerts, posting, execution, or network calls"


def median(values: list[Any]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(statistics.median(clean), 6) if clean else None


def pct(n: int, d: int) -> float | None:
    return round((n / d) * 100, 2) if d else None


def load_source_rows(con: sqlite3.Connection, *, window: str | None = None, include_late_baseline: bool = False) -> list[dict[str, Any]]:
    ensure_schema(con)
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
        SELECT e.source,e.source_key,e.source_endpoint,e.event_type,e.chain_id,e.token_address,
               e.baseline_status,o.window_label,o.return_pct,o.max_return_pct,o.max_drawdown_pct,
               o.dead_or_alive,o.outcome_status,o.calibration_valid
        FROM event_outcomes o JOIN event_outcome_events e ON e.event_id=o.event_id
        {clause}
        ORDER BY e.source_key,o.window_label,e.event_id
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def source_id(row: dict[str, Any]) -> str:
    src = row.get("source") or "unknown"
    key = row.get("source_key") or row.get("source_endpoint") or row.get("event_type") or "unknown"
    return f"{src}:{key}"


def score_source(count: int, median_return: float | None, runner_rate: float | None, severe_rate: float | None, dead_rate: float | None, *, min_count: int) -> tuple[float, str, list[str]]:
    med = median_return or 0.0
    rr = runner_rate or 0.0
    sr = severe_rate or 0.0
    dr = dead_rate or 0.0
    sample_factor = min(20.0, count / max(1, min_count) * 20.0)
    score = 50.0 + min(30.0, med / 3.0) + rr * 0.35 - sr * 0.4 - dr * 0.25 + sample_factor - 20.0
    score = round(max(0.0, min(100.0, score)), 3)
    reasons: list[str] = []
    if count < min_count:
        verdict = "unproven-source"
        reasons.append("sample below minimum")
    elif med >= 25 and rr >= 15 and sr <= 25:
        verdict = "positive-source-watch"
        reasons.append("positive median/runner profile")
    elif med <= -25 or sr >= 40 or dr >= 35:
        verdict = "negative-source-avoid"
        reasons.append("drawdown/dead profile dominates")
    else:
        verdict = "neutral-source-watch-more"
        reasons.append("mixed or flat source profile")
    return score, verdict, reasons


def aggregate_sources(rows: list[dict[str, Any]], *, min_count: int = 20) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(source_id(row), []).append(row)
    out: list[dict[str, Any]] = []
    for sid, items in groups.items():
        returns = [r.get("return_pct") for r in items]
        runners = sum(1 for r in items if r.get("outcome_status") == "runner" or ((r.get("return_pct") or 0) >= 100))
        severe = sum(1 for r in items if r.get("outcome_status") == "severe_drawdown" or ((r.get("return_pct") or 0) <= -70))
        dead = sum(1 for r in items if r.get("dead_or_alive") == "dead")
        positive = sum(1 for r in items if r.get("return_pct") is not None and float(r.get("return_pct") or 0) > 0)
        med = median(returns)
        rr = pct(runners, len(items))
        sr = pct(severe, len(items))
        dr = pct(dead, len(items))
        score, verdict, reasons = score_source(len(items), med, rr, sr, dr, min_count=min_count)
        out.append({
            "source_id": sid,
            "source": items[0].get("source"),
            "source_key": items[0].get("source_key"),
            "source_endpoint": items[0].get("source_endpoint"),
            "event_types": sorted({str(r.get("event_type") or "unknown") for r in items}),
            "windows": sorted({str(r.get("window_label") or "unknown") for r in items}),
            "count": len(items),
            "median_return_pct": med,
            "positive_rate_pct": pct(positive, len(items)),
            "runner_rate_pct": rr,
            "severe_drawdown_rate_pct": sr,
            "dead_rate_pct": dr,
            "score": score,
            "verdict": verdict,
            "reasons": reasons,
        })
    out.sort(key=lambda r: (r["score"], r["count"]), reverse=True)
    return out


def run(db: Path, *, window: str | None = None, include_late_baseline: bool = False, min_count: int = 20, limit: int = 100) -> dict[str, Any]:
    if not db.exists():
        return {"ok": False, "error": f"missing db: {db}", "rows": [], "summary": {}, "boundary": BOUNDARY}
    con = connect(db)
    try:
        rows = load_source_rows(con, window=window, include_late_baseline=include_late_baseline)
    finally:
        con.close()
    sources = aggregate_sources(rows, min_count=max(1, min_count))
    summary = {
        "outcome_rows": len(rows),
        "sources_scored": len(sources),
        "positive_source_watch": sum(1 for r in sources if r["verdict"] == "positive-source-watch"),
        "negative_source_avoid": sum(1 for r in sources if r["verdict"] == "negative-source-avoid"),
        "unproven_source": sum(1 for r in sources if r["verdict"] == "unproven-source"),
    }
    return {
        "ok": True,
        "mode": "chaos_source_quality_v1",
        "db": str(db),
        "window": window,
        "include_late_baseline": include_late_baseline,
        "min_count": max(1, min_count),
        "summary": summary,
        "rows": sources[: max(1, min(limit, 1000))],
        "boundary": BOUNDARY,
    }


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Chaos Source Quality v1",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Window: {payload.get('window') or 'all'}",
        f"Minimum count: {payload.get('min_count')}",
        f"Boundary: {BOUNDARY}.",
        "",
        "## Summary",
        "",
    ]
    for k, v in (payload.get("summary") or {}).items():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "## Ranked sources",
        "",
        "| Verdict | Score | Source | Count | Median return | Runner | Severe DD | Dead | Event types |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload.get("rows") or []:
        lines.append(f"| {row['verdict']} | {row['score']} | `{row['source_id']}` | {row['count']} | {row['median_return_pct']}% | {row['runner_rate_pct']}% | {row['severe_drawdown_rate_pct']}% | {row['dead_rate_pct']}% | {', '.join(row['event_types'])} |")
    if not payload.get("rows"):
        lines.append("| no sources | 0 | - | 0 | - | - | - | - | - |")
    lines += [
        "",
        "## Discipline",
        "",
        "- Source quality is independent from token confidence.",
        "- Small samples stay `unproven-source`; they must not trigger alerts.",
        "- Late baselines are excluded unless explicitly requested.",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Score Chaos source surfaces from measured event outcomes")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--window", default=None)
    p.add_argument("--include-late-baseline", action="store_true")
    p.add_argument("--min-count", type=int, default=20)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--write", action="store_true")
    p.add_argument("--raw", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    payload = run(Path(args.db).expanduser(), window=args.window, include_late_baseline=args.include_late_baseline, min_count=args.min_count, limit=args.limit)
    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return 0 if payload.get("ok") else 2
    md = render_md(payload)
    if args.write and payload.get("ok"):
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        suffix = args.window or "all"
        path = REPORT_DIR / f"source_quality_{suffix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", encoding="utf-8")
        print(f"☄️ Source quality report written: {path}")
    print(md)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
