#!/usr/bin/env python3
"""Prospective, bounded Alpha Elite paper cohort.

Consumes only on-chain buy facts, read over any Solana RPC (Helius adds mint history and the wallet API), from the frozen Alpha Elite roster. It
writes a separate paper database and never mutates ingestion, legacy paper,
signal, wallet, signing, routing, or execution state.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from dexscreener_client import resolve_best_token_market  # noqa: E402
from smart_wallet_tracker import ONCHAIN_SOURCE_SQL  # noqa: E402
from elite_wallet_pipeline import (  # noqa: E402
    DEFAULT_DB as DEFAULT_EVIDENCE_DB,
    DEFAULT_ROSTER,
    QUOTE_MINTS,
    load_roster,
)

from chaos_home import chaos_home, unreadable_db  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "alpha_elite_paper.sqlite"
DEFAULT_REPORT_DIR = PROFILE_HOME / "trading" / "reports" / "alpha_elite_paper"
SOL_MINT = "So11111111111111111111111111111111111111112"
NO_INGEST = "No ingest yet. Run chaos run chaos_alpha_elite_ingest first."
BOUNDARY = "paper/simulated Alpha Elite cohort only; read-only market/onchain inputs; no wallets, signing, orders, routing, swaps, alerts, X, generic radar, legacy paper DB, or execution"
POLICY = {
    "version": "alpha-elite-paper-v1",
    "notional_sol": 1.0,
    "risk_unit_sol": 1.0,
    "baseline_grace_seconds": 45 * 60,
    "min_liquidity_usd": 25_000.0,
    "max_notional_to_liquidity": 0.0025,
    "entry_slippage_bps": 150.0,
    "entry_fee_bps": 50.0,
    "exit_slippage_bps": 150.0,
    "exit_fee_bps": 50.0,
    "mark_late_grace_seconds": 35 * 60,
    "windows": {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600},
    "max_new_episodes_per_cycle": 5,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohort_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes(
  episode_id TEXT PRIMARY KEY,
  mint TEXT UNIQUE NOT NULL,
  symbol TEXT,
  state TEXT NOT NULL,
  gate_reason TEXT NOT NULL,
  source_event_id INTEGER NOT NULL,
  source_wallet TEXT NOT NULL,
  source_signature TEXT,
  source_event_at_utc TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  baseline_age_seconds INTEGER NOT NULL,
  source_confidence TEXT,
  source_event_ids_json TEXT NOT NULL,
  source_wallet_count INTEGER NOT NULL,
  roster_version TEXT NOT NULL,
  roster_sha256 TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  pair_address TEXT,
  dex_id TEXT,
  baseline_token_usd REAL,
  baseline_sol_usd REAL,
  baseline_token_sol REAL,
  baseline_liquidity_usd REAL,
  baseline_market_cap REAL,
  entry_notional_sol REAL,
  entry_cost_sol REAL,
  token_quantity REAL,
  opened_at_utc TEXT,
  closed_at_utc TEXT,
  final_net_pnl_sol REAL,
  final_net_r REAL,
  final_status TEXT,
  raw_baseline_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS marks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id TEXT NOT NULL,
  window_label TEXT NOT NULL,
  horizon_seconds INTEGER NOT NULL,
  checked_at_utc TEXT NOT NULL,
  age_seconds INTEGER NOT NULL,
  lateness_seconds INTEGER NOT NULL,
  calibration_valid INTEGER NOT NULL,
  mark_status TEXT NOT NULL,
  token_price_usd REAL,
  sol_price_usd REAL,
  token_price_sol REAL,
  liquidity_usd REAL,
  market_cap REAL,
  token_usd_return_pct REAL,
  token_vs_sol_return_pct REAL,
  gross_value_sol REAL,
  exit_cost_sol REAL,
  net_value_sol REAL,
  net_pnl_sol REAL,
  net_r REAL,
  raw_market_json TEXT NOT NULL,
  UNIQUE(episode_id, window_label),
  FOREIGN KEY(episode_id) REFERENCES episodes(episode_id)
);
CREATE TABLE IF NOT EXISTS cycle_receipts(
  cycle_id TEXT PRIMARY KEY,
  started_at_utc TEXT NOT NULL,
  completed_at_utc TEXT NOT NULL,
  command TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_elite_paper_episodes_state ON episodes(state, source_event_at_utc);
CREATE INDEX IF NOT EXISTS idx_elite_paper_marks_window ON marks(window_label, calibration_valid);
"""

