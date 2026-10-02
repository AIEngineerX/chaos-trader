#!/usr/bin/env python3
"""Chaos signal outcome tracker.

Reprices prior read-only token signals at fixed windows so verdicts can be
calibrated against reality. No execution, no alerts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from dexscreener_client import fetch_token  # noqa: E402
from signal_ledger import DEFAULT_DB, connect  # noqa: E402

WINDOWS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
}
PRIMARY_TOLERANCE_SECONDS = 5 * 60
# A mint with several reads due in one tick costs one set of three DexScreener calls, so a mark's price can be
# up to this many seconds older than its check time.
FETCH_TTL_SECONDS = 60
LEGACY_WINDOW_LABELS = {"15m", "1h", "4h", "24h"}
P0_OUTCOME_VERSION_AT = datetime(2026, 7, 25, tzinfo=timezone.utc)

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


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_float(value: Any) -> float | None:
    try:
        if value in (None, "", [], {}):
            return None
        return float(value)
    except Exception:
        return None


def pct(now_value: float | None, start_value: float | None) -> float | None:
    if now_value is None or start_value is None or start_value == 0:
        return None
    return round(((now_value / start_value) - 1.0) * 100.0, 6)


def ensure_outcomes(con: sqlite3.Connection) -> None:
    con.executescript(OUTCOME_SCHEMA)
    existing = {row[1] for row in con.execute("PRAGMA table_info(outcomes)").fetchall()}
    wanted = {
        "target_time_utc": "TEXT",
        "target_lag_seconds": "INTEGER",
        "late_snapshot": "INTEGER NOT NULL DEFAULT 0",
        "primary_eligible": "INTEGER NOT NULL DEFAULT 0",
        "observation_source": "TEXT",
        "observation_sha256": "TEXT",
        "legacy_classification": "TEXT",
    }
    for name, typ in wanted.items():
        if name not in existing:
            con.execute(f"ALTER TABLE outcomes ADD COLUMN {name} {typ}")
    con.commit()


def _due_windows_for_signal(signal: dict[str, Any], done: set[str], *, at: datetime | None = None) -> list[tuple[str, timedelta]]:
    ts = parse_ts(signal["timestamp_utc"])
    age = (at or now()) - ts
    windows = WINDOWS
    # Preserve historical cohort shape: pre-P0 signals that already have the
    # original four windows are classified as legacy-complete, not silently
    # expanded into new 3d/7d targets after the fact.
    if ts < P0_OUTCOME_VERSION_AT and LEGACY_WINDOW_LABELS.issubset(done):
        windows = {k: v for k, v in WINDOWS.items() if k in LEGACY_WINDOW_LABELS}
    return [(label, delta) for label, delta in windows.items() if age >= delta and label not in done]


def due_signals(con: sqlite3.Connection, *, limit: int, include_candidates: bool) -> list[dict[str, Any]]:
    where = ""
    if not include_candidates:
        where = "WHERE verdict IS NULL OR verdict != 'sweep-candidate-unread'"
    # Prioritize rows inside an exact-horizon tolerance window. An oldest-first
    # bounded collector can otherwise drain legacy backlog while fresh targets
    # become late and permanently ineligible for calibration.
    timely: list[tuple[int, dict[str, Any]]] = []
    backlog: list[dict[str, Any]] = []
    checked_at = now()
    wanted = max(1, min(limit, 1000))
    completed: dict[int, set[str]] = {}
    for row in con.execute("SELECT signal_id,window_label FROM outcomes"):
        completed.setdefault(int(row[0]), set()).add(str(row[1]))
    rows = con.execute(f"SELECT * FROM signals {where} ORDER BY timestamp_utc ASC").fetchall()
    for row in rows:
        signal = dict(row)
        windows = _due_windows_for_signal(signal, completed.get(int(signal["id"]), set()), at=checked_at)
        if not windows:
            continue
        ts = parse_ts(signal["timestamp_utc"])
        lags = [int((checked_at - (ts + delta)).total_seconds()) for _label, delta in windows]
        in_tolerance = [lag for lag in lags if 0 <= lag <= PRIMARY_TOLERANCE_SECONDS]
        if in_tolerance:
            # Seconds left before this read's nearest mark stops counting; the read with the least goes first.
            timely.append((PRIMARY_TOLERANCE_SECONDS - max(in_tolerance), signal))
        else:
            backlog.append(signal)
    timely.sort(key=lambda item: (item[0], int(item[1]["id"])))
    return [item[1] for item in timely[:wanted]] + backlog[: max(0, wanted - len(timely))]


def existing_windows(con: sqlite3.Connection, signal_id: int) -> set[str]:
    rows = con.execute("SELECT window_label FROM outcomes WHERE signal_id=?", (signal_id,)).fetchall()
    return {str(r[0]) for r in rows}


def classify_outcome(return_pct: float | None, liq_now: float | None, liq_start: float | None) -> tuple[str, str]:
    if liq_now is None or liq_now < 1000:
        return "dead", "non_exitable"
    liq_decay = pct(liq_now, liq_start)
    if return_pct is not None and return_pct <= -70:
        return "alive" if liq_now >= 1000 else "dead", "severe_drawdown"
    if liq_decay is not None and liq_decay <= -70:
        return "alive", "liquidity_decay"
    if return_pct is not None and return_pct >= 100:
        return "alive", "runner"
    if return_pct is not None and return_pct >= 25:
        return "alive", "positive"
    if return_pct is not None and return_pct <= -25:
        return "alive", "negative"
    return "alive", "observed"


def _observation_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def track_signal(con: sqlite3.Connection, signal: dict[str, Any], *, dry_run: bool = False) -> list[dict[str, Any]]:
    ts = parse_ts(signal["timestamp_utc"])
    checked_at = now()
    age = checked_at - ts
    done = existing_windows(con, int(signal["id"]))
    due = _due_windows_for_signal(signal, done, at=checked_at)
    if not due:
        return []

    due_meta: list[tuple[str, timedelta, datetime, int, bool]] = []
    for label, delta in due:
        target_time = ts + delta
        target_lag = int((checked_at - target_time).total_seconds())
        late_snapshot = target_lag > PRIMARY_TOLERANCE_SECONDS
        due_meta.append((label, delta, target_time, max(0, target_lag), late_snapshot))

    # Causality guard: a restarted/late collector must not fetch one current
    # market mark and launder it into multiple fixed horizons. Only in-tolerance
    # targets receive observed price/MC marks; overdue targets become explicit
    # late/missing diagnostics and are excluded from primary calibration.
    should_fetch = any(not late for (_label, _delta, _target_time, _lag, late) in due_meta)
    summary: dict[str, Any] = {}
    # A pair gone from DexScreener is a -100% mark only when every endpoint answered and none listed a pair.
    # An endpoint that errored proves nothing, so that mark stays `missing` and the run exits 2.
    delisted = fetch_failed = False
    if should_fetch:
        dex = fetch_token("solana", signal["mint"], cache=True, ttl_seconds=FETCH_TTL_SECONDS)
        summary = dex.get("summary") or {}
        if dex.get("pair_count") == 0:
            fetch_failed = bool(dex.get("errors"))
            delisted = not fetch_failed
    price_now = as_float(summary.get("priceUsd"))
    liq_now = as_float(summary.get("liquidity_usd"))
    mc_now = as_float(summary.get("marketCap"))
    fdv_now = as_float(summary.get("fdv"))
    price_start = as_float(signal.get("price_usd_at_scan"))
    liq_start = as_float(signal.get("liquidity_usd"))
    mc_start = as_float(signal.get("market_cap"))
    ret = pct(price_now, price_start)
    liq_chg = pct(liq_now, liq_start)
    mc_chg = pct(mc_now, mc_start)

    written = []
    for label, delta, target_time, target_lag, late_snapshot in due_meta:
        has_mark = should_fetch and (price_now is not None or mc_now is not None)
        primary_eligible = bool(has_mark and not late_snapshot)
        if late_snapshot:
            alive, status = "dead", "missing_late"
            row_price = row_liq = row_mc = row_fdv = row_ret = row_liq_chg = row_mc_chg = None
        elif delisted and price_start is not None:
            alive, status = "dead", "delisted"
            primary_eligible = True
            row_price = row_liq = row_mc = row_fdv = row_liq_chg = row_mc_chg = None
            row_ret = -100.0
        elif not has_mark:
            alive, status = "dead", "missing"
            row_price = row_liq = row_mc = row_fdv = row_ret = row_liq_chg = row_mc_chg = None
        else:
            alive, classified_status = classify_outcome(ret, liq_now, liq_start)
            status = "non_exitable" if classified_status == "non_exitable" else "observed"
            row_price, row_liq, row_mc, row_fdv = price_now, liq_now, mc_now, fdv_now
            row_ret, row_liq_chg, row_mc_chg = ret, liq_chg, mc_chg
        raw = {
            "dex_summary": summary if should_fetch else {},
            "target_time_utc": target_time.isoformat(timespec="seconds"),
            "observed_at_utc": checked_at.isoformat(timespec="seconds"),
            "late_snapshot": late_snapshot,
            "primary_eligible": primary_eligible,
            "causality_policy": "p0_fixed_horizon_v1",
        }
        row = {
            "signal_id": signal["id"],
            "window_label": label,
            "checked_at_utc": checked_at.isoformat(timespec="seconds"),
            "target_time_utc": target_time.isoformat(timespec="seconds"),
            "target_lag_seconds": target_lag,
            "late_snapshot": 1 if late_snapshot else 0,
            "primary_eligible": 1 if primary_eligible else 0,
            "observation_source": "causality_guard_missing_late" if late_snapshot else "dexscreener_current",
            "observation_sha256": _observation_hash(raw),
            "legacy_classification": None,
            "age_seconds": int(age.total_seconds()),
            "price_usd_now": row_price,
            "liquidity_usd_now": row_liq,
            "market_cap_now": row_mc,
            "fdv_now": row_fdv,
            "return_pct": row_ret,
            "liquidity_change_pct": row_liq_chg,
            "market_cap_change_pct": row_mc_chg,
            "dead_or_alive": alive,
            "outcome_status": status,
            "dex_url": summary.get("url") if should_fetch else None,
            "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str),
        }
        written.append({"signal": signal["id"], "mint": signal["mint"], "window": label, "return_pct": row_ret, "status": status, "late_snapshot": late_snapshot, "target_lag_seconds": target_lag, "primary_eligible": primary_eligible, "fetch_failed": fetch_failed and not late_snapshot})
        if dry_run:
            continue
        cols = list(row.keys())
        con.execute(
            f"INSERT OR IGNORE INTO outcomes ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
    if not dry_run:
        con.commit()
    time.sleep(0.12)
    return written


def summarize(con: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT o.id,o.window_label,o.return_pct,o.outcome_status,o.dead_or_alive,
               s.id AS signal_id,s.timestamp_utc,s.source_command,s.mint,s.symbol,s.verdict,s.score,s.fact_grade
        FROM outcomes o JOIN signals s ON s.id=o.signal_id
        ORDER BY o.id DESC LIMIT ?
        """,
        (max(1, min(limit, 200)),),
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    p = argparse.ArgumentParser(description="Track Chaos signal outcomes. Read-only market repricing only.")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--include-candidates", action="store_true", help="Also track low-grade sweep-candidate-unread rows")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--summary", action="store_true", help="Print recent outcomes only")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()

    con = connect(Path(args.db).expanduser())
    ensure_outcomes(con)
    try:
        if args.summary:
            rows = summarize(con, args.limit)
            print(json.dumps({"ok": True, "db_path": args.db, "outcomes": rows}, indent=2, ensure_ascii=False) if args.raw else render_rows(rows))
            return
        written: list[dict[str, Any]] = []
        due = due_signals(con, limit=args.limit, include_candidates=args.include_candidates)
        for signal in due:
            written.extend(track_signal(con, signal, dry_run=args.dry_run))
        unanswered = len({row["signal"] for row in written if row["fetch_failed"]})
        if args.raw:
            print(json.dumps({"ok": not unanswered, "dry_run": args.dry_run, "due": len(due), "written": written}, indent=2, ensure_ascii=False))
        else:
            print(f"☄️ Outcome tracker · {'dry-run ' if args.dry_run else ''}{len(due)} due · {len(written)} outcomes")
            for row in written[:25]:
                print(f"signal#{row['signal']} {row['window']} {row['mint'][:6]}…{row['mint'][-4:]} return={row['return_pct']}% status={row['status']}")
            if unanswered:
                print(f"DexScreener did not answer for {unanswered} read{'s' if unanswered != 1 else ''}; those marks are recorded missing")
            print("read-only repricing; no execution")
    finally:
        con.close()
    if unanswered:
        raise SystemExit(2)


def render_rows(rows: list[dict[str, Any]]) -> str:
    lines = [f"☄️ Recent outcomes · {len(rows)}"]
    for r in rows:
        lines.append(f"#{r['id']} signal#{r['signal_id']} {r['window_label']} {r.get('symbol') or 'UNKNOWN'} {r['mint'][:6]}…{r['mint'][-4:]} {r.get('verdict')} return={r.get('return_pct')}% {r.get('outcome_status')}")
    lines.append("read-only repricing; no execution")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
