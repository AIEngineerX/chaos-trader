#!/usr/bin/env python3
"""Wallet-seeded live paper hunter for Chaos.

Read-only. Pulls recent activity from scored wallet leads, turns wallet-touched
mints into paper candidates, and lets the paper engine validate them.

No wallet connection, signing, orders, routing, swaps, webhooks, or alerts.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
SMART_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
REPORT_DIR = PROFILE_HOME / "trading" / "reports" / "wallet_hunter"
BOUNDARY = "wallet-seeded paper-only hunter; no wallet, signing, orders, routing, webhooks, alerts, or live execution"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chaos_paper_autopilot import PaperAutopilotRunner, RunnerConfig, CONFIG_PATH, jdump, now_utc  # noqa: E402
from dexscreener_client import fetch_token  # noqa: E402
from smart_wallet_tracker import enrich_wallet, ensure_db as ensure_smart_db  # noqa: E402
import x_provider  # noqa: E402


def load_latest_wallet_map() -> dict[str, Any]:
    files = sorted(glob.glob(str(PROFILE_HOME / "trading" / "reports" / "wallet_maps" / "wallet_ledger_map_*.json")))
    if not files:
        return {}
    with open(files[-1], "r", encoding="utf-8") as f:
        return json.load(f)


def seed_wallets(limit: int, *, source: str = "legacy-map") -> list[dict[str, Any]]:
    if source != "legacy-map":
        raise ValueError("External signal API paper sources are retired; use elite_wallet_pipeline.py for ingest/review")
    m = load_latest_wallet_map()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for bucket in ("top_strength", "top_insider_candidates"):
        for w in m.get(bucket) or []:
            addr = w.get("wallet")
            if not addr or addr in seen:
                continue
            # Exclude obvious avoid-only wallets unless they are very strong as sensors.
            if w.get("copyability") == "avoid" and float(w.get("strength_score") or 0) < 45:
                continue
            seen.add(addr)
            out.append(w)
            if len(out) >= limit:
                return out
    return out


def parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def recent_wallet_events(con: sqlite3.Connection, wallets: list[str], lookback_hours: float) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")
    qmarks = ",".join("?" for _ in wallets)
    if not qmarks:
        return []
    rows = con.execute(
        f"""
        SELECT wallet,mint,signature,block_time_utc,event_type,side,token_delta,sol_delta,fee_sol,confidence,metadata_json
        FROM wallet_token_events
        WHERE wallet IN ({qmarks})
          AND mint IS NOT NULL
          AND block_time_utc IS NOT NULL
          AND block_time_utc >= ?
          AND event_type IN ('buy','sell','token_transfer_in','token_transfer_out')
        ORDER BY block_time_utc DESC
        LIMIT 500
        """,
        (*wallets, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def wallet_weight(seed_by_wallet: dict[str, dict[str, Any]], wallet: str) -> float:
    s = seed_by_wallet.get(wallet) or {}
    strength = float(s.get("strength_score") or 0)
    insider = float(s.get("insider_score") or 0)
    contam = float(s.get("transfer_contamination") or 1)
    base = 1.0 + min(strength, 75) / 75.0
    if insider >= 45:
        base += 0.35
    if contam >= 0.9:
        base *= 0.75
    return max(0.25, round(base, 4))


def event_weight(event_type: str) -> float:
    return {
        "buy": 1.0,
        # A transfer is not an entry. It may be distribution, custody movement,
        # a relay, or inventory rotation. Keep it in the evidence payload when
        # a real swap also exists, but never let it create positive alpha.
        "token_transfer_in": 0.0,
        "sell": -0.60,
        "token_transfer_out": -0.35,
    }.get(event_type, 0.0)


def score_mints(events: list[dict[str, Any]], seed_by_wallet: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for e in events:
        mint = e.get("mint")
        if not mint:
            continue
        ew = event_weight(str(e.get("event_type")))
        if ew == 0:
            continue
        ww = wallet_weight(seed_by_wallet, e.get("wallet"))
        rec = grouped.setdefault(mint, {"mint": mint, "score": 0.0, "wallets": {}, "events": []})
        rec["score"] += ew * ww
        rec["wallets"].setdefault(e.get("wallet"), 0)
        rec["wallets"][e.get("wallet")] += 1
        rec["events"].append(e)
    out = []
    for rec in grouped.values():
        rec["wallet_count"] = len(rec["wallets"])
        rec["event_count"] = len(rec["events"])
        rec["score"] = round(float(rec["score"]), 4)
        # Require positive net signal from decoded swaps. Transfer-only balance
        # changes must never become wallet-led paper candidates.
        if rec["score"] > 0:
            out.append(rec)
    return sorted(out, key=lambda r: (r["score"], r["wallet_count"], r["event_count"]), reverse=True)


def ensure_paper_candidate_cols(con: sqlite3.Connection) -> None:
    # chaos_paper_autopilot owns schema; opening runner DB ensures migrations.
    cfg = RunnerConfig.from_file(CONFIG_PATH)
    runner = PaperAutopilotRunner(cfg)
    with runner.connect() as pc:
        pc.commit()


def dex_precheck(candidates: list[dict[str, Any]], *, min_liquidity_usd: float, limit: int) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    for c in candidates[: max(limit * 3, limit)]:
        mint = c["mint"]
        try:
            dex = fetch_token("solana", mint, cache=True, ttl_seconds=45)
            summary = dex.get("summary") or {}
            liq = summary.get("liquidity_usd")
            mc = summary.get("marketCap") or summary.get("fdv")
            c["dex"] = summary
            c["liquidity_usd"] = float(liq) if liq is not None else None
            c["market_cap"] = float(mc) if mc is not None else None
        except Exception as exc:
            c["dex_error"] = str(exc)[:200]
            c["liquidity_usd"] = None
            c["market_cap"] = None
        if (c.get("liquidity_usd") or 0) >= min_liquidity_usd:
            checked.append(c)
        elif c.get("wallet_count", 0) >= 2 and c.get("score", 0) >= 2.0:
            # Keep multi-wallet cluster touches even if Dex liquidity is missing; paper engine will judge.
            checked.append(c)
        if len(checked) >= limit:
            break
    return checked


def upsert_candidates(con: sqlite3.Connection, candidates: list[dict[str, Any]], limit: int) -> int:
    n = 0
    ts = now_utc()
    for c in candidates[:limit]:
        mint = c["mint"]
        source = {
            "source": "wallet_live_paper_hunter",
            "boundary": BOUNDARY,
            "score": c["score"],
            "wallet_count": c["wallet_count"],
            "event_count": c["event_count"],
            "liquidity_usd": c.get("liquidity_usd"),
            "market_cap": c.get("market_cap"),
            "dex": c.get("dex"),
            "events": c["events"][:20],
        }
        con.execute(
            """
            INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,sweep_hits,candidate_score,gate_label,market_cap,liquidity_usd,source_json,updated_at_utc)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mint) DO UPDATE SET
              last_seen_utc=excluded.last_seen_utc,
              sweep_hits=candidates.sweep_hits+1,
              candidate_score=MAX(COALESCE(candidates.candidate_score,0), excluded.candidate_score),
              gate_label='wallet-seed',
              source_json=excluded.source_json,
              state=CASE WHEN candidates.state IN ('AVOIDED','PAPER_OPEN') THEN candidates.state ELSE 'DISCOVERED' END,
              updated_at_utc=excluded.updated_at_utc
            """,
            (mint, None, "DISCOVERED", ts, ts, 1, c["score"], "wallet-seed", c.get("market_cap"), c.get("liquidity_usd"), jdump(source), ts),
        )
        n += 1
    return n


def wallet_candidates_for_analysis(
    con: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    cooldown_min: int,
    with_x: bool,
) -> list[dict[str, Any]]:
    """Select only candidates emitted by this wallet cycle.

    The main paper database carries a large discovery backlog. Calling the
    global ``active_candidates`` selector here can spend the wallet-hunter
    analysis budget on unrelated sweep candidates. Preserve cycle score order
    and apply the normal terminal-state/cooldown rules locally instead.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(0, cooldown_min))).isoformat(timespec="seconds")
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        row = con.execute("SELECT * FROM candidates WHERE mint=?", (candidate["mint"],)).fetchone()
        if row is None:
            continue
        item = dict(row)
        if item.get("state") not in {"DISCOVERED", "PRE_FILTERED", "PAPER_WAIT"}:
            continue
        last = item.get("last_deep_analyze_utc")
        needs_x = with_x and item.get("state") == "PAPER_WAIT" and not int(item.get("x_checked") or 0)
        if last and str(last) > cutoff and not needs_x:
            continue
        selected.append(item)
        if len(selected) >= max(0, limit):
            break
    return selected


