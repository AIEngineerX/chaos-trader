#!/usr/bin/env python3
"""Chaos signal calibration report.

Aggregates signal outcomes by verdict/window so heuristics can be killed or kept
based on observed results. No execution, no advice automation.
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from signal_ledger import DEFAULT_DB, connect  # noqa: E402

REPORT_DIR = PROFILE_HOME / "trading" / "reports"

OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    window_label TEXT NOT NULL,
    checked_at_utc TEXT NOT NULL,
    target_time_utc TEXT,
    target_lag_seconds INTEGER,
    late_snapshot INTEGER NOT NULL DEFAULT 0,
    primary_eligible INTEGER NOT NULL DEFAULT 0,
    observation_source TEXT,
    observation_sha256 TEXT,
    legacy_classification TEXT,
    age_seconds INTEGER NOT NULL,
    price_usd_now REAL,
    liquidity_usd_now REAL,
    market_cap_now REAL,
    fdv_now REAL,
    return_pct REAL,
    liquidity_change_pct REAL,
    market_cap_change_pct REAL,
    dead_or_alive TEXT NOT NULL,
    outcome_status TEXT NOT NULL,
    dex_url TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(signal_id, window_label),
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_signal_window ON outcomes(signal_id, window_label);
CREATE INDEX IF NOT EXISTS idx_outcomes_status ON outcomes(outcome_status);
"""


def ensure_outcomes(con: sqlite3.Connection) -> None:
    con.executescript(OUTCOME_SCHEMA)
    existing = {str(row[1]) for row in con.execute("PRAGMA table_info(outcomes)").fetchall()}
    wanted = {
        "target_time_utc": "TEXT",
        "target_lag_seconds": "INTEGER",
        "late_snapshot": "INTEGER NOT NULL DEFAULT 0",
        "primary_eligible": "INTEGER NOT NULL DEFAULT 0",
        "observation_source": "TEXT",
        "observation_sha256": "TEXT",
        "legacy_classification": "TEXT",
    }
    for name, decl in wanted.items():
        if name not in existing:
            con.execute(f"ALTER TABLE outcomes ADD COLUMN {name} {decl}")
    con.commit()

def median(values: list[float]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(statistics.median(clean), 6) if clean else None


def pct(n: int, d: int) -> float | None:
    return round((n / d) * 100, 2) if d else None


def load_rows(con: sqlite3.Connection, *, min_id: int = 0, primary_only: bool = True) -> list[dict[str, Any]]:
    primary_filter = "AND COALESCE(o.primary_eligible,0)=1" if primary_only else ""
    rows = con.execute(
        f"""
        SELECT s.id AS signal_id,s.timestamp_utc,s.source_command,s.mint,s.symbol,s.verdict,s.score,s.fact_grade,
               o.window_label,o.return_pct,o.liquidity_change_pct,o.outcome_status,o.dead_or_alive,
               COALESCE(o.primary_eligible,0) AS primary_eligible,COALESCE(o.late_snapshot,0) AS late_snapshot,
               o.target_lag_seconds,o.legacy_classification
        FROM outcomes o JOIN signals s ON s.id=o.signal_id
        WHERE s.id >= ? {primary_filter}
        ORDER BY s.verdict,o.window_label,s.id
        """,
        (min_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def disclosure_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "eligible": sum(1 for r in rows if int(r.get("primary_eligible") or 0) == 1),
        "late": sum(1 for r in rows if int(r.get("late_snapshot") or 0) == 1),
        "missing": sum(1 for r in rows if str(r.get("outcome_status") or "").startswith("missing")),
        "non_exitable": sum(1 for r in rows if r.get("outcome_status") == "non_exitable"),
        "unresolved": sum(1 for r in rows if r.get("outcome_status") in {"flat_or_unresolved", "unresolved"}),
        "legacy": sum(1 for r in rows if r.get("legacy_classification") or (not r.get("target_lag_seconds") and int(r.get("primary_eligible") or 0) == 0)),
    }


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row.get("verdict") or "unknown"), str(row.get("window_label") or "unknown"))].append(row)
    out = []
    for (verdict, window), items in sorted(groups.items()):
        returns = [r.get("return_pct") for r in items if r.get("return_pct") is not None]
        runners = sum(1 for r in items if r.get("outcome_status") == "runner" or ((r.get("return_pct") or 0) >= 100))
        severe = sum(1 for r in items if r.get("outcome_status") == "severe_drawdown" or ((r.get("return_pct") or 0) <= -70))
        positive = sum(1 for r in items if (r.get("return_pct") is not None and r.get("return_pct") > 0))
        counts = disclosure_counts(items)
        out.append({
            "verdict": verdict,
            "window": window,
            "count": len(items),
            "eligible": counts["eligible"],
            "late": counts["late"],
            "missing": counts["missing"],
            "non_exitable": counts["non_exitable"],
            "unresolved": counts["unresolved"],
            "legacy": counts["legacy"],
            "median_return_pct": median(returns),
            "positive_rate_pct": pct(positive, len(items)),
            "runner_rate_pct": pct(runners, len(items)),
            "severe_drawdown_rate_pct": pct(severe, len(items)),
        })
    return out


