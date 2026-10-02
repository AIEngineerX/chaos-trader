#!/usr/bin/env python3
"""Fast local Chaos alpha tape.

This is the read-only fast lane for CLI and chat callers. It prefers local
SQLite/materialized artifacts and optional short-TTL Dex context over live deep
analysis. No wallet connection, signing, posting, webhooks, alerts, or trading.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home, stop_if_corrupt  # noqa: E402
from smart_wallet_tracker import ONCHAIN_SOURCE_SQL  # noqa: E402
PROFILE_HOME = chaos_home()
DB_PATH = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
SIGNAL_LEDGER_PATH = PROFILE_HOME / "trading" / "db" / "signal_ledger.sqlite"
SECONDARY_SUMMARY_PATH = PROFILE_HOME / "trading" / "alpha" / "secondary" / "mint_score_summary.json"
SECONDARY_SCORE_DIR = PROFILE_HOME / "trading" / "alpha" / "mint_scores"
BOUNDARY = "Advisory + paper only. No wallet, signing, routing, or live execution."
ELITE_ACTIONABLE_WINDOW_MINUTES = 45
ELITE_MAX_MINTS_PER_WALLET = 3
ELITE_EVENT_MAX_STALE_SECONDS = 45 * 60  # ingest cron runs every 30m; must exceed one full cadence
EXCLUDED_SWEEP_MINTS = {
    "So11111111111111111111111111111111111111111",  # native SOL
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(value: Any) -> str:
    try:
        f = float(value)
    except Exception:
        return "?"
    if f >= 1_000_000:
        return f"${f/1_000_000:.2f}M"
    if f >= 1_000:
        return f"${f/1_000:.1f}K"
    return f"${f:.0f}"


def short_ca(mint: str) -> str:
    return f"{mint[:6]}…{mint[-4:]}" if len(mint) > 12 else mint


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", [], {}):
            return default
        return int(float(value))
    except Exception:
        return default


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    quoted = urllib.parse.quote(str(path.resolve()), safe="/:")
    con = None
    try:
        con = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1")
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
    # A mode=ro open cannot create the missing -shm/-wal sidecars of a WAL
    # database; fall back to a normal open locked to reads via query_only.
    con = None
    try:
        con = sqlite3.connect(path, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("SELECT 1")
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
        return None


def stop_if_corrupt_tape() -> None:
    """The payloads read a missing, busy, or corrupt wallet database alike as unavailable. A caller that
    runs unattended calls this when the tape is unavailable: a corrupt file stops it with one line, so a
    tape that can never fill does not pass for an empty one. Missing or busy stays the caller's to handle."""
    stop_if_corrupt(DB_PATH)


def table_names(con: sqlite3.Connection | None) -> set[str]:
    if con is None:
        return set()
    return {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def require_tables(con: sqlite3.Connection | None, names: set[str]) -> list[str]:
    if con is None:
        return ["smart_wallets.sqlite missing or unreadable"]
    try:
        existing = table_names(con)
    except sqlite3.Error as exc:
        return [f"smart_wallets.sqlite unreadable: {exc}"]
    return [f"missing table: {name}" for name in sorted(names - existing)]


def parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)([+-]\d{2}:\d{2})?$", text)
    if match:
        base, frac, tz = match.groups()
        text = f"{base}.{(frac + '000000')[:6]}{tz or ''}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(value: Any) -> int | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def age_label(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def max_ts(con: sqlite3.Connection | None, table: str, column: str) -> str | None:
    if con is None:
        return None
    try:
        row = con.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def tape_freshness(con: sqlite3.Connection | None, *, token_signal_max_stale_seconds: int = 15 * 60, wallet_event_max_stale_seconds: int = 15 * 60, concentration_max_stale_seconds: int = 60 * 60) -> dict[str, Any]:
    latest = {
        "token_signals": max_ts(con, "token_signals", "COALESCE(created_at_utc, first_buy_utc, captured_at_utc)"),
        "wallet_events": max_ts(con, "wallet_token_events", "block_time_utc"),
        "concentration": max_ts(con, "token_concentration_snapshots", "snapshot_at_utc"),
    }
    ages = {k: age_seconds(v) for k, v in latest.items()}
    stale_reasons: list[str] = []
    if ages["wallet_events"] is None or ages["wallet_events"] > wallet_event_max_stale_seconds:
        stale_reasons.append(f"wallet events stale: {age_label(ages['wallet_events'])}")
    if ages["token_signals"] is None or ages["token_signals"] > token_signal_max_stale_seconds:
        stale_reasons.append(f"token signals stale: {age_label(ages['token_signals'])}")
    if ages["concentration"] is None or ages["concentration"] > concentration_max_stale_seconds:
        stale_reasons.append(f"concentration stale: {age_label(ages['concentration'])}")
    return {
        "ok": not stale_reasons,
        "status": "fresh" if not stale_reasons else "stale",
        "latest": latest,
        "age_seconds": ages,
        "age_labels": {k: age_label(v) for k, v in ages.items()},
        "stale_reasons": stale_reasons,
        "threshold_seconds": {
            "token_signals": token_signal_max_stale_seconds,
            "wallet_events": wallet_event_max_stale_seconds,
            "concentration": concentration_max_stale_seconds,
        },
    }


