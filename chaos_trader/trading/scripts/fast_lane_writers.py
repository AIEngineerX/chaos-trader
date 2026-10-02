#!/usr/bin/env python3
"""The rows the fast lanes read, written by the live sweep for each mint it ranks.

`chaos sweep` ranks DexScreener candidates; for each ranked mint this module writes one `token_signals` row of
type `sweep-rank` and, when the RPC served a holder read, one `token_concentration_snapshots` row. Both are keyed
to a 15-minute bucket: a second sweep in the same bucket updates the rows it wrote instead of adding new ones.
Mints the sweep did not rank get nothing. Writes only the smart-wallet event store; read-only on chain.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from alpha_tape import ELITE_ACTIONABLE_WINDOW_MINUTES
from elite_wallet_pipeline import DEFAULT_DB, connect_db
from holder_resolver import cached_holders, resolve_holders
from smart_wallet_tracker import ONCHAIN_SOURCE_SQL, upsert_token

SWEEP_SOURCE_ID = "chaos_sweep"
SIGNAL_TYPE = "sweep-rank"
BUCKET_SECONDS = 15 * 60
HOLDER_LIMIT = 20


def bucket_start(now: datetime) -> str:
    """Start of the 15-minute UTC bucket that holds `now`."""
    start = int(now.timestamp()) // BUCKET_SECONDS * BUCKET_SECONDS
    return datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds")


def roster_buys(con: sqlite3.Connection, mint: str, now: datetime) -> dict[str, Any]:
    """Roster buys of `mint` in the fast tape's window, by the same rules `chaos sweep --fast` uses."""
    cutoff = (now - timedelta(minutes=ELITE_ACTIONABLE_WINDOW_MINUTES)).isoformat(timespec="seconds")
    row = con.execute(
        f"""
        SELECT COUNT(DISTINCT wallet),
               SUM(CASE WHEN COALESCE(sol_delta, 0) < 0 THEN -sol_delta ELSE COALESCE(amount_sol, 0) END),
               MIN(block_time_utc)
        FROM wallet_token_events
        WHERE mint=? AND event_type='buy'
          AND source_id IN ({ONCHAIN_SOURCE_SQL})
          AND run_id LIKE 'elite-%'
          AND COALESCE(confidence, 'medium') IN ('medium', 'high')
          AND block_time_utc >= ?
        """,
        (mint, cutoff),
    ).fetchone()
    return {"wallet_count": int(row[0] or 0), "total_sol_amount": row[1], "first_buy_utc": row[2]}


def holder_sample(mint: str) -> dict[str, Any]:
    """A top-20 holder sample cached for this mint in the last 15 minutes, else a live read of the top 20."""
    try:
        return cached_holders(mint, HOLDER_LIMIT) or resolve_holders(mint, HOLDER_LIMIT)
    except SystemExit as exc:
        # A failed RPC read past the largest-accounts call; this mint gets no snapshot, the others still do.
        return {"mint": mint, "holder_data": f"unavailable ({str(exc)[:120]})"}


def write_signal(con: sqlite3.Connection, rank: int, candidate: dict[str, Any], *, now: datetime, bucket: str) -> None:
    mint = candidate["mint"]
    summary = candidate.get("summary") or {}
    buys = roster_buys(con, mint, now)
    at = now.isoformat(timespec="seconds")
    metadata = {"bucket": bucket, "rank": rank, "sources": candidate.get("sources") or [], "window_minutes": ELITE_ACTIONABLE_WINDOW_MINUTES}
    con.execute(
        """
        INSERT INTO token_signals(source_id,source_signal_id,mint,signal_type,wallet_count,tg_channel_count,total_sol_amount,
                                  current_market_cap_usd,first_buy_utc,created_at_utc,captured_at_utc,score,metadata_json)
        VALUES(?,?,?,?,?,0,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,source_signal_id) DO UPDATE SET
            wallet_count=excluded.wallet_count, total_sol_amount=excluded.total_sol_amount,
            current_market_cap_usd=excluded.current_market_cap_usd, first_buy_utc=excluded.first_buy_utc,
            created_at_utc=excluded.created_at_utc, captured_at_utc=excluded.captured_at_utc,
            score=excluded.score, metadata_json=excluded.metadata_json
        """,
        (SWEEP_SOURCE_ID, f"{SIGNAL_TYPE}:{mint}:{bucket}", mint, SIGNAL_TYPE, buys["wallet_count"], buys["total_sol_amount"],
         summary.get("marketCap"), buys["first_buy_utc"], at, at, candidate.get("candidate_score"), json.dumps(metadata, sort_keys=True)),
    )