def run_cycle(*, seed_limit: int, enrich_limit: int, history_limit: int, pages: int, lookback_hours: float, candidate_limit: int, analyze_top: int, with_x: bool, min_liquidity_usd: float, wallet_source: str = "legacy-map") -> dict[str, Any]:
    with_x = bool(with_x) and x_provider.provider_name() != "none"  # D3: no provider is X off
    seeds =seed_wallets(seed_limit, source=wallet_source)
    seed_by = {s["wallet"]: s for s in seeds}
    selected = [s["wallet"] for s in seeds[:enrich_limit]]
    smart = sqlite3.connect(SMART_DB)
    smart.row_factory = sqlite3.Row
    smart.execute("PRAGMA foreign_keys=ON")
    ensure_smart_db(smart)
    enriched = []
    for w in selected:
        try:
            enriched.append(enrich_wallet(smart, w, history_limit, pages, True))
        except Exception as exc:
            enriched.append({"wallet": w, "ok": False, "error": str(exc)[:300]})
    events = recent_wallet_events(smart, selected, lookback_hours)
    smart.commit(); smart.close()
    scored = score_mints(events, seed_by)
    checked = dex_precheck(scored, min_liquidity_usd=min_liquidity_usd, limit=candidate_limit)

    cfg = RunnerConfig.from_file(CONFIG_PATH)
    runner = PaperAutopilotRunner(cfg)
    decisions = []
    with runner.connect() as pc:
        inserted = upsert_candidates(pc, checked, candidate_limit)
        runner.log_event(pc, "wallet_hunter", message=f"{len(checked)}/{len(scored)} wallet-seeded mints passed Dex precheck", payload={"boundary": BOUNDARY, "inserted": inserted, "scored_mints": scored[:25], "dex_checked_mints": checked[:25], "enriched": enriched})
        cooldown = int(runner.config.get("deep_analyze", "cooldown_per_mint_min", default=10))
        analysis_candidates = wallet_candidates_for_analysis(pc, checked, limit=analyze_top, cooldown_min=cooldown, with_x=with_x)
        for c in analysis_candidates:
            try:
                decisions.append(runner.decide_candidate(pc, c["mint"], use_x=with_x))
            except Exception as exc:
                runner.log_event(pc, "wallet_hunter_analyze_error", mint=c.get("mint"), message=str(exc)[:500], payload={"boundary": BOUNDARY})
        monitored = runner.monitor_positions_once(pc)
        pc.commit()
    source_versions = sorted({str(s.get("source_version")) for s in seeds if s.get("source_version")})
    return {"ok": True, "boundary": BOUNDARY, "wallet_source": wallet_source, "wallet_source_versions": source_versions, "seeds": len(seeds), "enriched": len(selected), "events": len(events), "scored_mints": len(scored), "dex_prechecked_mints": len(checked), "inserted": inserted, "analysis_candidates": [row["mint"] for row in analysis_candidates], "decisions": decisions, "open_positions_checked": len(monitored)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-cycles", type=int, default=1)
    ap.add_argument("--sleep-sec", type=float, default=120)
    ap.add_argument("--seed-limit", type=int, default=25)
    ap.add_argument("--wallet-source", choices=("legacy-map",), default="legacy-map")
    ap.add_argument("--enrich-limit", type=int, default=8)
    ap.add_argument("--history-limit", type=int, default=30)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--lookback-hours", type=float, default=24)
    ap.add_argument("--candidate-limit", type=int, default=20)
    ap.add_argument("--min-liquidity-usd", type=float, default=7500)
    ap.add_argument("--analyze-top", type=int, default=3)
    ap.add_argument("--with-x", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    if args.with_x and x_provider.provider_name() == "none":
        print(x_provider.no_provider_notice(), file=sys.stderr)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for i in range(max(1, args.max_cycles)):
        res = run_cycle(seed_limit=args.seed_limit, enrich_limit=args.enrich_limit, history_limit=args.history_limit, pages=args.pages, lookback_hours=args.lookback_hours, candidate_limit=args.candidate_limit, analyze_top=args.analyze_top, with_x=args.with_x, min_liquidity_usd=args.min_liquidity_usd, wallet_source=args.wallet_source)
        res["cycle"] = i + 1
        results.append(res)
        print(json.dumps(res if args.raw else {"ok": res["ok"], "cycle": i+1, "wallet_source": res["wallet_source"], "wallet_source_versions": res["wallet_source_versions"], "events": res["events"], "scored_mints": res["scored_mints"], "dex_prechecked_mints": res.get("dex_prechecked_mints"), "decisions": [d.get("decision") for d in res.get("decisions", [])], "boundary": BOUNDARY}, ensure_ascii=False))
        sys.stdout.flush()
        if i + 1 < args.max_cycles:
            time.sleep(max(1, args.sleep_sec))
    out = {"ok": True, "mode": "wallet_live_paper_hunter", "cycles": len(results), "last": results[-1] if results else None, "boundary": BOUNDARY}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (REPORT_DIR / f"wallet_live_paper_hunter_{stamp}.json").write_text(json.dumps(out, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
