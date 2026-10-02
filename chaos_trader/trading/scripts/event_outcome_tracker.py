#!/usr/bin/env python3
"""Chaos Event Tape outcome tracker.

Turns official-safe Event Tape rows into measured outcome rows at fixed windows.
Read-only: fetches public DEXScreener market context, writes local SQLite, no
alerts, no wallet connection, no execution.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from dexscreener_client import fetch_token  # noqa: E402
from event_tape import DEFAULT_OUT as DEFAULT_TAPE  # noqa: E402
from signal_ledger import connect  # noqa: E402

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "event_outcomes.sqlite"
BOUNDARY = "read-only event outcome calibration; no execution, alerts, wallets, scraping, or webhooks"
DEFAULT_WINDOWS = "5m,15m,1h,6h,24h"
DEFAULT_BASELINE_GRACE_SECONDS = 10 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_outcome_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT,
    source_key TEXT,
    source_endpoint TEXT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT,
    observed_at TEXT NOT NULL,
    event_time TEXT,
    outcome_start_at TEXT NOT NULL,
    baseline_checked_at TEXT,
    baseline_age_seconds INTEGER,
    baseline_status TEXT NOT NULL DEFAULT 'missing',
    baseline_price_usd REAL,
    baseline_liquidity_usd REAL,
    baseline_market_cap REAL,
    baseline_fdv REAL,
    baseline_dex_url TEXT,
    raw_event_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    window_label TEXT NOT NULL,
    checked_at_utc TEXT NOT NULL,
    age_seconds INTEGER NOT NULL,
    price_usd_now REAL,
    liquidity_usd_now REAL,
    market_cap_now REAL,
    fdv_now REAL,
    return_pct REAL,
    liquidity_change_pct REAL,
    market_cap_change_pct REAL,
    max_return_pct REAL,
    max_drawdown_pct REAL,
    dead_or_alive TEXT NOT NULL,
    outcome_status TEXT NOT NULL,
    calibration_valid INTEGER NOT NULL DEFAULT 0,
    dex_url TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(event_id, window_label),
    FOREIGN KEY(event_id) REFERENCES event_outcome_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_outcome_events_type ON event_outcome_events(event_type, outcome_start_at);
CREATE INDEX IF NOT EXISTS idx_event_outcomes_event_window ON event_outcomes(event_id, window_label);
CREATE INDEX IF NOT EXISTS idx_event_outcomes_valid_window ON event_outcomes(calibration_valid, window_label);
"""

MarketFetcher = Callable[[str, str], dict[str, Any]]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
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


