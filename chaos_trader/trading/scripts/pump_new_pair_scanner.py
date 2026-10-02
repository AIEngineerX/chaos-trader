#!/usr/bin/env python3
"""Read-only pump.fun new-pair scanner for Chaos paper discovery.

Fetches latest pump.fun launches, filters by age/curve state/market cap, stores
candidate mints in paper_autopilot.sqlite, and optionally runs the existing paper
analysis engine. No buy/sell/create/signing/routing.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
PAPER_DB = PROFILE_HOME / "trading" / "db" / "paper_autopilot.sqlite"
REPORT_DIR = PROFILE_HOME / "trading" / "reports" / "pump_scanner"
PUMP_URL = "https://frontend-api-v3.pump.fun/coins"
BOUNDARY = "read-only pump.fun new-pair paper scanner; no wallet, signing, orders, routing, swaps, alerts, or execution"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chaos_paper_autopilot import CONFIG_PATH, PaperAutopilotRunner, RunnerConfig, jdump, now_utc  # noqa: E402


def fetch_sol_usd() -> float:
    """Fetch live SOL/USD price. Returns 0.0 on failure (caller should fall back)."""
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            headers={"Accept": "application/json", "User-Agent": "ChaosReadOnly/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            price = float(data.get("solana", {}).get("usd", 0))
            if price > 0:
                return price
    except Exception:
        pass
    # Jupiter fallback
    try:
        req = urllib.request.Request(
            "https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000000&slippageBps=50",
            headers={"Accept": "application/json", "User-Agent": "ChaosReadOnly/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            out = float(data.get("outAmount", 0))
            if out > 0:
                return out / 1e6  # USDC is 6 decimals, 1 SOL input
    except Exception:
        pass
    return 0.0


def get_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ChaosReadOnly/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest(limit: int, offset: int = 0, include_nsfw: bool = False) -> list[dict[str, Any]]:
    q = {
        "offset": offset,
        "limit": max(1, min(limit, 100)),
        "sort": "created_timestamp",
        "order": "DESC",
        "includeNsfw": "true" if include_nsfw else "false",
    }
    data = get_json(PUMP_URL + "?" + urllib.parse.urlencode(q))
    return data if isinstance(data, list) else []


def age_minutes(ms: int | float | None) -> float | None:
    if not ms:
        return None
    return max(0.0, (time.time() * 1000.0 - float(ms)) / 60000.0)


def coin_usd_market_cap(c: dict[str, Any]) -> float:
    """Best-effort USD market cap from the pump.fun API."""
    usd = c.get("usd_market_cap")
    return float(usd) if usd else 0.0


def coin_liquidity_usd(c: dict[str, Any], sol_usd: float) -> float:
    """Bonding-curve liquidity = real SOL reserves in the curve."""
    real = float(c.get("real_sol_reserves") or 0)
    return round(real / 1e9 * sol_usd, 2)


def score_coin(c: dict[str, Any], sol_usd: float) -> float:
    mc_usd = coin_usd_market_cap(c)
    age = age_minutes(c.get("created_timestamp")) or 9999
    replies = float(c.get("reply_count") or 0)
    last_trade_ms = c.get("last_trade_timestamp") or c.get("created_timestamp")
    trade_age = age_minutes(last_trade_ms) or 9999
    liq = coin_liquidity_usd(c, sol_usd)
    score = 0.0
    score += min(35, mc_usd / 1000.0)          # rewards curve traction, capped
    score += max(0, 20 - age / 3)              # fresh but not blindly
    score += min(15, replies * 1.5)
    if trade_age <= 5:
        score += 10
    if liq >= 5000:
        score += 5                             # bonus for real liquidity
    if c.get("complete"):
        score -= 8
    if c.get("is_banned") or c.get("hidden"):
        score -= 100
    return round(score, 4)


def filter_coins(rows: list[dict[str, Any]], *, max_age_min: float, min_mc_sol: float, max_mc_sol: float, sol_usd: float) -> list[dict[str, Any]]:
    out = []
    for c in rows:
        mint = c.get("mint")
        if not mint:
            continue
        a = age_minutes(c.get("created_timestamp"))
        mc_sol = float(c.get("market_cap") or 0)
        if a is None or a > max_age_min:
            continue
        if mc_sol < min_mc_sol or mc_sol > max_mc_sol:
            continue
        if c.get("is_banned") or c.get("hidden"):
            continue
        cc = dict(c)
        cc["age_minutes"] = round(a, 3)
        cc["market_cap_sol"] = mc_sol
        cc["market_cap_usd_est"] = coin_usd_market_cap(c)
        cc["liquidity_usd_est"] = coin_liquidity_usd(c, sol_usd)
        cc["candidate_score"] = score_coin(c, sol_usd)
        out.append(cc)
    return sorted(out, key=lambda x: x.get("candidate_score") or 0, reverse=True)


def upsert_paper_candidates(con: sqlite3.Connection, coins: list[dict[str, Any]], limit: int, sol_usd: float) -> int:
    ts = now_utc()
    n = 0
    for c in coins[:limit]:
        mint = c["mint"]
        source = {
            "source": "pump_new_pair_scanner",
            "boundary": BOUNDARY,
            "pump": c,
            "sol_usd_assumption": sol_usd,
        }
        con.execute(
            """
            INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,sweep_hits,candidate_score,gate_label,market_cap,liquidity_usd,source_json,updated_at_utc)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mint) DO UPDATE SET
              symbol=COALESCE(excluded.symbol,candidates.symbol),
              last_seen_utc=excluded.last_seen_utc,
              sweep_hits=candidates.sweep_hits+1,
              candidate_score=MAX(COALESCE(candidates.candidate_score,0), excluded.candidate_score),
              gate_label='pump-new-pair',
              market_cap=excluded.market_cap,
              source_json=excluded.source_json,
              state=CASE WHEN candidates.state IN ('PAPER_OPEN') THEN candidates.state ELSE 'DISCOVERED' END,
              updated_at_utc=excluded.updated_at_utc
            """,
            (mint, c.get("symbol"), "DISCOVERED", ts, ts, 1, c.get("candidate_score"), "pump-new-pair", c.get("market_cap_usd_est"), c.get("liquidity_usd_est"), jdump(source), ts),
        )
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--max-age-min", type=float, default=20)
    ap.add_argument("--min-mc-sol", type=float, default=20)
    ap.add_argument("--max-mc-sol", type=float, default=500)
    ap.add_argument("--sol-usd", type=float, default=160.0, help="Only for rough pump market-cap USD estimate")
    ap.add_argument("--candidate-limit", type=int, default=15)
    ap.add_argument("--analyze-top", type=int, default=0)
    ap.add_argument("--with-x", action="store_true")
    ap.add_argument("--include-nsfw", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    sol_usd = fetch_sol_usd()
    if sol_usd <= 0:
        sol_usd = args.sol_usd  # fallback to CLI arg

    rows = fetch_latest(args.limit, args.offset, args.include_nsfw)
    coins = filter_coins(rows, max_age_min=args.max_age_min, min_mc_sol=args.min_mc_sol, max_mc_sol=args.max_mc_sol, sol_usd=sol_usd)
    cfg = RunnerConfig.from_file(CONFIG_PATH)
    runner = PaperAutopilotRunner(cfg)
    decisions = []
    with runner.connect() as con:
        inserted = upsert_paper_candidates(con, coins, args.candidate_limit, sol_usd)
        runner.log_event(con, "pump_new_pair_scan", message=f"{len(coins)} pump.fun candidates", payload={"boundary": BOUNDARY, "inserted": inserted, "coins": coins[:25]})
        for c in runner.active_candidates(con, limit=args.analyze_top, with_x=args.with_x):
            try:
                decisions.append(runner.decide_candidate(con, c["mint"], use_x=args.with_x))
            except Exception as exc:
                runner.log_event(con, "pump_new_pair_analyze_error", mint=c.get("mint"), message=str(exc)[:500], payload={"boundary": BOUNDARY})
        monitored = runner.monitor_positions_once(con)
        con.commit()

    payload = {
        "ok": True,
        "mode": "pump_new_pair_scanner",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched": len(rows),
        "matched": len(coins),
        "inserted": inserted,
        "decisions": decisions,
        "open_positions_checked": len(monitored),
        "filters": {"max_age_min": args.max_age_min, "min_mc_sol": args.min_mc_sol, "max_mc_sol": args.max_mc_sol, "sol_usd": sol_usd},
        "top": coins[:10],
        "boundary": BOUNDARY,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"pump_new_pair_scan_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    payload["report"] = str(out)
    print(json.dumps(payload if args.raw else {"ok": True, "matched": len(coins), "inserted": inserted, "decisions": [d.get("decision") for d in decisions], "top": [{"symbol": c.get("symbol"), "mint": c.get("mint"), "age_min": c.get("age_minutes"), "mc_sol": c.get("market_cap_sol"), "score": c.get("candidate_score")} for c in coins[:5]], "report": str(out)}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
