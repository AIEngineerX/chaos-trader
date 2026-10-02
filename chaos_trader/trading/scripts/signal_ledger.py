#!/usr/bin/env python3
"""Chaos read-only signal ledger.

Stores token/sweep research signals so later outcome trackers can measure whether
verdicts were useful. No execution, no signing, no alerts.
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "signal_ledger.sqlite"
OWNER_WALLETS_PATH = PROFILE_HOME / "trading" / "watchlists" / "owner_wallets.json"
BOUNDARY = "read-only research ledger; no execution"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT UNIQUE NOT NULL,
    timestamp_utc TEXT NOT NULL,
    source_command TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    verdict TEXT,
    signal_kind TEXT,
    entry_action TEXT,
    structural_gate TEXT,
    legacy_verdict TEXT,
    position_action TEXT,
    catalyst_type TEXT,
    flow_conversion_status TEXT,
    fake_flow_severity TEXT,
    attention_phase TEXT,
    score REAL,
    fact_grade TEXT,
    source_coverage_json TEXT NOT NULL DEFAULT '{}',
    price_usd_at_scan REAL,
    liquidity_usd REAL,
    market_cap REAL,
    fdv REAL,
    volume_liquidity_ratio REAL,
    avg_tx_usd REAL,
    tx_count INTEGER,
    holder_adjusted_pct REAL,
    watch_wallet_hits INTEGER,
    x_citation_count INTEGER,
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    json_artifact_path TEXT,
    markdown_artifact_path TEXT,
    raw_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_mint_time ON signals(mint, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_signals_verdict_time ON signals(verdict, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_signals_source_time ON signals(source_command, timestamp_utc);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_float(value: Any) -> float | None:
    try:
        if value in (None, "", [], {}):
            return None
        return float(value)
    except Exception:
        return None


def as_int(value: Any) -> int | None:
    try:
        if value in (None, "", [], {}):
            return None
        return int(float(value))
    except Exception:
        return None


def x_citation_count(x: Any) -> int:
    if not isinstance(x, dict):
        return 0
    return len(x.get("citations") or []) + len(x.get("inline_citations") or [])


def source_coverage(result: dict[str, Any]) -> dict[str, bool]:
    dex = result.get("dex") or {}
    market = result.get("market") or {}
    token_scan = result.get("token_scan")
    pump = result.get("pumpfun") or {}
    wallet = result.get("wallet_timing") or {}
    x = result.get("x_attention")
    return {
        "dex_market": bool(market.get("price_usd") or market.get("liquidity_usd") or dex.get("ok")),
        "helius_token_scan": isinstance(token_scan, dict) and bool(token_scan.get("ok")),
        "holder_resolution": bool(((token_scan or {}).get("holder_resolution") if isinstance(token_scan, dict) else None) or pump.get("holder_resolution")),
        "pump_or_raydium_sample": bool(pump.get("pumpfun_activity_visible") or pump.get("raydium_activity_visible")),
        "watch_wallet_validation": int(wallet.get("watch_wallet_hit_count") or 0) > 0,
        "x_attention": isinstance(x, dict) and bool(x.get("success")),
        "social_catalyst": str((result.get("social_catalyst") or {}).get("catalyst_type") or "none") != "none",
        "position_context": bool((result.get("position_context") or {}).get("owner_exposed")),
        "flow_conversion": bool((result.get("flow_conversion") or {}).get("conversion_status")),
    }


def fact_grade(result: dict[str, Any]) -> str:
    cov = source_coverage(result)
    if cov["dex_market"] and cov["helius_token_scan"] and cov["holder_resolution"] and cov["pump_or_raydium_sample"] and (cov["watch_wallet_validation"] or cov["x_attention"]):
        return "A"
    if cov["dex_market"] and cov["helius_token_scan"] and cov["holder_resolution"] and cov["pump_or_raydium_sample"]:
        return "B"
    if cov["dex_market"] and (cov["helius_token_scan"] or cov["holder_resolution"] or cov["pump_or_raydium_sample"]):
        return "C"
    return "D"


def mask_wallet(value: Any) -> str:
    text = str(value or "")
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


def private_owner_wallets() -> set[str]:
    try:
        data = json.loads(OWNER_WALLETS_PATH.read_text()) if OWNER_WALLETS_PATH.exists() else {}
    except Exception:
        return set()
    rows = data.get("wallets") if isinstance(data, dict) else None
    if isinstance(rows, dict):
        return {str(k) for k in rows.keys()}
    if isinstance(rows, list):
        return {str(r.get("wallet") or r.get("address")) for r in rows if isinstance(r, dict) and (r.get("wallet") or r.get("address"))}
    return set()


def sanitized_raw_result(result: dict[str, Any]) -> dict[str, Any]:
    """Redact exact owner wallet identifiers before durable ledger storage."""
    try:
        clean = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    except Exception:
        clean = dict(result)
    owner = clean.get("owner_exposure") if isinstance(clean, dict) else None
    owner_wallets = private_owner_wallets()
    if isinstance(owner, dict):
        owner_wallets.update(str(item.get("wallet")) for key in ("owner_wallet_hits", "errors") for item in (owner.get(key) or []) if isinstance(item, dict) and item.get("wallet"))
        for key in ("owner_wallet_hits", "errors"):
            for item in owner.get(key) or []:
                if isinstance(item, dict) and item.get("wallet"):
                    item["wallet"] = mask_wallet(item.get("wallet"))
        if owner.get("owner_wallet_file"):
            owner["owner_wallet_file"] = "[redacted-owner-wallet-file]"
    timing = clean.get("wallet_timing") if isinstance(clean, dict) else None
    if isinstance(timing, dict):
        for item in timing.get("wallet_timing") or []:
            if isinstance(item, dict) and item.get("wallet") and (item.get("private_owner_context") or str(item.get("wallet")) in owner_wallets):
                item["wallet"] = mask_wallet(item.get("wallet"))
        for key in ("watch_wallet_hits", "quality_wallet_hits", "scout_wallet_hits"):
            for item in timing.get(key) or []:
                if isinstance(item, dict) and str(item.get("wallet") or "") in owner_wallets:
                    item["wallet"] = mask_wallet(item.get("wallet"))
        for item in timing.get("top_holders_sample") or []:
            if isinstance(item, dict) and str(item.get("owner") or "") in owner_wallets:
                item["owner"] = mask_wallet(item.get("owner"))
        for item in timing.get("top_signers_sample") or []:
            if isinstance(item, dict) and str(item.get("wallet") or "") in owner_wallets:
                item["wallet"] = mask_wallet(item.get("wallet"))
    return clean


def holder_adjusted_pct(result: dict[str, Any]) -> float | None:
    token_scan = result.get("token_scan") if isinstance(result.get("token_scan"), dict) else {}
    pump = result.get("pumpfun") if isinstance(result.get("pumpfun"), dict) else {}
    for container in (token_scan, pump):
        hr = (container or {}).get("holder_resolution")
        if isinstance(hr, dict):
            value = as_float(hr.get("adjusted_discretionary_pct"))
            if value is not None:
                return value
    return None


def normalize_signal(result: dict[str, Any], *, source_command: str) -> dict[str, Any]:
    market = result.get("market") or {}
    cls = result.get("classification") or {}
    flow = cls.get("flow") or {}
    wallet = result.get("wallet_timing") or {}
    generated = str(result.get("generated_at") or now_utc())
    mint = str(result.get("mint") or "").strip()
    cov = source_coverage(result)
    grade = str(result.get("fact_grade") or fact_grade(result))
    risk_flags = cls.get("risk_flags") or result.get("risk_flags") or []
    if not isinstance(risk_flags, list):
        risk_flags = [str(risk_flags)]
    signal_id = f"{source_command}:{mint}:{generated}:{result.get('json_path') or result.get('markdown_path') or ''}"
    gate = result.get("gate") or {}
    entry_gate = result.get("entry_gate") or {}
    position = result.get("position_context") or {}
    catalyst = result.get("social_catalyst") or {}
    flow_conversion = result.get("flow_conversion") or {}
    entry_action = entry_gate.get("action") or gate.get("gate") or cls.get("verdict") or result.get("verdict")
    position_action = position.get("position_action")
    signal_kind = "owner_position_read" if position.get("owner_exposed") else ("sweep_candidate" if source_command.startswith("sweep") else "entry_read")
    signal_verdict = position_action if signal_kind == "owner_position_read" and position_action not in {None, "", "no-position"} else entry_action
    return {
        "signal_id": signal_id,
        "timestamp_utc": generated,
        "source_command": source_command,
        "mint": mint,
        "symbol": market.get("symbol"),
        "verdict": signal_verdict,
        "signal_kind": signal_kind,
        "entry_action": entry_action,
        "structural_gate": gate.get("gate"),
        "legacy_verdict": cls.get("verdict") or result.get("verdict"),
        "position_action": position_action,
        "catalyst_type": catalyst.get("catalyst_type"),
        "flow_conversion_status": flow_conversion.get("conversion_status"),
        "fake_flow_severity": flow_conversion.get("fake_flow_severity"),
        "attention_phase": cls.get("attention_phase") or (result.get("mode_context") or {}).get("mode"),
        "score": as_float(cls.get("score") if cls else result.get("candidate_score")),
        "fact_grade": grade,
        "source_coverage_json": json.dumps(cov, sort_keys=True),
        "price_usd_at_scan": as_float(market.get("price_usd") or market.get("priceUsd")),
        "liquidity_usd": as_float(market.get("liquidity_usd")),
        "market_cap": as_float(market.get("market_cap") or market.get("marketCap")),
        "fdv": as_float(market.get("fdv")),
        "volume_liquidity_ratio": as_float(flow.get("volume_liquidity_ratio")),
        "avg_tx_usd": as_float(flow.get("avg_tx_usd")),
        "tx_count": as_int(flow.get("tx_count")),
        "holder_adjusted_pct": holder_adjusted_pct(result),
        "watch_wallet_hits": as_int(wallet.get("watch_wallet_hit_count")) or 0,
        "x_citation_count": x_citation_count(result.get("x_attention")),
        "risk_flags_json": json.dumps(risk_flags, ensure_ascii=False),
        "json_artifact_path": result.get("json_path"),
        "markdown_artifact_path": result.get("markdown_path"),
        "raw_json": json.dumps(sanitized_raw_result(result), ensure_ascii=False, sort_keys=True, default=str),
        "created_at_utc": now_utc(),
    }


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    existing = {row[1] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
    for name in (
        "signal_kind",
        "entry_action",
        "structural_gate",
        "legacy_verdict",
        "position_action",
        "catalyst_type",
        "flow_conversion_status",
        "fake_flow_severity",
    ):
        if name not in existing:
            con.execute(f"ALTER TABLE signals ADD COLUMN {name} TEXT")
    con.commit()
    return con


def record_signal(result: dict[str, Any], *, source_command: str, db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    row = normalize_signal(result, source_command=source_command)
    if not row["mint"]:
        raise ValueError("signal missing mint")
    con = connect(db_path)
    try:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        update_cols = [c for c in cols if c != "signal_id"]
        updates = ",".join(f"{c}=excluded.{c}" for c in update_cols)
        con.execute(
            f"INSERT INTO signals ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(signal_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )
        con.commit()
        db_id = con.execute("SELECT id FROM signals WHERE signal_id=?", (row["signal_id"],)).fetchone()["id"]
        return {"ok": True, "db_path": str(db_path), "id": db_id, "signal_id": row["signal_id"], "fact_grade": row["fact_grade"]}
    finally:
        con.close()


def recent(limit: int = 20, db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT id,timestamp_utc,source_command,mint,symbol,verdict,attention_phase,score,fact_grade,liquidity_usd,market_cap,volume_liquidity_ratio,watch_wallet_hits,x_citation_count FROM signals ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Inspect Chaos read-only signal ledger")
    p.add_argument("recent", nargs="?", default="recent")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()
    rows = recent(args.limit)
    if args.raw:
        print(json.dumps({"ok": True, "db_path": str(DEFAULT_DB), "rows": rows}, indent=2, ensure_ascii=False))
        return
    print(f"☄️ Signal ledger · {len(rows)} recent · {DEFAULT_DB}")
    for r in rows:
        print(f"#{r['id']} {r['timestamp_utc']} {r['source_command']} {r.get('symbol') or 'UNKNOWN'} {r['mint'][:6]}…{r['mint'][-4:]} {r.get('verdict')} score={r.get('score')} fact={r.get('fact_grade')}")
    print(BOUNDARY)


if __name__ == "__main__":
    main()