def parse_windows(value: str) -> list[tuple[str, timedelta]]:
    windows: list[tuple[str, timedelta]] = []
    for raw in str(value or "").split(","):
        label = raw.strip().lower()
        if not label:
            continue
        unit = label[-1]
        try:
            amount = int(label[:-1])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"bad window label {label!r}; expected e.g. 5m,1h,24h") from exc
        if amount <= 0:
            raise argparse.ArgumentTypeError(f"bad window label {label!r}; amount must be positive")
        if unit == "m":
            delta = timedelta(minutes=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        elif unit == "d":
            delta = timedelta(days=amount)
        else:
            raise argparse.ArgumentTypeError(f"bad window unit {unit!r}; use m/h/d")
        windows.append((label, delta))
    if not windows:
        raise argparse.ArgumentTypeError("at least one outcome window is required")
    return windows


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def load_tape(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return events, [f"missing tape: {path}"]
    for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_no}: invalid JSONL: {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"{path}:{line_no}: row is not an object")
            continue
        events.append(row)
    return events, errors


def event_start(event: dict[str, Any]) -> datetime | None:
    return parse_ts(event.get("event_time")) or parse_ts(event.get("observed_at"))


def valid_event(event: dict[str, Any]) -> tuple[bool, str | None]:
    if not event.get("event_id"):
        return False, "missing event_id"
    if not event.get("event_type"):
        return False, "missing event_type"
    if not event.get("chain_id"):
        return False, "missing chain_id"
    if not event.get("token_address"):
        return False, "missing token_address"
    if event_start(event) is None:
        return False, "missing/invalid observed_at/event_time"
    return True, None


def default_market_fetcher(chain: str, token: str) -> dict[str, Any]:
    return fetch_token(chain, token, cache=True)


def market_snapshot(event: dict[str, Any], fetcher: MarketFetcher = default_market_fetcher) -> dict[str, Any]:
    chain = str(event["chain_id"])
    token = str(event["token_address"])
    dex = fetcher(chain, token)
    summary = dex.get("summary") if isinstance(dex, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    return {
        "price_usd": as_float(summary.get("priceUsd") or summary.get("price_usd")),
        "liquidity_usd": as_float(summary.get("liquidity_usd")),
        "market_cap": as_float(summary.get("marketCap") or summary.get("market_cap")),
        "fdv": as_float(summary.get("fdv")),
        "dex_url": summary.get("url"),
        "raw": {"dex_summary": summary},
    }


def classify_outcome(return_pct: float | None, liq_now: float | None, liq_start: float | None) -> tuple[str, str]:
    if liq_now is None and return_pct is None:
        return "unknown", "missing_market"
    if liq_now is None or liq_now < 1_000:
        return "dead", "illiquid_or_missing"
    liq_decay = pct(liq_now, liq_start)
    if return_pct is not None and return_pct <= -70:
        return "alive", "severe_drawdown"
    if liq_decay is not None and liq_decay <= -70:
        return "alive", "liquidity_decay"
    if return_pct is not None and return_pct >= 100:
        return "alive", "runner"
    if return_pct is not None and return_pct >= 25:
        return "alive", "positive"
    if return_pct is not None and return_pct <= -25:
        return "alive", "negative"
    return "alive", "flat_or_unresolved"


def get_event_row(con: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM event_outcome_events WHERE event_id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def existing_windows(con: sqlite3.Connection, event_id: str) -> set[str]:
    rows = con.execute("SELECT window_label FROM event_outcomes WHERE event_id=?", (event_id,)).fetchall()
    return {str(r[0]) for r in rows}


def upsert_event_baseline(
    con: sqlite3.Connection,
    event: dict[str, Any],
    *,
    checked_at: datetime,
    baseline_grace_seconds: int,
    fetcher: MarketFetcher,
    dry_run: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    existing = get_event_row(con, str(event["event_id"]))
    if existing is not None:
        return existing, None

    start = event_start(event)
    assert start is not None
    age_seconds = max(0, int((checked_at - start).total_seconds()))
    baseline_status = "valid_baseline" if age_seconds <= baseline_grace_seconds else "late_baseline"
    snap = market_snapshot(event, fetcher)
    row = {
        "event_id": str(event["event_id"]),
        "event_type": str(event["event_type"]),
        "source": event.get("source"),
        "source_key": event.get("source_key"),
        "source_endpoint": event.get("source_endpoint"),
        "chain_id": str(event["chain_id"]),
        "token_address": str(event["token_address"]),
        "pair_address": event.get("pair_address"),
        "observed_at": str(event.get("observed_at") or iso(start)),
        "event_time": event.get("event_time"),
        "outcome_start_at": iso(start),
        "baseline_checked_at": iso(checked_at),
        "baseline_age_seconds": age_seconds,
        "baseline_status": baseline_status,
        "baseline_price_usd": snap["price_usd"],
        "baseline_liquidity_usd": snap["liquidity_usd"],
        "baseline_market_cap": snap["market_cap"],
        "baseline_fdv": snap["fdv"],
        "baseline_dex_url": snap["dex_url"],
        "raw_event_json": json.dumps(event, ensure_ascii=False, sort_keys=True, default=str),
        "created_at_utc": iso(checked_at),
        "updated_at_utc": iso(checked_at),
    }
    if not dry_run:
        cols = list(row.keys())
        con.execute(
            f"INSERT OR IGNORE INTO event_outcome_events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
        con.commit()
        stored = get_event_row(con, str(event["event_id"]))
        if stored is not None:
            row = stored
    return row, {"event_id": row["event_id"], "event_type": row["event_type"], "baseline_status": baseline_status, "baseline_age_seconds": age_seconds}


def previous_returns(con: sqlite3.Connection, event_id: str) -> list[float]:
    rows = con.execute("SELECT return_pct FROM event_outcomes WHERE event_id=? AND return_pct IS NOT NULL", (event_id,)).fetchall()
    return [float(r[0]) for r in rows if r[0] is not None]


def build_outcome_row(event_row: dict[str, Any], window_label: str, checked_at: datetime, snap: dict[str, Any], prior_returns: list[float]) -> dict[str, Any]:
    start = parse_ts(event_row["outcome_start_at"]) or checked_at
    age_seconds = max(0, int((checked_at - start).total_seconds()))
    price_start = as_float(event_row.get("baseline_price_usd"))
    liq_start = as_float(event_row.get("baseline_liquidity_usd"))
    mc_start = as_float(event_row.get("baseline_market_cap"))
    ret = pct(snap.get("price_usd"), price_start)
    liq_chg = pct(snap.get("liquidity_usd"), liq_start)
    mc_chg = pct(snap.get("market_cap"), mc_start)
    alive, status = classify_outcome(ret, snap.get("liquidity_usd"), liq_start)
    return_series = [*prior_returns]
    if ret is not None:
        return_series.append(ret)
    return {
        "event_id": event_row["event_id"],
        "window_label": window_label,
        "checked_at_utc": iso(checked_at),
        "age_seconds": age_seconds,
        "price_usd_now": snap.get("price_usd"),
        "liquidity_usd_now": snap.get("liquidity_usd"),
        "market_cap_now": snap.get("market_cap"),
        "fdv_now": snap.get("fdv"),
        "return_pct": ret,
        "liquidity_change_pct": liq_chg,
        "market_cap_change_pct": mc_chg,
        "max_return_pct": max(return_series) if return_series else None,
        "max_drawdown_pct": min(return_series) if return_series else None,
        "dead_or_alive": alive,
        "outcome_status": status,
        "calibration_valid": 1 if event_row.get("baseline_status") == "valid_baseline" else 0,
        "dex_url": snap.get("dex_url"),
        "raw_json": json.dumps(snap.get("raw") or {}, ensure_ascii=False, sort_keys=True, default=str),
    }


def due_windows(event_row: dict[str, Any], windows: list[tuple[str, timedelta]], checked_at: datetime, done: set[str]) -> list[str]:
    start = parse_ts(event_row["outcome_start_at"])
    if start is None:
        return []
    age = checked_at - start
    return [label for label, delta in windows if age >= delta and label not in done]


def track_event(
    con: sqlite3.Connection,
    event: dict[str, Any],
    *,
    windows: list[tuple[str, timedelta]],
    checked_at: datetime,
    baseline_grace_seconds: int,
    fetcher: MarketFetcher = default_market_fetcher,
    dry_run: bool = False,
) -> dict[str, Any]:
    ok, reason = valid_event(event)
    if not ok:
        return {"ok": False, "skipped": True, "reason": reason}

    event_row, baseline = upsert_event_baseline(
        con,
        event,
        checked_at=checked_at,
        baseline_grace_seconds=baseline_grace_seconds,
        fetcher=fetcher,
        dry_run=dry_run,
    )
    done = existing_windows(con, str(event_row["event_id"])) if not dry_run else set()
    due = due_windows(event_row, windows, checked_at, done)
    outcomes: list[dict[str, Any]] = []
    if due:
        snap = market_snapshot(event, fetcher)
        prior = previous_returns(con, str(event_row["event_id"])) if not dry_run else []
        for label in due:
            row = build_outcome_row(event_row, label, checked_at, snap, prior)
            prior = [*prior, row["return_pct"]] if row["return_pct"] is not None else prior
            outcomes.append(row)
            if not dry_run:
                cols = list(row.keys())
                con.execute(
                    f"INSERT OR IGNORE INTO event_outcomes ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                    [row[c] for c in cols],
                )
        if not dry_run:
            con.commit()
    return {
        "ok": True,
        "event_id": event_row["event_id"],
        "event_type": event_row["event_type"],
        "baseline": baseline,
        "due_windows": due,
        "written_outcomes": [
            {
                "event_id": row["event_id"],
                "window": row["window_label"],
                "return_pct": row["return_pct"],
                "status": row["outcome_status"],
                "calibration_valid": bool(row["calibration_valid"]),
            }
            for row in outcomes
        ],
    }


def track_tape(
    con: sqlite3.Connection,
    tape_path: Path,
    *,
    windows: list[tuple[str, timedelta]],
    checked_at: datetime | None = None,
    baseline_grace_seconds: int = DEFAULT_BASELINE_GRACE_SECONDS,
    limit: int = 200,
    fetcher: MarketFetcher = default_market_fetcher,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_schema(con)
    checked = checked_at or now_utc()
    events, load_errors = load_tape(tape_path)
    selected = events[: max(1, min(limit, 10_000))]
    baselines = 0
    late_baselines = 0
    outcomes = 0
    skipped: list[dict[str, Any]] = []
    written: list[dict[str, Any]] = []
    for event in selected:
        try:
            result = track_event(
                con,
                event,
                windows=windows,
                checked_at=checked,
                baseline_grace_seconds=baseline_grace_seconds,
                fetcher=fetcher,
                dry_run=dry_run,
            )
        except Exception as exc:
            skipped.append({"event_id": event.get("event_id"), "reason": str(exc)[:300]})
            continue
        if not result.get("ok"):
            skipped.append({"event_id": event.get("event_id"), "reason": result.get("reason")})
            continue
        if result.get("baseline"):
            baselines += 1
            if (result.get("baseline") or {}).get("baseline_status") == "late_baseline":
                late_baselines += 1
        row_outcomes = result.get("written_outcomes") or []
        outcomes += len(row_outcomes)
        if result.get("baseline") or row_outcomes:
            written.append(result)
        time.sleep(0.05)
    return {
        "ok": not load_errors,
        "mode": "chaos_event_outcome_tracker_v1",
        "dry_run": dry_run,
        "checked_at": iso(checked),
        "tape": str(tape_path),
        "events_seen": len(events),
        "events_selected": len(selected),
        "baselines_written": baselines,
        "late_baselines": late_baselines,
        "outcomes_written": outcomes,
        "load_errors": load_errors,
        "skipped": skipped,
        "written": written[:50],
        "boundary": BOUNDARY,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Track Chaos Event Tape outcomes. Read-only market repricing only.")
    p.add_argument("--from-tape", default=str(DEFAULT_TAPE), help=f"Event Tape JSONL path (default: {DEFAULT_TAPE})")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"Outcome DB path (default: {DEFAULT_DB})")
    p.add_argument("--windows", default=DEFAULT_WINDOWS, help=f"Comma windows, e.g. 5m,15m,1h (default: {DEFAULT_WINDOWS})")
    p.add_argument("--baseline-grace-seconds", type=int, default=DEFAULT_BASELINE_GRACE_SECONDS)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--raw", action="store_true", help="Emit JSON envelope")
    return p


def render_summary(result: dict[str, Any]) -> str:
    lines = [
        f"☄️ Event outcome tracker · {'dry-run ' if result['dry_run'] else ''}{result['outcomes_written']} outcomes",
        f"events: {result['events_selected']}/{result['events_seen']} · baselines: {result['baselines_written']} · late: {result['late_baselines']}",
    ]
    if result.get("load_errors"):
        lines.append(f"load errors: {len(result['load_errors'])}")
    if result.get("skipped"):
        lines.append(f"skipped: {len(result['skipped'])}")
    for item in (result.get("written") or [])[:12]:
        if item.get("baseline"):
            base = item["baseline"]
            lines.append(f"baseline {item['event_type']} {item['event_id'][:10]} {base['baseline_status']} age={base['baseline_age_seconds']}s")
        for outcome in item.get("written_outcomes") or []:
            lines.append(f"{item['event_type']} {outcome['window']} {item['event_id'][:10]} return={outcome['return_pct']}% {outcome['status']} valid={outcome['calibration_valid']}")
    lines.append("read-only calibration; no execution")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    windows = parse_windows(args.windows)
    db = Path(args.db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    try:
        result = track_tape(
            con,
            Path(args.from_tape).expanduser(),
            windows=windows,
            baseline_grace_seconds=max(0, int(args.baseline_grace_seconds)),
            limit=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        con.close()
    if args.raw:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    else:
        print(render_summary(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