def render_md(rows: list[dict[str, Any]], agg: list[dict[str, Any]], *, all_rows: int | None = None, counts: dict[str, int] | None = None) -> str:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    disclosure = counts or disclosure_counts(rows)
    lines = [
        "# Chaos Signal Calibration",
        "",
        f"Generated: {generated}",
        f"Calibration-eligible outcome rows: {len(rows)}",
        f"All outcome rows: {all_rows if all_rows is not None else len(rows)}",
        f"Excluded/disclosure: late={disclosure.get('late', 0)}, missing={disclosure.get('missing', 0)}, non-exitable={disclosure.get('non_exitable', 0)}, unresolved={disclosure.get('unresolved', 0)}, legacy={disclosure.get('legacy', 0)}",
        "Boundary: read-only calibration; no execution.",
        "",
        "## Verdict performance",
        "",
        "| Verdict | Window | Count | Eligible | Late | Missing | Non-exitable | Unresolved | Legacy | Median return | Positive | Runner | Severe DD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if not rows:
        lines.insert(8, "WARNING: no calibration-eligible exact-horizon sample exists; performance is uncalibrated.")
    for r in agg:
        lines.append(
            f"| {r['verdict']} | {r['window']} | {r['count']} | {r.get('eligible', 0)} | {r.get('late', 0)} | {r.get('missing', 0)} | {r.get('non_exitable', 0)} | {r.get('unresolved', 0)} | {r.get('legacy', 0)} | {r['median_return_pct']}% | {r['positive_rate_pct']}% | {r['runner_rate_pct']}% | {r['severe_drawdown_rate_pct']}% |"
        )
    lines += [
        "",
        "## Read discipline",
        "",
        "- Small samples are not edge. Treat this as calibration telemetry until counts are large.",
        "- Entry gates and position actions are intentionally separate; owner-position rows measure private management reads, not public alpha.",
        "- Fake-flow conversion labels should be judged by later holder/price/liquidity survival, not by first-read excitement.",
        "- `avoid-entry` working means it caught decay/drawdown; `watch/manual-review/manage` must beat that baseline or be downgraded.",
        "- Liquidity survival matters as much as price return for memecoin exits.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Build Chaos signal calibration report")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--min-id", type=int, default=0)
    p.add_argument("--write", action="store_true")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()

    con = connect(Path(args.db).expanduser())
    try:
        ensure_outcomes(con)
        all_rows = load_rows(con, min_id=args.min_id, primary_only=False)
        rows = load_rows(con, min_id=args.min_id, primary_only=True)
    finally:
        con.close()
    agg = aggregate(rows)
    counts = disclosure_counts(all_rows)
    if args.raw:
        print(json.dumps({"ok": True, "rows": len(rows), "all_rows": len(all_rows), "disclosure_counts": counts, "aggregate": agg}, indent=2, ensure_ascii=False))
        return
    md = render_md(rows, agg, all_rows=len(all_rows), counts=counts)
    if args.write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"signal_calibration_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", encoding="utf-8")
        print(f"☄️ Calibration report written: {path}")
    print(md)


if __name__ == "__main__":
    main()
