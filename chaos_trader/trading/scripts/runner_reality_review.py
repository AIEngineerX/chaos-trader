#!/usr/bin/env python3
"""Review whether paper-book runner candidates are real by current market data.

Read-only. Uses the paper book's candidates under CHAOS_HOME, current Dexscreener market
data, and optionally a JSON summary of your own wallets. No execution.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_DIR))

from chaos_home import chaos_home  # noqa: E402
from dexscreener_client import resolve_best_token_market  # noqa: E402

PROFILE_HOME = chaos_home()
PAPER_DB = PROFILE_HOME / "trading" / "db" / "paper_autopilot.sqlite"
REPORT_DIR = PROFILE_HOME / "trading" / "reports" / "runner_reality"


def jloads(s: Any) -> Any:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def fnum(x: Any) -> float | None:
    try:
        if x in (None, "", [], {}):
            return None
        return float(x)
    except Exception:
        return None


def dex_summary(mint: str) -> dict[str, Any]:
    """Get the best active market, preferring liquid migrated pairs."""
    try:
        market = resolve_best_token_market(mint, cache=True, ttl_seconds=15)
        return {
            "ok": True,
            "symbol": market.get("symbol"),
            "market_cap": fnum(market.get("market_cap")),
            "liquidity_usd": fnum(market.get("liquidity_usd")),
            "volume_m5": fnum(market.get("volume_m5")),
            "volume_h1": fnum(market.get("volume_h1")),
            "volume_h24": fnum(market.get("volume_h6")),
            "price_change_h1": fnum(market.get("price_change_h1")),
            "price_change_h24": fnum(market.get("price_change_h24")),
            "txns_m5": market.get("txns_m5"),
            "txns_h1": market.get("txns_h1"),
            "url": market.get("url"),
            "pair_count": market.get("pair_count_total"),
            "dex_id": market.get("dex_id"),
            "pair_selection_reason": market.get("pair_selection_reason"),
            "stale_possible": bool(market.get("_stale_possible")),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def candidate_rows(limit: int) -> list[dict[str, Any]]:
    # No paper book yet means no candidates; connecting would create an empty file instead.
    if not PAPER_DB.exists():
        return []
    con = sqlite3.connect(PAPER_DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM candidates ORDER BY updated_at_utc DESC LIMIT ?", (limit,))]
    con.close()
    return rows


def classify_source(src: dict[str, Any]) -> str:
    return src.get("source") or ("pump_new_pair_scanner" if src.get("pump") else "unknown")


def review_candidates(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out = []
    for r in candidate_rows(limit):
        src = jloads(r.get("source_json"))
        source = classify_source(src)
        mint = r["mint"]
        current = dex_summary(mint)
        initial_mc = fnum(r.get("market_cap"))
        current_mc = fnum(current.get("market_cap"))
        current_liq = fnum(current.get("liquidity_usd"))
        mc_mult = (current_mc / initial_mc) if initial_mc and current_mc else None
        real_runner = False
        reason = []
        major_mints = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "So11111111111111111111111111111111111111112",  # WSOL
            "So11111111111111111111111111111111111111111",
        }
        if mint in major_mints:
            reason.append("excluded_major_asset")
        else:
            h1 = fnum(current.get("price_change_h1"))
            h24 = fnum(current.get("price_change_h24"))
            vol = fnum(current.get("volume_h1")) or fnum(current.get("volume_h24")) or 0.0
            if current_mc and current_mc >= 25_000:
                reason.append("current_mc>=25k")
            if current_liq and current_liq >= 7_500:
                reason.append("current_liq>=7.5k")
            if mc_mult and mc_mult >= 1.5:
                reason.append(f"mc_mult={mc_mult:.2f}x")
            if h1 is not None and h1 >= 25:
                reason.append(f"h1_change={h1:.1f}%")
            if h24 is not None and h24 >= 50:
                reason.append(f"h24_change={h24:.1f}%")
            if current.get("volume_h1") and current.get("volume_h1") >= 10_000:
                reason.append("h1_volume>=10k")
            # A real runner must have *movement*, not merely survivable liquidity.
            real_runner = bool(
                (mc_mult is not None and mc_mult >= 1.5 and current_mc and current_mc >= 10_000)
                or ((h1 is not None and h1 >= 25) and vol >= 5_000 and current_mc and current_mc >= 10_000)
                or ((h24 is not None and h24 >= 50) and vol >= 10_000 and current_mc and current_mc >= 10_000)
            )
        out.append({
            "mint": mint,
            "symbol": r.get("symbol") or current.get("symbol"),
            "state": r.get("state"),
            "source": source,
            "candidate_score": fnum(r.get("candidate_score")),
            "initial_market_cap": initial_mc,
            "current": current,
            "mc_multiple": None if mc_mult is None else round(mc_mult, 4),
            "real_runner_candidate": real_runner,
            "runner_evidence": reason,
            "updated_at_utc": r.get("updated_at_utc"),
        })
    by_source = Counter(x["source"] for x in out)
    real_by_source = Counter(x["source"] for x in out if x["real_runner_candidate"])
    stats = {"checked": len(out), "by_source": dict(by_source), "real_runner_candidates": sum(1 for x in out if x["real_runner_candidate"]), "real_by_source": dict(real_by_source)}
    return out, stats


def owner_patterns(owner_json: Path | None = None) -> dict[str, Any]:
    if owner_json is None:
        return {"status": "omitted", "reason": "owner analysis input requires explicit --owner-analysis path outside the source package"}
    if not owner_json.exists():
        return {"error": "owner analysis json missing"}
    d = json.loads(owner_json.read_text())
    wallets = d.get("wallets") or []
    all_mints: dict[str, dict[str, Any]] = defaultdict(lambda: {"wallets": set(), "pnl": 0.0, "buys": 0, "sells": 0, "firsts": [], "lasts": []})
    summaries = []
    for w in wallets:
        summaries.append({k: w.get(k) for k in ("label", "tx_sample", "failed_pct", "unique_mints_touched", "roundtrip_mints", "approx_win_rate_on_roundtrips_pct", "approx_realized_sol_sample", "median_hold_hours_sample")})
        for m in w.get("top_mints") or []:
            mint = m.get("mint")
            if not mint:
                continue
            rec = all_mints[mint]
            rec["wallets"].add(w.get("label"))
            rec["pnl"] += float(m.get("approx_realized_sol") or 0)
            rec["buys"] += int(m.get("buys") or 0)
            rec["sells"] += int(m.get("sells") or 0)
            if m.get("first"):
                rec["firsts"].append(m.get("first"))
            if m.get("last"):
                rec["lasts"].append(m.get("last"))
    cross = []
    for mint, rec in all_mints.items():
        if len(rec["wallets"]) >= 2:
            cross.append({"mint": mint, "wallet_count": len(rec["wallets"]), "wallets": sorted(rec["wallets"]), "approx_pnl_sol": round(rec["pnl"], 6), "buys": rec["buys"], "sells": rec["sells"], "first": min(rec["firsts"]) if rec["firsts"] else None, "last": max(rec["lasts"]) if rec["lasts"] else None})
    cross.sort(key=lambda x: (x["wallet_count"], x["approx_pnl_sol"]), reverse=True)
    winners = sorted(([{"mint": k, "approx_pnl_sol": round(v["pnl"], 6), "wallet_count": len(v["wallets"]), "buys": v["buys"], "sells": v["sells"]} for k, v in all_mints.items()]), key=lambda x: x["approx_pnl_sol"], reverse=True)
    return {
        "generated_at": d.get("generated_at"),
        "wallet_summaries": summaries,
        "style_read": d.get("style_read"),
        "cross_wallet_mints": cross[:20],
        "top_winners": winners[:15],
        "top_losers": list(reversed(winners[-15:])),
    }


def render_md(payload: dict[str, Any]) -> str:
    lines = ["# Runner Reality Review", "", f"Generated: {payload['generated_at_utc']}", "", "> Read-only. No execution.", ""]
    stats = payload["candidate_stats"]
    lines += ["## Runner Reality", "", f"- Candidates checked: **{stats['checked']}**", f"- Real-runner candidates by current marks: **{stats['real_runner_candidates']}**", f"- Sources: `{stats['by_source']}`", ""]
    lines += ["| Symbol | Mint | Source | State | Initial MC | Current MC | Liq | MC x | Runner evidence |", "|---|---|---|---|---:|---:|---:|---:|---|"]
    for r in payload["candidates"][:30]:
        cur = r.get("current") or {}
        lines.append(f"| {r.get('symbol') or ''} | `{r['mint'][:6]}…{r['mint'][-4:]}` | {r.get('source')} | {r.get('state')} | {r.get('initial_market_cap')} | {cur.get('market_cap')} | {cur.get('liquidity_usd')} | {r.get('mc_multiple')} | {', '.join(r.get('runner_evidence') or [])} |")
    owner = payload.get("owner") or {}
    lines += ["", "## Own-Wallet Pattern", "", "| Wallet | Fail % | Mints | Roundtrips | Win % | Approx SOL | Median hold h |", "|---|---:|---:|---:|---:|---:|---:|"]
    for w in owner.get("wallet_summaries") or []:
        lines.append(f"| {w.get('label')} | {w.get('failed_pct')} | {w.get('unique_mints_touched')} | {w.get('roundtrip_mints')} | {w.get('approx_win_rate_on_roundtrips_pct')} | {w.get('approx_realized_sol_sample')} | {w.get('median_hold_hours_sample')} |")
    lines += ["", "## Cross-wallet mints", "", "| Mint | Wallets | Approx SOL | Buys/Sells |", "|---|---:|---:|---:|"]
    for m in (owner.get("cross_wallet_mints") or [])[:15]:
        lines.append(f"| `{m['mint'][:6]}…{m['mint'][-4:]}` | {m['wallet_count']} | {m['approx_pnl_sol']} | {m['buys']}/{m['sells']} |")
    lines += ["", "## Rules to Test", "", "1. Compare candidates with the own-wallet summary when one is given: hold time, failed-transaction share, and overlap across wallets.", "2. Mark a runner as real only when current MC/liquidity/volume confirms it; avoid social-only runner claims.", "3. Promote fresh pairs only when source rails converge: pump traction + timing seen in the own-wallet summary OR wallet cluster + liquidity.", "4. Penalize candidates that show loss patterns: repeated failed attempts, high concentration, no exit liquidity, or stale multi-wallet overlap."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--owner-analysis", type=Path, default=None, help="Optional JSON summary of your own wallets, kept outside the package; its content is never packaged.")
    args = ap.parse_args()
    candidates, stats = review_candidates(args.limit)
    payload = {"ok": True, "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "candidate_stats": stats, "candidates": candidates, "owner": owner_patterns(args.owner_analysis), "boundary": "read-only; no execution"}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jp = REPORT_DIR / f"runner_reality_review_{stamp}.json"
    mp = REPORT_DIR / f"runner_reality_review_{stamp}.md"
    jp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=lambda o: sorted(o) if isinstance(o, set) else str(o)), encoding="utf-8")
    mp.write_text(render_md(payload), encoding="utf-8")
    print(json.dumps({"ok": True, "json": str(jp), "md": str(mp), "candidate_stats": stats, "top_real": [x for x in candidates if x['real_runner_candidate']][:10]}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
