#!/usr/bin/env python3
"""Bounded elite-wallet ingestion (Solana RPC reads only) and review for Chaos.

This module writes only the smart-wallet event store (any Solana RPC) and local
receipts. It never imports or opens paper, signal, execution, Dex, or X rails.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from smart_wallet_tracker import (  # noqa: E402
    ONCHAIN_SOURCE_SQL,
    classify_tx,
    current_source_id,
    ensure_db as ensure_smart_db,
    fetch_txs,
    insert_event,
    rebuild_positions,
    tx_sig,
    upsert_wallet,
)

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
# The roster ships with the package as a public seed (address + tier only). A copy in
# the profile home wins when present.
_HOME_ROSTER = PROFILE_HOME / "trading" / "config" / "roster.json"
_PACKAGE_ROSTER = SCRIPT_DIR.parents[1] / "seed" / "roster.json"


def _default_roster() -> Path:
    return _HOME_ROSTER if _HOME_ROSTER.is_file() else _PACKAGE_ROSTER


DEFAULT_ROSTER = _default_roster()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
DEFAULT_REPORT_DIR = PROFILE_HOME / "trading" / "reports" / "elite_wallets"
BOUNDARY = "read-only Alpha Elite ingestion/review; smart-wallet event store only; no paper, X, Dex, wallet, signing, orders, routing, swaps, or execution"
BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
VALID_TIERS = {"A", "B", "C"}
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111111",  # native SOL ledger marker
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_address(address: str) -> bool:
    return 32 <= len(address) <= 44 and all(ch in BASE58 for ch in address)


def load_roster(path: Path | None = None, *, lenient_tiers: bool = False) -> dict[str, Any]:
    """Load and validate a roster. An explicit path wins, then the home copy, then the package seed.

    Provenance (version, hash) is derived from the file that was loaded. With lenient_tiers (the read-only
    `chaos wallets --review`), a tier is stripped and upper-cased and one outside A, B, C becomes "?"
    instead of an error; the ingest keeps the strict check.
    """
    if path is None:
        path = _default_roster()
    if not path.is_file():
        raise SystemExit(f"No roster found at {path}. Run `chaos onboard` to create the home with the seed roster, or pass `--roster <path>`.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("elite roster must be a JSON object")
    wallets = payload.get("wallets")
    if not isinstance(wallets, list) or not wallets:
        raise ValueError("elite roster has no wallets list")
    records: list[dict[str, Any]] = []
    addresses: list[str] = []
    for index, row in enumerate(wallets):
        if not isinstance(row, dict):
            raise ValueError(f"elite roster wallet {index} is not an object")
        address = str(row.get("address") or "").strip()
        if not _valid_address(address):
            raise ValueError(f"elite roster wallet {index} has invalid address")
        tier = row.get("tier") or "C"
        if lenient_tiers:
            tier = str(tier).strip().upper()
            tier = tier if tier in VALID_TIERS else "?"
        elif tier not in VALID_TIERS:
            raise ValueError(f"elite roster wallet {index} has tier {tier!r}; tier must be one of A, B, C")
        addresses.append(address)
        records.append({**row, "address": address, "tier": tier})
    if len(set(addresses)) != len(addresses):
        raise ValueError("elite roster contains duplicate addresses")
    return {
        "version": str(payload.get("version") or "unpinned"),
        "data_through": str(payload.get("data_through") or payload.get("captured_at") or "unknown"),
        "wallet_count": len(addresses),
        "wallets": addresses,
        "records": records,
        "source_path": str(path.resolve()),
        "source_sha256": _sha256(path),
    }


def connect_db(path: Path, ensure_func: Callable[[sqlite3.Connection], None] = ensure_smart_db) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    ensure_func(con)
    return con


def event_exists(con: sqlite3.Connection, run_id: str, event: dict[str, Any]) -> bool:
    # Same on-chain event from either fetch path is one event; rebuild_positions reads both.
    row = con.execute(
        f"""
        SELECT 1 FROM wallet_token_events
        WHERE wallet=? AND signature IS ? AND mint IS ? AND event_type=? AND source_id IN ({ONCHAIN_SOURCE_SQL})
          AND (? NOT GLOB 'elite-*' OR run_id GLOB 'elite-*')
        LIMIT 1
        """,
        (event.get("wallet"), event.get("signature"), event.get("mint"), event.get("event_type"), run_id),
    ).fetchone()
    return row is not None


def insert_event_if_new(
    con: sqlite3.Connection,
    run_id: str,
    event: dict[str, Any],
    insert_func: Callable[[sqlite3.Connection, str, dict[str, Any]], None] = insert_event,
) -> bool:
    if event_exists(con, run_id, event):
        return False
    insert_func(con, run_id, event)
    return True


def preserve_transaction(con: sqlite3.Connection, tx: dict[str, Any], *, source_id: str) -> bool:
    """Persist immutable onchain transaction evidence without rewriting prior raw data."""
    signature = tx_sig(tx)
    if not signature:
        return False
    block_time = tx.get("blockTime")
    block_time_utc = None
    if block_time:
        block_time_utc = datetime.fromtimestamp(int(block_time), timezone.utc).isoformat(timespec="seconds")
    meta = tx.get("meta") or {}
    fee_lamports = int(meta.get("fee") or 0)
    err = tx.get("err") or meta.get("err")
    raw = json.dumps(tx, sort_keys=True, ensure_ascii=False, default=str)
    existing = con.execute("SELECT raw_json FROM transactions WHERE signature=?", (signature,)).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO transactions(signature,block_time_utc,fee_lamports,err_json,source_id,raw_json) VALUES(?,?,?,?,?,?)",
            (signature, block_time_utc, fee_lamports, json.dumps(err, sort_keys=True, default=str) if err is not None else None, source_id, raw),
        )
        return True
    prior_raw = existing[0] if not isinstance(existing, sqlite3.Row) else existing["raw_json"]
    if not prior_raw or prior_raw == '{"smart_wallet_tracker": true}':
        con.execute(
            "UPDATE transactions SET block_time_utc=COALESCE(block_time_utc,?),fee_lamports=COALESCE(fee_lamports,?),err_json=COALESCE(err_json,?),raw_json=? WHERE signature=?",
            (block_time_utc, fee_lamports, json.dumps(err, sort_keys=True, default=str) if err is not None else None, raw, signature),
        )
        return True
    return False


def ingest_one_wallet(
    con: sqlite3.Connection,
    wallet: str,
    *,
    cycle_id: str,
    roster: dict[str, Any],
    history_limit: int,
    pages: int,
    run_prefix: str = "elite",
    pipeline_name: str = "alpha_elite_ingest_v1",
    fetch_func: Callable[[str, int, int], list[dict[str, Any]]] = fetch_txs,
    classify_func: Callable[[str, dict[str, Any]], list[dict[str, Any]]] = classify_tx,
) -> dict[str, Any]:
    if not run_prefix or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in run_prefix):
        raise ValueError("run_prefix must contain only lowercase letters, digits, and hyphens")
    run_id = f"{run_prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    source_id = current_source_id()
    insert_func = functools.partial(insert_event, source_id=source_id)
    record = next((row for row in roster.get("records", []) if isinstance(row, dict) and str(row.get("address") or "") == wallet), {})
    notes = json.dumps({
        "pipeline": pipeline_name,
        "cycle_id": cycle_id,
        "wallet": wallet,
        "wallet_tier": record.get("tier"),
        "wallet_trades_per_day": record.get("trades_per_day"),
        "cohort_lane": record.get("lane"),
        "roster_version": roster["version"],
        "roster_sha256": roster["source_sha256"],
    }, sort_keys=True)
    con.execute(
        "INSERT INTO ingestion_runs(run_id,source_id,source_path,source_commit,started_at,status,notes) VALUES(?,?,?,?,?,?,?)",
        (run_id, source_id, roster["source_path"], roster["source_sha256"], now_utc(), "running", notes),
    )
    con.commit()
    try:
        upsert_wallet(con, wallet)
        try:
            txs = fetch_func(wallet, history_limit, pages)
        except SystemExit as exc:
            raise RuntimeError(f"Helius transaction fetch failed: {exc}") from exc
        seen = Counter()
        inserted = Counter()
        raw_transactions_preserved = 0
        duplicate_events = 0
        for tx in txs:
            if preserve_transaction(con, tx, source_id=source_id):
                raw_transactions_preserved += 1
            for event in classify_func(wallet, tx):
                label = str(event.get("event_type") or "unknown")
                seen[label] += 1
                if insert_event_if_new(con, run_id, event, insert_func=insert_func):
                    inserted[label] += 1
                else:
                    duplicate_events += 1
        positions = rebuild_positions(con, wallet)
        counts = {
            "txs_seen": len(txs),
            "raw_transactions_preserved": raw_transactions_preserved,
            "events_seen": sum(seen.values()),
            "events_inserted": sum(inserted.values()),
            "duplicate_events": duplicate_events,
            "event_types_seen": dict(seen),
            "event_types_inserted": dict(inserted),
            "positions_rebuilt": len(positions),
        }
        con.execute(
            "UPDATE ingestion_runs SET completed_at=?,status='completed',row_counts_json=? WHERE run_id=?",
            (now_utc(), json.dumps(counts, sort_keys=True), run_id),
        )
        con.commit()
        return {"wallet": wallet, "ok": True, "run_id": run_id, **counts}
    except Exception as exc:
        con.rollback()
        con.execute(
            "INSERT OR REPLACE INTO ingestion_runs(run_id,source_id,source_path,source_commit,started_at,completed_at,status,notes,row_counts_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, source_id, roster["source_path"], roster["source_sha256"], now_utc(), now_utc(), "failed", notes, json.dumps({"error": str(exc)[:500]}, sort_keys=True)),
        )
        con.commit()
        return {"wallet": wallet, "ok": False, "run_id": run_id, "error": str(exc)[:500]}


def _event_scope_sql(wallet_count: int) -> str:
    return ",".join("?" for _ in range(wallet_count))


def cohort_metrics(con: sqlite3.Connection, wallets: list[str], *, top: int = 10) -> dict[str, Any]:
    if not wallets:
        return {"events": 0, "distinct_mints": 0, "latest_event_utc": None, "event_types": {}, "quote_events": 0, "top_mints": []}
    qmarks = _event_scope_sql(len(wallets))
    params: tuple[Any, ...] = tuple(wallets)
    summary = con.execute(
        f"SELECT COUNT(*) events,COUNT(DISTINCT mint) distinct_mints,MAX(block_time_utc) latest_event_utc FROM wallet_token_events WHERE wallet IN ({qmarks}) AND source_id IN ({ONCHAIN_SOURCE_SQL})",
        params,
    ).fetchone()
    event_types = {
        str(row[0]): int(row[1])
        for row in con.execute(
            f"SELECT event_type,COUNT(*) FROM wallet_token_events WHERE wallet IN ({qmarks}) AND source_id IN ({ONCHAIN_SOURCE_SQL}) GROUP BY event_type ORDER BY COUNT(*) DESC",
            params,
        )
    }
    quote_marks = ",".join("?" for _ in QUOTE_MINTS)
    quote_events = con.execute(
        f"SELECT COUNT(*) FROM wallet_token_events WHERE wallet IN ({qmarks}) AND source_id IN ({ONCHAIN_SOURCE_SQL}) AND mint IN ({quote_marks})",
        (*params, *sorted(QUOTE_MINTS)),
    ).fetchone()[0]
    rows = con.execute(
        f"""
        SELECT e.mint,COALESCE(t.symbol,'') symbol,COUNT(DISTINCT e.wallet) wallets,
               SUM(CASE WHEN e.event_type='buy' THEN 1 ELSE 0 END) buys,
               SUM(CASE WHEN e.event_type='sell' THEN 1 ELSE 0 END) sells,
               SUM(CASE WHEN e.event_type='token_transfer_in' THEN 1 ELSE 0 END) transfer_in,
               SUM(CASE WHEN e.event_type='token_transfer_out' THEN 1 ELSE 0 END) transfer_out,
               MAX(e.block_time_utc) latest_event_utc
        FROM wallet_token_events e LEFT JOIN tokens t ON t.mint=e.mint
        WHERE e.wallet IN ({qmarks}) AND e.source_id IN ({ONCHAIN_SOURCE_SQL})
          AND e.mint IS NOT NULL AND e.mint NOT IN ({quote_marks})
        GROUP BY e.mint,t.symbol
        HAVING buys>0 OR sells>0
        ORDER BY wallets DESC,(buys-sells) DESC,latest_event_utc DESC
        LIMIT ?
        """,
        (*params, *sorted(QUOTE_MINTS), max(1, top)),
    ).fetchall()
    return {
        "events": int(summary["events"] or 0),
        "distinct_mints": int(summary["distinct_mints"] or 0),
        "latest_event_utc": summary["latest_event_utc"],
        "event_types": event_types,
        "quote_events": int(quote_events or 0),
        "top_mints": [dict(row) for row in rows],
    }


def write_receipt(report_dir: Path, receipt: dict[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"elite_ingest_{receipt['cycle_id']}.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str), encoding="utf-8")
    temp.replace(path)
    return path


def ingest_order(con: sqlite3.Connection, wallets: list[str]) -> list[str]:
    """The roster in the order a run reads it: wallets with no completed ingest first, in roster order, then by
    oldest last completed ingest. A run the time bound cuts short is followed by one that starts with the
    wallets it did not reach."""
    last = {row[0]: row[1] for row in con.execute(
        "SELECT json_extract(notes,'$.wallet'), MAX(completed_at) FROM ingestion_runs "
        "WHERE status='completed' AND run_id LIKE 'elite-%' AND json_valid(notes) GROUP BY 1"
    )}
    position = {wallet: index for index, wallet in enumerate(wallets)}
    return sorted(wallets, key=lambda wallet: (wallet in last, last.get(wallet) or "", position[wallet]))


def run_ingest(
    roster: dict[str, Any],
    *,
    db_path: Path = DEFAULT_DB,
    report_dir: Path = DEFAULT_REPORT_DIR,
    history_limit: int = 50,
    pages: int = 1,
    ingest_func: Callable[..., dict[str, Any]] = ingest_one_wallet,
    metrics_func: Callable[..., dict[str, Any]] = cohort_metrics,
    ensure_func: Callable[[sqlite3.Connection], None] = ensure_smart_db,
) -> dict[str, Any]:
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = now_utc()
    con = connect_db(db_path, ensure_func=ensure_func)
    try:
        results = [
            ingest_func(
                con,
                wallet,
                cycle_id=cycle_id,
                roster=roster,
                history_limit=history_limit,
                pages=pages,
            )
            for wallet in ingest_order(con, roster["wallets"])
        ]
        metrics = metrics_func(con, roster["wallets"], top=10)
    finally:
        con.close()
    receipt = {
        "schema": "chaos.alpha_elite_ingest.v1",
        "cycle_id": cycle_id,
        "started_at_utc": started,
        "completed_at_utc": now_utc(),
        "roster": {key: roster[key] for key in ("version", "data_through", "wallet_count", "source_path", "source_sha256")},
        "attempted": len(results),
        "succeeded": sum(1 for row in results if row.get("ok")),
        "failed": sum(1 for row in results if not row.get("ok")),
        "txs_seen": sum(int(row.get("txs_seen") or 0) for row in results),
        "raw_transactions_preserved": sum(int(row.get("raw_transactions_preserved") or 0) for row in results),
        "events_seen": sum(int(row.get("events_seen") or 0) for row in results),
        "events_inserted": sum(int(row.get("events_inserted") or 0) for row in results),
        "duplicates": sum(int(row.get("duplicate_events") or 0) for row in results),
        "metrics": metrics,
        "wallet_results": results,
        "boundary": BOUNDARY,
    }
    receipt_path = write_receipt(report_dir, receipt)
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def latest_receipt(report_dir: Path) -> dict[str, Any] | None:
    paths = sorted(report_dir.glob("elite_ingest_*.json"))
    if not paths:
        return None
    try:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def status(roster: dict[str, Any], *, db_path: Path = DEFAULT_DB, report_dir: Path = DEFAULT_REPORT_DIR) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        metrics = cohort_metrics(con, roster["wallets"], top=5)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    receipt = latest_receipt(report_dir)
    return {
        "ok": integrity == "ok",
        "integrity": integrity,
        "roster": {key: roster[key] for key in ("version", "data_through", "wallet_count", "source_sha256")},
        "last_ingest": None if receipt is None else {key: receipt.get(key) for key in ("cycle_id", "completed_at_utc", "attempted", "succeeded", "failed", "events_inserted")},
        "metrics": metrics,
        "boundary": BOUNDARY,
    }


def compact_card(result: dict[str, Any], command: str) -> str:
    roster = result.get("roster") or {}
    metrics = result.get("metrics") or {}
    if command == "ingest":
        return (
            "☄️ ALPHA ELITE INGEST · " + ("ok" if not result.get("failed") else "partial") + "\n"
            f"Roster {roster.get('version')} · {roster.get('wallet_count')} wallets\n"
            f"Attempted {result.get('attempted')} · succeeded {result.get('succeeded')} · failed {result.get('failed')}\n"
            f"Txs {result.get('txs_seen')} · raw preserved {result.get('raw_transactions_preserved')} · events seen {result.get('events_seen')} · new {result.get('events_inserted')} · duplicates {result.get('duplicates')}\n"
            f"Latest event {metrics.get('latest_event_utc') or 'none'}\n"
            "smart-wallet DB only · no paper/X/Dex/execution"
        )
    last = result.get("last_ingest") or {}
    lines = [
        "☄️ ALPHA ELITE " + command.upper(),
        f"Roster {roster.get('version')} · {roster.get('wallet_count')} wallets · integrity {result.get('integrity', 'unknown')}",
        f"Last ingest {last.get('completed_at_utc') or 'no receipt'} · {last.get('succeeded', 0)}/{last.get('attempted', 0)} succeeded",
        f"Events {metrics.get('events', 0)} · mints {metrics.get('distinct_mints', 0)} · quote/stable events {metrics.get('quote_events', 0)}",
        f"Types {json.dumps(metrics.get('event_types') or {}, sort_keys=True)}",
    ]
    if command == "review":
        for row in (metrics.get("top_mints") or [])[:5]:
            lines.append(f"- {row.get('symbol') or str(row.get('mint'))[:8]} · wallets {row.get('wallets')} · B/S {row.get('buys')}/{row.get('sells')} · T {row.get('transfer_in')}/{row.get('transfer_out')}")
    lines.append("raw transfers preserved; stable/quote excluded from ranking · no paper/execution")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Elite-wallet ingest/status/review (Solana RPC reads only)")
    parser.add_argument("command", choices=("ingest", "status", "review"))
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)

    parser.add_argument("--history-limit", type=int, default=50)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()
    roster = load_roster(args.roster)
    if args.command == "ingest":
        result = run_ingest(roster, db_path=args.db, report_dir=args.report_dir, history_limit=args.history_limit, pages=args.pages)
    else:
        result = status(roster, db_path=args.db, report_dir=args.report_dir)
    if args.raw:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(compact_card(result, args.command))
    return 0 if result.get("ok", result.get("failed", 0) == 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