MarketFetcher = Callable[[str], dict[str, Any]]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def as_float(value: Any) -> float | None:
    try:
        if value in (None, "", [], {}):
            return None
        return float(value)
    except Exception:
        return None


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.commit()
    return con


def connect_evidence(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()  # reads the header and schema
    except sqlite3.DatabaseError as exc:
        con.close()
        raise SystemExit(unreadable_db(path, exc))
    return con


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM cohort_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(con: sqlite3.Connection, key: str, value: Any, at: datetime) -> None:
    con.execute(
        "INSERT INTO cohort_meta(key,value,updated_at_utc) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_utc=excluded.updated_at_utc",
        (key, str(value), iso(at)),
    )


def default_market_fetcher(mint: str) -> dict[str, Any]:
    return resolve_best_token_market(mint, cache=True, ttl_seconds=30)


def market_snapshot(mint: str, fetcher: MarketFetcher) -> dict[str, Any]:
    raw = fetcher(mint)
    return {
        "mint": mint,
        "price_usd": as_float(raw.get("price_usd")),
        "liquidity_usd": as_float(raw.get("liquidity_usd")),
        "market_cap": as_float(raw.get("market_cap")),
        "pair_address": raw.get("best_pair"),
        "dex_id": raw.get("dex_id"),
        "symbol": raw.get("symbol"),
        "raw": raw,
    }


def eligible_buy_rows(evidence: sqlite3.Connection, wallets: list[str], after_id: int, through_id: int) -> list[dict[str, Any]]:
    if not wallets or through_id <= after_id:
        return []
    marks = ",".join("?" for _ in wallets)
    quote_marks = ",".join("?" for _ in QUOTE_MINTS)
    rows = evidence.execute(
        f"""
        SELECT id,wallet,mint,signature,block_time_utc,confidence,token_delta,sol_delta,fee_sol
        FROM wallet_token_events
        WHERE id>? AND id<=? AND wallet IN ({marks})
          AND source_id IN ({ONCHAIN_SOURCE_SQL}) AND event_type='buy'
          AND run_id GLOB 'elite-*'
          AND confidence IN ('medium','high')
          AND mint IS NOT NULL AND mint NOT IN ({quote_marks})
        ORDER BY id ASC
        """,
        (after_id, through_id, *wallets, *sorted(QUOTE_MINTS)),
    ).fetchall()
    return [dict(row) for row in rows]


def cluster_evidence(evidence: sqlite3.Connection, wallets: list[str], mint: str, event_at: datetime, through_id: int) -> tuple[list[int], int]:
    marks = ",".join("?" for _ in wallets)
    start = iso(event_at - timedelta(minutes=15))
    end = iso(event_at + timedelta(minutes=15))
    rows = evidence.execute(
        f"""
        SELECT id,wallet FROM wallet_token_events
        WHERE id<=? AND wallet IN ({marks}) AND source_id IN ({ONCHAIN_SOURCE_SQL})
          AND event_type='buy' AND confidence IN ('medium','high') AND mint=?
          AND run_id GLOB 'elite-*'
          AND block_time_utc>=? AND block_time_utc<=?
        ORDER BY id
        """,
        (through_id, *wallets, mint, start, end),
    ).fetchall()
    return [int(r["id"]) for r in rows], len({str(r["wallet"]) for r in rows})


def gate_baseline(token: dict[str, Any], sol: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    token_price = as_float(token.get("price_usd"))
    sol_price = as_float(sol.get("price_usd"))
    liquidity = as_float(token.get("liquidity_usd"))
    if token_price is None or token_price <= 0 or sol_price is None or sol_price <= 0:
        return False, "missing_price", {}
    if liquidity is None or liquidity < float(policy["min_liquidity_usd"]):
        return False, "liquidity_below_floor", {}
    notional_sol = float(policy["notional_sol"])
    notional_usd = notional_sol * sol_price
    if notional_usd > liquidity * float(policy["max_notional_to_liquidity"]):
        return False, "notional_exceeds_liquidity_cap", {}
    token_sol = token_price / sol_price
    entry_cost = notional_sol * (float(policy["entry_slippage_bps"]) + float(policy["entry_fee_bps"])) / 10_000.0
    spendable = notional_sol - entry_cost
    return True, "paper_entry", {
        "token_price": token_price,
        "sol_price": sol_price,
        "liquidity": liquidity,
        "token_sol": token_sol,
        "entry_cost": entry_cost,
        "token_quantity": spendable / token_sol,
    }


def initialize_cursor(con: sqlite3.Connection, evidence: sqlite3.Connection, at: datetime, from_event_id: int | None) -> tuple[int, bool]:
    current = get_meta(con, "cursor_event_id")
    if current is not None:
        return int(current), False
    max_id = int(evidence.execute("SELECT COALESCE(MAX(id),0) FROM wallet_token_events").fetchone()[0])
    cursor = max_id if from_event_id is None else max(0, min(int(from_event_id), max_id))
    set_meta(con, "cursor_event_id", cursor, at)
    set_meta(con, "cohort_started_at_utc", iso(at), at)
    set_meta(con, "cohort_start_event_id", cursor, at)
    con.commit()
    return cursor, True


def observe(
    con: sqlite3.Connection,
    evidence: sqlite3.Connection,
    roster: dict[str, Any],
    *,
    checked_at: datetime,
    from_event_id: int | None = None,
    limit: int | None = None,
    fetcher: MarketFetcher = default_market_fetcher,
    policy: dict[str, Any] = POLICY,
) -> dict[str, Any]:
    cursor, initialized = initialize_cursor(con, evidence, checked_at, from_event_id)
    through_id = int(evidence.execute("SELECT COALESCE(MAX(id),0) FROM wallet_token_events").fetchone()[0])
    rows = eligible_buy_rows(evidence, roster["wallets"], cursor, through_id)
    cap = max(1, min(int(limit or policy["max_new_episodes_per_cycle"]), 100))
    grouped: dict[str, dict[str, Any]] = {}
    cursor_after = through_id
    for row in rows:
        mint = str(row["mint"])
        if mint in grouped:
            continue
        if len(grouped) >= cap:
            cursor_after = int(row["id"]) - 1
            break
        grouped[mint] = row
    selected = list(grouped.values())
    counts = {"events_scanned": sum(1 for row in rows if int(row["id"]) <= cursor_after), "mints_seen": len(grouped), "opened": 0, "rejected": 0, "duplicate_mints": 0, "late": 0}
    written: list[dict[str, Any]] = []
    sol: dict[str, Any] | None = None
    for row in selected:
        mint = str(row["mint"])
        if con.execute("SELECT 1 FROM episodes WHERE mint=?", (mint,)).fetchone():
            counts["duplicate_mints"] += 1
            continue
        event_at = parse_ts(row.get("block_time_utc"))
        if event_at is None:
            continue
        age = max(0, int((checked_at - event_at).total_seconds()))
        source_ids, wallet_count = cluster_evidence(evidence, roster["wallets"], mint, event_at, through_id)
        state = "REJECTED"
        reason = "late_baseline" if age > int(policy["baseline_grace_seconds"]) else "market_unavailable"
        token: dict[str, Any] = {"raw": {}}
        metrics: dict[str, float] = {}
        if reason == "late_baseline":
            counts["late"] += 1
        else:
            try:
                token = market_snapshot(mint, fetcher)
                if sol is None:
                    sol = market_snapshot(SOL_MINT, fetcher)
                passed, reason, metrics = gate_baseline(token, sol, policy)
                if passed:
                    state = "OPEN"
            except Exception as exc:
                reason = f"market_error:{type(exc).__name__}"
        episode_id = "aep1_" + uuid.uuid4().hex
        opened = iso(checked_at) if state == "OPEN" else None
        raw = {"token": token.get("raw") or {}, "sol": (sol or {}).get("raw") or {}, "source_row": row}
        con.execute(
            """INSERT INTO episodes(
              episode_id,mint,symbol,state,gate_reason,source_event_id,source_wallet,source_signature,
              source_event_at_utc,observed_at_utc,baseline_age_seconds,source_confidence,
              source_event_ids_json,source_wallet_count,roster_version,roster_sha256,policy_version,
              pair_address,dex_id,baseline_token_usd,baseline_sol_usd,baseline_token_sol,
              baseline_liquidity_usd,baseline_market_cap,entry_notional_sol,entry_cost_sol,
              token_quantity,opened_at_utc,raw_baseline_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                episode_id,mint,token.get("symbol"),state,reason,int(row["id"]),row["wallet"],row.get("signature"),
                iso(event_at),iso(checked_at),age,row.get("confidence"),json.dumps(source_ids),wallet_count,
                roster["version"],roster["source_sha256"],policy["version"],token.get("pair_address"),token.get("dex_id"),
                metrics.get("token_price"),metrics.get("sol_price"),metrics.get("token_sol"),metrics.get("liquidity"),
                token.get("market_cap"),float(policy["notional_sol"]) if state == "OPEN" else None,
                metrics.get("entry_cost"),metrics.get("token_quantity"),opened,json.dumps(raw,sort_keys=True,default=str),
            ),
        )
        counts["opened" if state == "OPEN" else "rejected"] += 1
        written.append({"episode_id": episode_id, "mint": mint, "state": state, "reason": reason, "wallets": wallet_count, "baseline_age_seconds": age})
    set_meta(con, "cursor_event_id", cursor_after, checked_at)
    con.commit()
    return {"initialized": initialized, "cursor_before": cursor, "cursor_after": cursor_after, **counts, "episodes": written}


def mark_one(episode: dict[str, Any], label: str, horizon: int, checked_at: datetime, token: dict[str, Any], sol: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    started = parse_ts(episode.get("opened_at_utc")) or parse_ts(episode["source_event_at_utc"]) or checked_at
    age = max(0, int((checked_at - started).total_seconds()))
    lateness = max(0, age - horizon)
    token_usd = as_float(token.get("price_usd"))
    sol_usd = as_float(sol.get("price_usd"))
    liquidity = as_float(token.get("liquidity_usd"))
    baseline_token_usd = as_float(episode.get("baseline_token_usd"))
    baseline_token_sol = as_float(episode.get("baseline_token_sol"))
    qty = as_float(episode.get("token_quantity"))
    status = "ok"
    if token_usd is None or sol_usd is None or token_usd <= 0 or sol_usd <= 0:
        status = "missing_market"
    elif liquidity is None or liquidity < 1_000:
        status = "illiquid"
    token_sol = token_usd / sol_usd if token_usd is not None and sol_usd is not None and token_usd > 0 and sol_usd > 0 else None
    usd_return = ((token_usd / baseline_token_usd) - 1.0) * 100.0 if token_usd is not None and baseline_token_usd is not None and baseline_token_usd > 0 else None
    sol_return = ((token_sol / baseline_token_sol) - 1.0) * 100.0 if token_sol is not None and baseline_token_sol is not None and baseline_token_sol > 0 else None
    gross = qty * token_sol if qty is not None and token_sol is not None else None
    exit_cost = gross * (float(policy["exit_slippage_bps"]) + float(policy["exit_fee_bps"])) / 10_000.0 if gross is not None else None
    net_value = gross - exit_cost if gross is not None and exit_cost is not None else None
    net_pnl = net_value - float(policy["notional_sol"]) if net_value is not None else None
    net_r = net_pnl / float(policy["risk_unit_sol"]) if net_pnl is not None else None
    valid = int(status == "ok" and lateness <= int(policy["mark_late_grace_seconds"]))
    if status == "ok" and not valid:
        status = "late_mark"
    return {
        "episode_id": episode["episode_id"], "window_label": label, "horizon_seconds": horizon,
        "checked_at_utc": iso(checked_at), "age_seconds": age, "lateness_seconds": lateness,
        "calibration_valid": valid, "mark_status": status, "token_price_usd": token_usd,
        "sol_price_usd": sol_usd, "token_price_sol": token_sol, "liquidity_usd": liquidity,
        "market_cap": token.get("market_cap"), "token_usd_return_pct": usd_return,
        "token_vs_sol_return_pct": sol_return, "gross_value_sol": gross, "exit_cost_sol": exit_cost,
        "net_value_sol": net_value, "net_pnl_sol": net_pnl, "net_r": net_r,
        "raw_market_json": json.dumps({"token": token.get("raw") or {}, "sol": sol.get("raw") or {}},sort_keys=True,default=str),
    }


def collect_marks(
    con: sqlite3.Connection,
    *,
    checked_at: datetime,
    fetcher: MarketFetcher = default_market_fetcher,
    policy: dict[str, Any] = POLICY,
    limit: int = 200,
) -> dict[str, Any]:
    episodes = [dict(r) for r in con.execute("SELECT * FROM episodes WHERE state='OPEN' ORDER BY source_event_at_utc")]
    due_episodes: list[tuple[datetime, dict[str, Any], list[tuple[str, int]]]] = []
    for episode in episodes:
        started = parse_ts(episode.get("opened_at_utc")) or parse_ts(episode["source_event_at_utc"])
        if started is None:
            continue
        done = {str(r[0]) for r in con.execute("SELECT window_label FROM marks WHERE episode_id=?", (episode["episode_id"],))}
        due = [
            (label, int(seconds))
            for label, seconds in policy["windows"].items()
            if checked_at - started >= timedelta(seconds=int(seconds)) and label not in done
        ]
        if due:
            first_deadline = min(started + timedelta(seconds=horizon) for _label, horizon in due)
            due_episodes.append((first_deadline, episode, due))
    due_episodes.sort(key=lambda item: (item[0], str(item[1]["episode_id"])))
    network_call_limit = max(0, min(limit, 200))
    token_episode_limit = max(0, network_call_limit - 1)
    selected = due_episodes[:token_episode_limit]
    counts = {
        "open_seen": len(episodes),
        "due_seen": len(due_episodes),
        "due_selected": len(selected),
        "due_deferred_limit": len(due_episodes) - len(selected),
        "network_call_limit": network_call_limit,
        "network_calls_planned": len(selected) + (1 if selected else 0),
        "marks_written": 0,
        "marks_deferred": 0,
        "valid_marks": 0,
        "late_marks": 0,
        "closed": 0,
        "market_errors": 0,
    }
    written: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    sol: dict[str, Any] | None = None
    for _deadline, episode, due in selected:
        token: dict[str, Any] = {"raw": {}}
        fetch_error: str | None = None
        try:
            token = market_snapshot(str(episode["mint"]), fetcher)
            if sol is None:
                sol = market_snapshot(SOL_MINT, fetcher)
        except Exception as exc:
            counts["market_errors"] += 1
            fetch_error = type(exc).__name__
        for label, horizon in due:
            row = mark_one(episode, label, horizon, checked_at, token, sol or {"raw": {}}, policy)
            if row["mark_status"] == "missing_market" and row["lateness_seconds"] <= int(policy["mark_late_grace_seconds"]):
                counts["marks_deferred"] += 1
                deferred.append({"episode_id": episode["episode_id"], "mint": episode["mint"], "window": label, "reason": fetch_error or "missing_market"})
                continue
            cols = list(row)
            con.execute(f"INSERT OR IGNORE INTO marks({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", [row[c] for c in cols])
            counts["marks_written"] += 1
            counts["valid_marks" if row["calibration_valid"] else "late_marks"] += 1
            written.append({"episode_id": episode["episode_id"], "mint": episode["mint"], "window": label, "status": row["mark_status"], "net_pnl_sol": row["net_pnl_sol"], "net_r": row["net_r"]})
            if label == "24h":
                con.execute(
                    "UPDATE episodes SET state='CLOSED',closed_at_utc=?,final_net_pnl_sol=?,final_net_r=?,final_status=? WHERE episode_id=?",
                    (iso(checked_at), row["net_pnl_sol"], row["net_r"], row["mark_status"], episode["episode_id"]),
                )
                counts["closed"] += 1
    con.commit()
    return {**counts, "marks": written, "deferred": deferred}


def status(con: sqlite3.Connection) -> dict[str, Any]:
    integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
    states={str(r[0]):int(r[1]) for r in con.execute("SELECT state,COUNT(*) FROM episodes GROUP BY state")}
    marks={str(r[0]):int(r[1]) for r in con.execute("SELECT window_label,COUNT(*) FROM marks GROUP BY window_label")}
    valid={str(r[0]):int(r[1]) for r in con.execute("SELECT window_label,COUNT(*) FROM marks WHERE calibration_valid=1 GROUP BY window_label")}
    pnl=con.execute("SELECT COUNT(*),COALESCE(SUM(final_net_pnl_sol),0),COALESCE(SUM(final_net_r),0) FROM episodes WHERE state='CLOSED' AND final_status='ok'").fetchone()
    latest=con.execute("SELECT MAX(checked_at_utc) FROM marks").fetchone()[0]
    db_row = con.execute("PRAGMA database_list").fetchone()
    db_path = str(db_row["file"] or DEFAULT_DB) if isinstance(db_row, sqlite3.Row) else str(db_row[2] or DEFAULT_DB)
    return {
        "ok":integrity=="ok","integrity":integrity,"mode":"paper/simulated","db":db_path,
        "policy":POLICY,"cursor_event_id":get_meta(con,"cursor_event_id"),"cohort_start_event_id":get_meta(con,"cohort_start_event_id"),
        "episode_states":states,"marks":marks,"valid_marks":valid,"valid_closed":int(pnl[0]),
        "realized_pnl_sol":float(pnl[1]),"realized_r":float(pnl[2]),"latest_mark_utc":latest,"boundary":BOUNDARY,
    }


def save_receipt(con: sqlite3.Connection, report_dir: Path, command: str, started: datetime, payload: dict[str, Any]) -> str:
    cycle_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:6]
    completed=now_utc()
    receipt={"schema":"chaos.alpha_elite_paper.v1","cycle_id":cycle_id,"started_at_utc":iso(started),"completed_at_utc":iso(completed),"command":command,"payload":payload,"boundary":BOUNDARY}
    con.execute("INSERT INTO cycle_receipts VALUES(?,?,?,?,?)",(cycle_id,iso(started),iso(completed),command,json.dumps(receipt,sort_keys=True,default=str)))
    con.commit()
    report_dir.mkdir(parents=True,exist_ok=True)
    path=report_dir/f"elite_paper_{cycle_id}.json"
    tmp=path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipt,indent=2,sort_keys=True,default=str),encoding="utf-8")
    tmp.replace(path)
    return str(path)


def run_cycle(con: sqlite3.Connection,evidence: sqlite3.Connection,roster: dict[str,Any],*,checked_at:datetime,from_event_id:int|None,limit:int,fetcher:MarketFetcher=default_market_fetcher)->dict[str,Any]:
    observations=observe(con,evidence,roster,checked_at=checked_at,from_event_id=from_event_id,limit=limit,fetcher=fetcher)
    # Observation throughput and mark coverage are separate controls. Reusing the
    # small new-episode cap here starves newer opens behind older not-yet-due rows.
    marks=collect_marks(con,checked_at=checked_at,fetcher=fetcher)
    return {"ok":marks["market_errors"] == 0 and marks["marks_deferred"] == 0 and marks["due_deferred_limit"] == 0,"mode":"paper/simulated","observations":observations,"marking":marks,"status":status(con),"boundary":BOUNDARY}


def compact(result: dict[str,Any],command:str)->str:
    s=result if command=="status" else result.get("status") or {}
    states=s.get("episode_states") or {}
    lines=["☄️ ALPHA ELITE PAPER · "+command.upper(),f"Mode paper/simulated · integrity {s.get('integrity','unknown')}",f"Episodes open {states.get('OPEN',0)} · closed {states.get('CLOSED',0)} · rejected {states.get('REJECTED',0)}",f"Valid closed {s.get('valid_closed',0)} · net {s.get('realized_pnl_sol',0):.6f} SOL · {s.get('realized_r',0):.6f}R"]
    if command!="status":
        o=result.get("observations") or {}; m=result.get("marking") or {}
        lines.append(f"Observed events {o.get('events_scanned',0)} · opened {o.get('opened',0)} · rejected {o.get('rejected',0)}")
        lines.append(f"Marks {m.get('marks_written',0)} · valid {m.get('valid_marks',0)} · late {m.get('late_marks',0)} · market deferred {m.get('marks_deferred',0)} · capacity deferred {m.get('due_deferred_limit',0)} · errors {m.get('market_errors',0)}")
    lines.append("1 SOL paper notional · SOL benchmark · no execution")
    return "\n".join(lines)


def main() -> int:
    ap=argparse.ArgumentParser(description="Prospective Alpha Elite paper cohort")
    ap.add_argument("command",choices=("cycle","observe","mark","status"))
    ap.add_argument("--db",type=Path,default=DEFAULT_DB)
    ap.add_argument("--evidence-db",type=Path,default=DEFAULT_EVIDENCE_DB)
    ap.add_argument("--roster",type=Path,default=DEFAULT_ROSTER)
    ap.add_argument("--report-dir",type=Path,default=DEFAULT_REPORT_DIR)

    ap.add_argument("--from-event-id",type=int)
    ap.add_argument("--limit",type=int,default=int(POLICY["max_new_episodes_per_cycle"]))
    ap.add_argument("--raw",action="store_true")
    args=ap.parse_args()
    if args.command in {"cycle","observe"} and not args.evidence_db.exists():
        print(NO_INGEST)
        return 0
    started=now_utc(); con=connect(args.db)
    evidence=None
    try:
        if args.command in {"cycle","observe"}:
            roster=load_roster(args.roster)
            evidence=connect_evidence(args.evidence_db)
            if args.command=="cycle": result=run_cycle(con,evidence,roster,checked_at=started,from_event_id=args.from_event_id,limit=args.limit)
            else:
                obs=observe(con,evidence,roster,checked_at=started,from_event_id=args.from_event_id,limit=args.limit)
                result={"ok":True,"mode":"paper/simulated","observations":obs,"status":status(con),"boundary":BOUNDARY}
        elif args.command=="mark":
            marks=collect_marks(con,checked_at=started)
            result={"ok":marks["market_errors"] == 0 and marks["marks_deferred"] == 0 and marks["due_deferred_limit"] == 0,"mode":"paper/simulated","marking":marks,"status":status(con),"boundary":BOUNDARY}
        else: result=status(con)
        if args.command!="status": result["receipt_path"]=save_receipt(con,args.report_dir,args.command,started,result)
    finally:
        if evidence is not None: evidence.close()
        con.close()
    print(json.dumps(result,indent=2,sort_keys=True,default=str) if args.raw else compact(result,args.command))
    return 0 if result.get("ok") else 2


if __name__=="__main__":
    raise SystemExit(main())