def qone(con: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    if con is None:
        return None
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def qall(con: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if con is None:
        return []
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def secondary_score_for_mint(mint: str) -> dict[str, Any]:
    direct = load_json(SECONDARY_SCORE_DIR / f"{mint}.json", None)
    if isinstance(direct, dict):
        return direct
    summary = load_json(SECONDARY_SUMMARY_PATH, {"rows": []})
    for row in summary.get("rows") or []:
        if isinstance(row, dict) and row.get("mint") == mint:
            return row
    return {}


def dex_context(mint: str, *, ttl_seconds: int = 60, enabled: bool = True) -> tuple[dict[str, Any], str | None]:
    if not enabled:
        return {}, None
    try:
        from dexscreener_client import fetch_token
        dex = fetch_token("solana", mint, cache=True, ttl_seconds=ttl_seconds)
        return dex.get("summary") or {}, None
    except Exception as exc:
        return {}, str(exc)[:240]


def wallet_summary(con: sqlite3.Connection | None, mint: str) -> dict[str, Any]:
    counts = qone(con, """
        SELECT
            COUNT(*) AS events,
            COUNT(DISTINCT wallet) AS wallets,
            SUM(CASE WHEN lower(COALESCE(side, event_type, ''))='buy' THEN 1 ELSE 0 END) AS buys,
            SUM(CASE WHEN lower(COALESCE(side, event_type, ''))='sell' THEN 1 ELSE 0 END) AS sells,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(side, event_type, ''))='buy' THEN wallet END) AS buy_wallets,
            COUNT(DISTINCT CASE WHEN lower(COALESCE(side, event_type, ''))='sell' THEN wallet END) AS sell_wallets,
            MIN(CASE WHEN lower(COALESCE(side, event_type, ''))='buy' THEN block_time_utc END) AS first_buy_utc,
            MAX(block_time_utc) AS last_event_utc,
            SUM(COALESCE(amount_usd, 0)) AS amount_usd,
            SUM(COALESCE(amount_sol, 0)) AS amount_sol,
            MIN(CASE WHEN lower(COALESCE(side, event_type, ''))='buy' THEN market_cap_usd END) AS first_buy_market_cap_usd
        FROM wallet_token_events
        WHERE mint=?
    """, (mint,)) or {}
    top_wallets = qall(con, """
        SELECT
            e.wallet,
            MIN(e.block_time_utc) AS first_touch_utc,
            SUM(CASE WHEN lower(COALESCE(e.side, e.event_type, ''))='buy' THEN 1 ELSE 0 END) AS buys,
            SUM(CASE WHEN lower(COALESCE(e.side, e.event_type, ''))='sell' THEN 1 ELSE 0 END) AS sells,
            SUM(COALESCE(e.amount_usd, 0)) AS amount_usd,
            MIN(CASE WHEN lower(COALESCE(e.side, e.event_type, ''))='buy' THEN e.market_cap_usd END) AS first_buy_market_cap_usd,
            MAX(COALESCE(ws.wallet_score, 0)) AS wallet_score,
            MAX(COALESCE(ws.role, '')) AS role,
            MAX(ws.pnl_all) AS pnl_all
        FROM wallet_token_events e
        LEFT JOIN (
            SELECT wallet,
                   MAX(COALESCE(score, secondary_score, actor_score, 0)) AS wallet_score,
                   MAX(COALESCE(classification, tier, copyability, '')) AS role,
                   MAX(pnl_all) AS pnl_all
            FROM wallet_scores
            GROUP BY wallet
        ) ws ON ws.wallet=e.wallet
        WHERE e.mint=?
        GROUP BY e.wallet
        ORDER BY wallet_score DESC, amount_usd DESC
        LIMIT 5
    """, (mint,))
    counts["top_wallets"] = top_wallets
    return counts


def token_signal(con: sqlite3.Connection | None, mint: str) -> dict[str, Any]:
    return qone(con, """
        SELECT * FROM token_signals
        WHERE mint=?
        ORDER BY COALESCE(created_at_utc, first_buy_utc, captured_at_utc) DESC
        LIMIT 1
    """, (mint,)) or {}


def concentration(con: sqlite3.Connection | None, mint: str) -> dict[str, Any]:
    return qone(con, """
        SELECT * FROM token_concentration_snapshots
        WHERE mint=?
        ORDER BY snapshot_at_utc DESC
        LIMIT 1
    """, (mint,)) or {}


def token_row(con: sqlite3.Connection | None, mint: str) -> dict[str, Any]:
    return qone(con, "SELECT * FROM tokens WHERE mint=? LIMIT 1", (mint,)) or {}


def recent_ledger(con: sqlite3.Connection | None, mint: str) -> dict[str, Any]:
    return qone(con, """
        SELECT timestamp_utc, source_command, verdict, signal_kind, entry_action,
               position_action, catalyst_type, flow_conversion_status, fake_flow_severity,
               market_cap, liquidity_usd, fact_grade
        FROM signals
        WHERE mint=?
        ORDER BY timestamp_utc DESC
        LIMIT 1
    """, (mint,)) or {}


def classify(payload: dict[str, Any]) -> dict[str, Any]:
    sig = payload.get("signal") or {}
    wallets = payload.get("wallets") or {}
    conc = payload.get("concentration") or {}
    market = payload.get("market") or {}
    secondary = payload.get("secondary") or {}

    wallet_count = as_int(sig.get("wallet_count"), 0) or as_int(wallets.get("buy_wallets"), 0)
    tg_count = as_int(sig.get("tg_channel_count"), 0)
    ath_x = as_float(sig.get("ath_multiplier"), None)
    call_mc = as_float(sig.get("call_market_cap_usd"), None)
    current_mc = as_float(market.get("market_cap") or sig.get("current_market_cap_usd"), None)
    liq = as_float(market.get("liquidity_usd"), None)
    supply_pct = as_float(conc.get("supply_pct"), None)
    score = as_float(secondary.get("score"), None)

    why: list[str] = []
    risk: list[str] = []
    missing: list[str] = []

    if wallet_count >= 3:
        why.append(f"tracked multi-buy cluster: {wallet_count} wallets")
    elif wallet_count > 0:
        why.append(f"tracked wallet activity: {wallet_count} buy wallet(s)")
    else:
        missing.append("no tracked wallet activity in local tape")

    if tg_count >= 3:
        why.append(f"TG/channel confirmation: {tg_count} channels")
    if ath_x is not None:
        why.append(f"historical peak from signal: {ath_x:.2f}x")
    if score is not None and score >= 55:
        why.append(f"Secondary mint score: {score:.1f}")
    if call_mc is not None and 30_000 <= call_mc <= 300_000:
        why.append(f"call MC in trench band: {money(call_mc)}")

    if current_mc and call_mc and current_mc < call_mc * 0.45:
        risk.append("current MC is far below call MC; failed follow-through or dead bounce")
    if liq is not None and liq < 10_000:
        risk.append("thin liquidity under $10K")
    if supply_pct is not None and supply_pct >= 20:
        risk.append(f"elevated tracked concentration: {supply_pct:.1f}%")
    if not market:
        missing.append("fresh market snapshot unavailable")
    if not sig and not wallets.get("events"):
        risk.append("no local alpha evidence for this mint")

    freshness = payload.get("freshness") or {}
    if freshness.get("status") == "stale":
        risk.extend(str(x) for x in (freshness.get("stale_reasons") or [])[:3])
        missing.append("live ingest required before treating this as current alpha")

    verdict = "unknown"
    if freshness.get("status") == "stale" and (why or risk):
        verdict = "stale-tape"
    elif risk and not why:
        verdict = "ignore"
    elif current_mc and call_mc and current_mc < call_mc * 0.45:
        verdict = "avoid-entry"
    elif wallet_count >= 3 and (tg_count >= 3 or (score is not None and score >= 55)):
        verdict = "deep-check"
    elif wallet_count >= 2 or (score is not None and score >= 45):
        verdict = "watch"
    elif why:
        verdict = "study"

    return {"verdict": verdict, "why": why[:5], "risk": risk[:5], "missing": missing[:5]}


def elite_buy_signals(
    con: sqlite3.Connection | None,
    *,
    window_minutes: int = ELITE_ACTIONABLE_WINDOW_MINUTES,
    limit: int = 25,
    max_mints_per_wallet: int = ELITE_MAX_MINTS_PER_WALLET,
) -> list[dict[str, Any]]:
    """Fresh decoded elite buys, capped per source wallet before aggregation.

    A short actionable window prevents old wallet activity from becoming a late
    paper lead. Per-wallet ranking stops one hyperactive address from filling
    the entire candidate queue. Shadow/discovery prefixes remain excluded.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, window_minutes))).isoformat(timespec="seconds")
    placeholders = ",".join("?" for _ in EXCLUDED_SWEEP_MINTS)
    return qall(con, f"""
        WITH per_wallet_mint AS (
            SELECT mint,wallet,
                   COUNT(*) AS buy_events,
                   SUM(CASE WHEN COALESCE(sol_delta, 0) < 0 THEN -sol_delta ELSE COALESCE(amount_sol, 0) END) AS total_sol_amount,
                   MIN(block_time_utc) AS first_buy_utc,
                   MAX(block_time_utc) AS last_buy_utc
            FROM wallet_token_events
            WHERE event_type='buy'
              AND source_id IN ({ONCHAIN_SOURCE_SQL})
              AND run_id LIKE 'elite-%'
              AND COALESCE(confidence, 'medium') IN ('medium', 'high')
              AND mint IS NOT NULL
              AND mint NOT IN ({placeholders})
              AND block_time_utc >= ?
            GROUP BY mint,wallet
        ), ranked AS (
            SELECT *,ROW_NUMBER() OVER (
                PARTITION BY wallet ORDER BY last_buy_utc DESC,buy_events DESC,mint
            ) AS wallet_rank
            FROM per_wallet_mint
        ), capped AS (
            SELECT * FROM ranked WHERE wallet_rank<=?
        )
        SELECT mint,
               COUNT(DISTINCT wallet) AS wallet_count,
               SUM(buy_events) AS buy_events,
               SUM(total_sol_amount) AS total_sol_amount,
               MIN(first_buy_utc) AS first_buy_utc,
               MAX(last_buy_utc) AS last_buy_utc
        FROM capped
        GROUP BY mint
        ORDER BY COUNT(DISTINCT wallet) DESC,MAX(last_buy_utc) DESC,SUM(buy_events) DESC
        LIMIT ?
    """, (*EXCLUDED_SWEEP_MINTS, cutoff, max(1, max_mints_per_wallet), max(1, limit)))


def score_candidate(item: dict[str, Any]) -> float:
    """Deterministic priority score from the same facts classify() grades."""
    sig = item.get("signal") or {}
    wallets = item.get("wallets") or {}
    secondary = item.get("secondary") or {}
    gate = item.get("gate") or {}
    wallet_count = as_int(sig.get("wallet_count"), 0) or as_int(wallets.get("buy_wallets"), 0)
    tg_count = as_int(sig.get("tg_channel_count"), 0)
    secondary_score = as_float(secondary.get("score"), None)
    call_mc = as_float(sig.get("call_market_cap_usd"), None)
    score = min(wallet_count, 5) * 8.0 + min(tg_count, 5) * 4.0
    if secondary_score is not None:
        score += min(max(secondary_score, 0.0), 100.0) * 0.2
    if call_mc is not None and 30_000 <= call_mc <= 300_000:
        score += 10.0
    score += {"deep-check": 15.0, "watch": 8.0, "study": 2.0}.get(gate.get("verdict"), 0.0)
    return round(score, 2)


def unavailable_payload(mint: str, errors: list[str], started: float) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": "alpha_tape_token",
        "generated_at": now_utc(),
        "mint": mint,
        "market": {},
        "signal": {},
        "wallets": {},
        "concentration": {},
        "secondary": {},
        "latest_ledger": {},
        "source": "local-alpha-tape",
        "errors": errors[:8],
        "gate": {"verdict": "tape-unavailable", "why": [], "risk": errors[:5], "missing": []},
        "boundary": BOUNDARY,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def token_payload(mint: str, *, dex: bool = True, dex_ttl: int = 60) -> dict[str, Any]:
    started = time.perf_counter()
    con = connect_ro(DB_PATH)
    ledger = connect_ro(SIGNAL_LEDGER_PATH)
    required = {"token_signals", "wallet_token_events", "wallet_scores", "token_concentration_snapshots", "tokens"}
    errors = require_tables(con, required)
    if errors:
        if con is not None:
            con.close()
        if ledger is not None:
            ledger.close()
        return unavailable_payload(mint, errors, started)
    try:
        sig = token_signal(con, mint)
        wallets = wallet_summary(con, mint)
        conc = concentration(con, mint)
        freshness = tape_freshness(con)
        tok = token_row(con, mint)
        secondary = secondary_score_for_mint(mint)
        try:
            ledger_row = recent_ledger(ledger, mint) if ledger is not None and "signals" in table_names(ledger) else {}
        except sqlite3.Error:
            ledger_row = {}
        dex_summary, dex_error = dex_context(mint, ttl_seconds=dex_ttl, enabled=dex)
    except sqlite3.Error as exc:
        return unavailable_payload(mint, [f"query failed: {exc}"], started)
    finally:
        if con is not None:
            con.close()
        if ledger is not None:
            ledger.close()

    market = {
        "symbol": dex_summary.get("symbol") or secondary.get("symbol") or tok.get("symbol"),
        "name": dex_summary.get("name") or tok.get("name"),
        "market_cap": dex_summary.get("marketCap") or sig.get("current_market_cap_usd") or tok.get("latest_market_cap_usd"),
        "liquidity_usd": dex_summary.get("liquidity_usd"),
        "price_usd": dex_summary.get("priceUsd"),
        "url": dex_summary.get("url"),
        "dex_id": dex_summary.get("dexId"),
        "pair_created_at": dex_summary.get("pairCreatedAt"),
    }
    payload = {
        "ok": True,
        "mode": "alpha_tape_token",
        "generated_at": now_utc(),
        "mint": mint,
        "market": {k: v for k, v in market.items() if v not in (None, "")},
        "signal": sig,
        "wallets": wallets,
        "concentration": conc,
        "freshness": freshness,
        "secondary": secondary,
        "latest_ledger": ledger_row,
        "dex_error": dex_error,
        "source": "local-alpha-tape",
        "boundary": BOUNDARY,
    }
    payload["gate"] = classify(payload)
    payload["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return payload


def sweep_payload(*, limit: int = 5, dex: bool = False, dex_ttl: int = 60) -> dict[str, Any]:
    started = time.perf_counter()
    limit = max(1, min(int(limit or 5), 50))
    con = connect_ro(DB_PATH)
    required = {"token_signals", "wallet_token_events", "wallet_scores", "token_concentration_snapshots", "tokens"}
    errors = require_tables(con, required)
    if errors:
        if con is not None:
            con.close()
        return {
            "ok": False,
            "mode": "alpha_tape_sweep",
            "generated_at": now_utc(),
            "limit": limit,
            "candidates": [],
            "candidate_count": 0,
            "source": "local-alpha-tape",
            "errors": errors[:8],
            "dex_enabled": dex,
            "boundary": BOUNDARY,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    rows: list[dict[str, Any]] = []
    freshness: dict[str, Any] = {}
    seen_mints: set[str] = set()
    try:
        freshness = tape_freshness(con)
        # Live elite-buy candidates judge staleness on wallet events alone (with a
        # threshold that spans one ingest cadence); the imported token_signals feed
        # going quiet must not mark them stale.
        wallet_age = (freshness.get("age_seconds") or {}).get("wallet_events")
        elite_is_stale = wallet_age is None or wallet_age > ELITE_EVENT_MAX_STALE_SECONDS
        elite_freshness = {
            **freshness,
            "ok": not elite_is_stale,
            "status": "fresh" if not elite_is_stale else "stale",
            "stale_reasons": [f"wallet events stale: {age_label(wallet_age)}"] if elite_is_stale else [],
            "scope": "wallet-events-only",
        }
        for r in elite_buy_signals(con, limit=limit * 2):
            mint = str(r.get("mint") or "")
            if not mint or mint in seen_mints:
                continue
            seen_mints.add(mint)
            tok = token_row(con, mint)
            secondary = secondary_score_for_mint(mint)
            sig = {
                "signal_type": "elite-wallet-buys",
                "wallet_count": r.get("wallet_count"),
                "tg_channel_count": 0,
                "total_sol_amount": r.get("total_sol_amount"),
                "first_buy_utc": r.get("first_buy_utc"),
                "created_at_utc": r.get("last_buy_utc"),
                "captured_at_utc": r.get("last_buy_utc"),
                "buy_events": r.get("buy_events"),
                "actionable_window_minutes": ELITE_ACTIONABLE_WINDOW_MINUTES,
                "max_mints_per_wallet": ELITE_MAX_MINTS_PER_WALLET,
            }
            item = {
                "mint": mint,
                "market": {"symbol": secondary.get("symbol") or tok.get("symbol"), "market_cap": tok.get("latest_market_cap_usd")},
                "signal": sig,
                "wallets": wallet_summary(con, mint),
                "concentration": concentration(con, mint),
                "freshness": elite_freshness,
                "secondary": secondary,
                "source": "elite-wallet-buys",
            }
            item["gate"] = classify(item)
            rows.append(item)
        signals = qall(con, """
            SELECT mint, signal_type, wallet_count, tg_channel_count, total_sol_amount,
                   call_market_cap_usd, current_market_cap_usd, ath_market_cap_usd,
                   ath_multiplier, is_hit, first_buy_utc, created_at_utc, captured_at_utc
            FROM token_signals
            ORDER BY COALESCE(created_at_utc, first_buy_utc, captured_at_utc) DESC
            LIMIT ?
        """, (max(limit * 4, limit),))
        for sig in signals:
            mint = sig.get("mint") or ""
            if not mint or mint in seen_mints:
                continue
            seen_mints.add(mint)
            wallets = wallet_summary(con, mint)
            conc = concentration(con, mint)
            tok = token_row(con, mint)
            secondary = secondary_score_for_mint(mint)
            market = {
                "symbol": secondary.get("symbol") or tok.get("symbol"),
                "market_cap": sig.get("current_market_cap_usd") or tok.get("latest_market_cap_usd"),
            }
            item = {
                "mint": mint,
                "market": market,
                "signal": sig,
                "wallets": wallets,
                "concentration": conc,
                "freshness": freshness,
                "secondary": secondary,
                "source": "local-alpha-tape",
            }
            item["gate"] = classify(item)
            rows.append(item)
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "mode": "alpha_tape_sweep",
            "generated_at": now_utc(),
            "limit": limit,
            "candidates": [],
            "candidate_count": 0,
            "source": "local-alpha-tape",
            "errors": [f"query failed: {exc}"],
            "dex_enabled": dex,
            "boundary": BOUNDARY,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    finally:
        if con is not None:
            con.close()
    rows = rows[:limit]
    for item in rows:
        if dex:
            dex_summary, dex_error = dex_context(item["mint"], ttl_seconds=dex_ttl, enabled=True)
            market = item["market"]
            market["liquidity_usd"] = dex_summary.get("liquidity_usd")
            market["price_usd"] = dex_summary.get("priceUsd")
            market["market_cap"] = dex_summary.get("marketCap") or market.get("market_cap")
            if dex_error:
                item["dex_error"] = dex_error
            item["gate"] = classify(item)
        item["candidate_score"] = score_candidate(item)
    return {
        "ok": True,
        "mode": "alpha_tape_sweep",
        "generated_at": now_utc(),
        "limit": limit,
        "candidates": rows,
        "candidate_count": len(rows),
        "freshness": freshness,
        "source": "local-alpha-tape",
        "dex_enabled": dex,
        "boundary": BOUNDARY,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def render_token(payload: dict[str, Any]) -> str:
    mint = payload.get("mint") or ""
    if not payload.get("ok", True):
        errors = payload.get("errors") or ["local alpha tape unavailable"]
        lines = [
            "☄️ ⚠️ ALPHA TAPE UNAVAILABLE",
            f"`{short_ca(mint)}`",
            f"LATENCY: {payload.get('latency_ms')}ms · {payload.get('source')}",
            "WHY:",
            *[f"- {str(e)}" for e in errors[:5]],
            "⚡ Next",
            f"```text\nanalyze token {mint}\n```",
            BOUNDARY,
        ]
        return "\n".join(lines)
    market = payload.get("market") or {}
    sig = payload.get("signal") or {}
    wallets = payload.get("wallets") or {}
    conc = payload.get("concentration") or {}
    freshness = payload.get("freshness") or {}
    gate = payload.get("gate") or {}
    symbol = market.get("symbol") or sig.get("token_symbol") or "UNKNOWN"
    signal_wallet_count = as_int(sig.get('wallet_count'), 0) or as_int(wallets.get('buy_wallets'), 0)
    lines = [
        f"☄️ ⚡ ALPHA TAPE",
        f"${symbol} · `{short_ca(mint)}`",
        f"VERDICT: **{gate.get('verdict') or 'unknown'}**",
        f"SIGNAL: {sig.get('signal_type') or 'none'} · wallets {signal_wallet_count} · TG {as_int(sig.get('tg_channel_count'), 0)} · ATH {as_float(sig.get('ath_multiplier'), 0) or 0:.2f}x",
        f"MARKET: MC {money(market.get('market_cap'))} · Liq {money(market.get('liquidity_usd'))} · Call {money(sig.get('call_market_cap_usd'))}",
        f"WALLETS: buys {as_int(wallets.get('buy_wallets'), 0)} · sells {as_int(wallets.get('sell_wallets'), 0)} · first {wallets.get('first_buy_utc') or '?'}",
    ]
    if conc:
        lines.append(f"HOLDERS: {as_int(conc.get('holder_count'), 0)} · tracked supply {as_float(conc.get('supply_pct'), 0) or 0:.1f}%")
    if freshness:
        labels = freshness.get("age_labels") or {}
        lines.append(f"FRESHNESS: **{freshness.get('status')}** · wallet {labels.get('wallet_events', 'unknown')} · signal {labels.get('token_signals', 'unknown')} · conc {labels.get('concentration', 'unknown')}")
    lines.append(f"LATENCY: {payload.get('latency_ms')}ms · {payload.get('source')}")
    lines.append("WHY:")
    lines.extend(f"- {x}" for x in (gate.get("why") or ["no positive local alpha evidence"])[0:3])
    lines.append("RISK/MISSING:")
    risks = (gate.get("risk") or []) + (gate.get("missing") or [])
    lines.extend(f"- {x}" for x in (risks or ["run deep analysis before acting"])[0:3])
    lines.extend([
        "\n📋 Copy CA",
        f"```text\n{mint}\n```",
        f"🧭 Open: [DEX](https://dexscreener.com/solana/{mint}) · [SOL](https://solscan.io/token/{mint})",
        "⚡ Next",
        f"```text\nanalyze token {mint}\n```",
        BOUNDARY,
    ])
    return "\n".join(lines)


def render_sweep(payload: dict[str, Any]) -> str:
    rows = payload.get("candidates") or []
    if not payload.get("ok", True):
        lines = [
            "☄️ ⚠️ ALPHA TAPE SWEEP UNAVAILABLE",
            f"latency {payload.get('latency_ms')}ms · {payload.get('source')}",
            "WHY:",
            *[f"- {str(e)}" for e in (payload.get("errors") or ["local alpha tape unavailable"] )[:5]],
            BOUNDARY,
        ]
        return "\n".join(lines)
    freshness = payload.get("freshness") or {}
    lines = [
        "☄️ ⚡ ALPHA TAPE SWEEP",
        f"Candidates {len(rows)} · latency {payload.get('latency_ms')}ms · {payload.get('source')}",
    ]
    if freshness:
        labels = freshness.get("age_labels") or {}
        lines.append(f"FRESHNESS: **{freshness.get('status')}** · wallet {labels.get('wallet_events', 'unknown')} · signal {labels.get('token_signals', 'unknown')}")
    mints: list[str] = []
    for idx, item in enumerate(rows, 1):
        mint = item.get("mint") or ""
        mints.append(mint)
        market = item.get("market") or {}
        sig = item.get("signal") or {}
        gate = item.get("gate") or {}
        symbol = market.get("symbol") or "UNKNOWN"
        wallet_count = as_int(sig.get('wallet_count'), 0) or as_int((item.get("wallets") or {}).get('buy_wallets'), 0)
        lines.append(
            f"{idx}. **${symbol}** `{short_ca(mint)}` · {gate.get('verdict')} · wallets {wallet_count} · TG {as_int(sig.get('tg_channel_count'), 0)} · ATH {as_float(sig.get('ath_multiplier'), 0) or 0:.2f}x · call {money(sig.get('call_market_cap_usd'))}"
        )
    if mints:
        lines.extend([
            "\n📋 Copy CA list",
            "```text\n" + "\n".join(mints[:5]) + "\n```",
            "⚡ Next",
            "```text\n" + "\n".join(f"token {m}" for m in mints[:5]) + "\n```",
        ])
    else:
        lines.append("No local candidates surfaced.")
    lines.append(BOUNDARY)
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Chaos fast local alpha tape")
    sub = p.add_subparsers(dest="cmd", required=True)
    tp = sub.add_parser("token")
    tp.add_argument("mint")
    tp.add_argument("--with-dex", action="store_true", help="Opt in to a short-TTL Dex snapshot")
    tp.add_argument("--dex-ttl", type=int, default=60)
    tp.add_argument("--raw", action="store_true")
    sp = sub.add_parser("sweep")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--raw", action="store_true")
    args = p.parse_args()
    if args.cmd == "token":
        payload = token_payload(args.mint, dex=args.with_dex, dex_ttl=args.dex_ttl)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str) if args.raw else render_token(payload))
    elif args.cmd == "sweep":
        payload = sweep_payload(limit=args.limit)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str) if args.raw else render_sweep(payload))


if __name__ == "__main__":
    main()