def write_concentration(con: sqlite3.Connection, mint: str, holders: dict[str, Any], *, market_cap: Any, now: datetime, bucket: str) -> bool:
    """One snapshot per mint and bucket, from a served holder read. An unread sample writes nothing: no number is invented."""
    holder_data = str(holders.get("holder_data") or "live")
    if holder_data.startswith("unavailable"):
        return False
    metadata = {
        "bucket": bucket,
        "writer": "sweep",
        "holder_data": holder_data,
        "holder_limit": holders.get("limit"),
        "raw_top_pct": holders.get("raw_top_pct"),
        "lp_pool_pct": holders.get("lp_pool_pct"),
        "program_or_burn_pct": holders.get("program_or_burn_pct"),
        "unknown_pct": holders.get("unknown_pct"),
    }
    values = (holders.get("adjusted_discretionary_pct"), market_cap, now.isoformat(timespec="seconds"), SWEEP_SOURCE_ID, json.dumps(metadata, sort_keys=True))
    existing = con.execute(
        "SELECT id FROM token_concentration_snapshots WHERE mint=? AND json_extract(metadata_json,'$.writer')='sweep' AND json_extract(metadata_json,'$.bucket')=?",
        (mint, bucket),
    ).fetchone()
    if existing:
        con.execute(
            "UPDATE token_concentration_snapshots SET supply_pct=?,market_cap_usd=?,snapshot_at_utc=?,source_id=?,metadata_json=? WHERE id=?",
            (*values, existing[0]),
        )
    else:
        con.execute(
            "INSERT INTO token_concentration_snapshots(supply_pct,market_cap_usd,snapshot_at_utc,source_id,metadata_json,mint) VALUES(?,?,?,?,?,?)",
            (*values, mint),
        )
    return True


def write_ranked(
    ranked: list[dict[str, Any]],
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
    holder_func: Callable[[str], dict[str, Any]] = holder_sample,
) -> dict[str, Any]:
    """Write the fast-lane rows for every ranked candidate; returns counts and each mint's holder-data label."""
    now = now or datetime.now(timezone.utc)
    bucket = bucket_start(now)
    con = connect_db(db_path or DEFAULT_DB)
    holder_data: dict[str, str] = {}
    snapshots = 0
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(source_id,source_type,display_name,status,metadata_json) VALUES(?,?,?,?,?)",
            (SWEEP_SOURCE_ID, "manual", "chaos sweep ranking", "active", json.dumps({"boundary": "read_only"})),
        )
        for rank, candidate in enumerate(ranked, 1):
            mint = candidate["mint"]
            summary = candidate.get("summary") or {}
            upsert_token(con, mint, market_cap=summary.get("marketCap"))
            write_signal(con, rank, candidate, now=now, bucket=bucket)
            holders = holder_func(mint)
            holder_data[mint] = str(holders.get("holder_data") or "live")
            snapshots += write_concentration(con, mint, holders, market_cap=summary.get("marketCap"), now=now, bucket=bucket)
            con.commit()
    finally:
        con.close()
    return {"bucket": bucket, "token_signals": len(ranked), "concentration_snapshots": snapshots, "holder_data": holder_data}
