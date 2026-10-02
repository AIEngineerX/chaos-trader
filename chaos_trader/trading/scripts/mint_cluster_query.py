#!/usr/bin/env python3
"""Multi-source mint cluster query for Chaos smart-wallet ledger.

Given a mint, combine:
- Secondary-export historical buyer/seller cohorts/events
- actor/overlap graph evidence
- token signal/concentration data
- optional Helius enrichment status already in DB

Read-only by default. Optional --enrich-wallets invokes local read-only Helius tracker
for involved wallets; still no trading/signing/alerts/posting.
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from holder_resolver import resolve_holders
from smart_wallet_tracker import ONCHAIN_SOURCE_IDS

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
TRACKER = Path(__file__).resolve().parent / "smart_wallet_tracker.py"
OUT_DIR = PROFILE_HOME / "trading" / "alpha" / "cluster_queries"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short(x: str | None) -> str:
    if not x:
        return ""
    return x[:6] + "…" + x[-4:]


def jload(s: Any, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def q1(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def qrows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def latest_wallet_score(con: sqlite3.Connection, wallet: str, *sources: str) -> dict[str, Any] | None:
    if sources:
        marks = ",".join("?" for _ in sources)
        return q1(con, f"SELECT * FROM wallet_scores WHERE wallet=? AND source_id IN ({marks}) ORDER BY scored_at DESC, id DESC LIMIT 1", (wallet, *sources))
    return q1(con, "SELECT * FROM wallet_scores WHERE wallet=? ORDER BY scored_at DESC, id DESC LIMIT 1", (wallet,))


def actor_for_wallet(con: sqlite3.Connection, wallet: str) -> dict[str, Any] | None:
    return q1(con, "SELECT * FROM actors WHERE primary_wallet=? LIMIT 1", (wallet,))


def cluster_strength(con: sqlite3.Connection, wallets: list[str]) -> dict[str, Any]:
    if len(wallets) < 2:
        return {"edge_count": 0, "max_shared_mints": 0, "avg_shared_mints": 0, "edges": []}
    ph = ",".join("?" for _ in wallets)
    rows = qrows(
        con,
        f"""
        SELECT e.src_wallet, e.dst_wallet, e.shared_mints, wa.handle a_handle, wb.handle b_handle
        FROM wallet_edges e
        LEFT JOIN wallets wa ON wa.address=e.src_wallet
        LEFT JOIN wallets wb ON wb.address=e.dst_wallet
        WHERE e.edge_type='shared_mint_overlap'
          AND e.src_wallet IN ({ph}) AND e.dst_wallet IN ({ph})
        ORDER BY COALESCE(e.shared_mints,0) DESC
        LIMIT 25
        """,
        tuple(wallets + wallets),
    )
    vals = [r.get("shared_mints") or 0 for r in rows]
    return {
        "edge_count": len(rows),
        "max_shared_mints": max(vals) if vals else 0,
        "avg_shared_mints": round(sum(vals) / len(vals), 3) if vals else 0,
        "edges": rows[:10],
    }


def optionally_enrich(wallets: list[str], limit: int, pages: int) -> list[dict[str, Any]]:
    out = []
    for w in wallets:
        cp = subprocess.run(
            [sys.executable, str(TRACKER), w, "--limit", str(limit), "--pages", str(pages), "--no-wallet-api"],
            cwd=str(PROFILE_HOME),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=240,
        )
        if cp.returncode:
            out.append({"wallet": w, "ok": False, "error": (cp.stderr or cp.stdout)[-500:]})
            continue
        try:
            payload = json.loads(cp.stdout)
            out.extend(payload.get("results", []))
        except Exception as exc:
            out.append({"wallet": w, "ok": False, "error": str(exc)})
    return out


def score_mint(con: sqlite3.Connection, mint: str, *, enrich_wallets: bool = False, enrich_limit: int = 60, enrich_pages: int = 1, max_enrich_wallets: int = 5) -> dict[str, Any]:
    token = q1(con, "SELECT * FROM tokens WHERE mint=?", (mint,)) or {"mint": mint}
    events = qrows(
        con,
        """
        SELECT e.*, w.handle, w.display_name
        FROM wallet_token_events e
        LEFT JOIN wallets w ON w.address=e.wallet
        WHERE e.mint=?
        ORDER BY e.block_time_utc ASC, e.id ASC
        """,
        (mint,),
    )
    secondary_events = [e for e in events if e.get("source_id") == "secondary_export"]
    helius_events = [e for e in events if e.get("source_id") in ONCHAIN_SOURCE_IDS]
    buyers = []
    sellers = []
    first_seen: dict[str, dict[str, Any]] = {}
    for e in secondary_events:
        w = e.get("wallet")
        if not w:
            continue
        if w not in first_seen:
            first_seen[w] = e
        if e.get("event_type") == "buy":
            buyers.append(e)
        elif e.get("event_type") == "sell":
            sellers.append(e)
    buyer_wallets = sorted({e["wallet"] for e in buyers if e.get("wallet")})
    seller_wallets = sorted({e["wallet"] for e in sellers if e.get("wallet")})
    first_buys = []
    seen = set()
    for e in buyers:
        w = e.get("wallet")
        if w and w not in seen:
            seen.add(w)
            first_buys.append(e)
        if len(first_buys) >= 12:
            break

    # Actor evidence.
    actor_rows = []
    actor_labels = Counter()
    for e in first_buys:
        w = e["wallet"]
        a = actor_for_wallet(con, w)
        ws_h = latest_wallet_score(con, w, *ONCHAIN_SOURCE_IDS)
        ws_o = latest_wallet_score(con, w, "secondary_export")
        if a:
            actor_labels[a.get("classification") or "unknown"] += 1
        actor_rows.append({
            "wallet": w,
            "handle": e.get("handle") or e.get("display_name"),
            "time": e.get("block_time_utc"),
            "market_cap_usd": e.get("market_cap_usd"),
            "amount_sol": e.get("amount_sol"),
            "actor_label": a.get("classification") if a else None,
            "actor_score": (ws_h or {}).get("actor_score") or (ws_o or {}).get("actor_score"),
            "helius_copyability": (ws_h or {}).get("copyability"),
            "helius_contamination": (ws_h or {}).get("transfer_contamination_score"),
        })

    signals = qrows(con, "SELECT * FROM token_signals WHERE mint=? ORDER BY captured_at_utc DESC, id DESC", (mint,))
    conc = q1(con, "SELECT * FROM token_concentration_snapshots WHERE mint=? ORDER BY snapshot_at_utc DESC, id DESC LIMIT 1", (mint,))
    try:
        holder_resolution = resolve_holders(mint, 20)
        holder_resolution_error = None
    except BaseException as exc:
        holder_resolution = None
        holder_resolution_error = str(exc)
    cluster = cluster_strength(con, buyer_wallets)

    if enrich_wallets and first_buys:
        enrich_targets = [e["wallet"] for e in first_buys[:max_enrich_wallets] if e.get("wallet")]
        enrich_results = optionally_enrich(enrich_targets, enrich_limit, enrich_pages)
        # reload Helius fields after enrichment
        for row in actor_rows:
            ws_h = latest_wallet_score(con, row["wallet"], *ONCHAIN_SOURCE_IDS)
            if ws_h:
                row["helius_copyability"] = ws_h.get("copyability")
                row["helius_contamination"] = ws_h.get("transfer_contamination_score")
                row["helius_score"] = ws_h.get("score")
                row["helius_pnl_sol"] = ws_h.get("realized_pnl_sol")
    else:
        enrich_results = []

    # Multi-source score. Secondary evidence is one rail; Helius and structure can veto.
    score = 0.0
    positives: list[str] = []
    negatives: list[str] = []
    hard_flags: list[str] = []

    buyer_count = len(buyer_wallets)
    seller_count = len(seller_wallets)
    if buyer_count >= 10:
        score += 18; positives.append("many_tracked_buyers")
    elif buyer_count >= 5:
        score += 14; positives.append("multi_wallet_buy_strong")
    elif buyer_count >= 3:
        score += 9; positives.append("multi_wallet_buy")
    elif buyer_count >= 1:
        score += 3; positives.append("single_tracked_buyer")
    else:
        hard_flags.append("no_tracked_buyers")

    if seller_count >= 3:
        score += 8; positives.append("tracked_sellers_visible")
    elif buyer_count >= 5 and seller_count == 0:
        score -= 10; negatives.append("buyers_without_visible_exits")

    strong_actor_count = sum(actor_labels[x] for x in ["copyable-candidate", "deep-watch", "high-churn-scout"])
    if strong_actor_count >= 3:
        score += 16; positives.append("multiple_strong_actor_buyers")
    elif strong_actor_count >= 1:
        score += 8; positives.append("strong_actor_buyer_present")

    if cluster["edge_count"] >= 3:
        score += 12; positives.append("buyer_cluster_overlap")
    elif cluster["edge_count"] >= 1:
        score += 6; positives.append("some_buyer_overlap")

    if signals:
        sig = signals[0]
        ath = sig.get("ath_multiplier")
        tg = sig.get("tg_channel_count") or 0
        call_mc = sig.get("call_market_cap_usd")
        current_mc = sig.get("current_market_cap_usd")
        if ath is not None and ath >= 2:
            score += 10; positives.append("historical_followthrough")
        if call_mc is not None:
            if 50_000 <= call_mc <= 300_000:
                score += 8; positives.append("good_call_market_cap_band")
            elif call_mc > 750_000:
                score -= 10; negatives.append("late_call_market_cap")
        if current_mc and call_mc and current_mc > call_mc * 4:
            score -= 8; negatives.append("already_far_above_call")
        if tg >= 6 and buyer_count < 5:
            score -= 6; negatives.append("tg_attention_without_wallet_depth")
        elif tg >= 3:
            score += 3; positives.append("secondary_social_confirmation")
    else:
        negatives.append("no_token_signal_row")

    concentration_source = "none"
    if holder_resolution and not holder_resolution.get("holder_data"):
        concentration_source = "holder_resolver_adjusted"
        sp = holder_resolution.get("adjusted_discretionary_pct")
        unknown_pct = holder_resolution.get("unknown_pct") or 0
        if sp is not None:
            if sp >= 30:
                score -= 20; hard_flags.append("toxic_discretionary_concentration"); negatives.append("toxic_discretionary_concentration")
            elif sp >= 20:
                score -= 10; negatives.append("elevated_discretionary_concentration")
            elif sp <= 12:
                score += 5; positives.append("acceptable_adjusted_concentration")
        if unknown_pct >= 10:
            negatives.append("large_unknown_holder_bucket")
    elif conc:
        concentration_source = "secondary_snapshot_unclassified"
        sp = conc.get("supply_pct")
        holders = conc.get("holder_count")
        negatives.append("holder_resolution_unavailable_using_unclassified_snapshot")
        if sp is not None:
            if sp >= 30:
                score -= 12; hard_flags.append("raw_concentration_needs_resolution"); negatives.append("raw_supply_concentration_high_unclassified")
            elif sp >= 20:
                score -= 5; negatives.append("raw_supply_concentration_elevated_unclassified")
            elif sp <= 12:
                positives.append("raw_concentration_appears_low_unclassified")
        if holders and holders < 40:
            score -= 5; negatives.append("thin_holder_base")
    else:
        negatives.append("no_concentration_snapshot")
        if holder_resolution_error:
            negatives.append("holder_resolution_failed")

    hel_rows = [r for r in actor_rows if r.get("helius_copyability")]
    if hel_rows:
        cleanish = [r for r in hel_rows if (r.get("helius_contamination") is not None and r.get("helius_contamination") <= 0.35)]
        studies = [r for r in hel_rows if r.get("helius_copyability") == "study"]
        avoids = [r for r in hel_rows if r.get("helius_copyability") == "avoid"]
        if cleanish:
            score += 8; positives.append("helius_cleanish_wallet_present")
        if studies:
            score += 3; positives.append("helius_study_wallet_present")
        if avoids and len(avoids) >= max(2, len(hel_rows)//2):
            score -= 12; negatives.append("helius_recent_wallets_weak")
    else:
        negatives.append("no_helius_wallet_confirmation_yet")

    score = max(0.0, min(100.0, round(score, 3)))
    if "toxic_discretionary_concentration" in hard_flags:
        verdict = "avoid"
    elif score >= 70 and not hard_flags:
        verdict = "paper-plan-candidate"
    elif score >= 52:
        verdict = "deep-check"
    elif score >= 30:
        verdict = "watch"
    else:
        verdict = "ignore"

    result = {
        "ok": True,
        "mode": "multi_source_mint_cluster_query",
        "generated_at": now_utc(),
        "mint": mint,
        "symbol": token.get("symbol"),
        "name": token.get("name"),
        "score": score,
        "verdict": verdict,
        "positives": positives,
        "negatives": negatives,
        "hard_flags": hard_flags,
        "metrics": {
            "secondary_events": len(secondary_events),
            "helius_events_in_db": len(helius_events),
            "tracked_buyer_wallets": buyer_count,
            "tracked_seller_wallets": seller_count,
            "strong_actor_buyers": strong_actor_count,
            "cluster_edge_count": cluster["edge_count"],
            "max_shared_mints_between_buyers": cluster["max_shared_mints"],
            "latest_supply_pct": (conc or {}).get("supply_pct"),
            "concentration_source": concentration_source,
            "raw_top_holder_pct": (holder_resolution or {}).get("raw_top_pct"),
            "lp_pool_pct": (holder_resolution or {}).get("lp_pool_pct"),
            "adjusted_discretionary_pct": (holder_resolution or {}).get("adjusted_discretionary_pct"),
            "unknown_holder_pct": (holder_resolution or {}).get("unknown_pct"),
            "latest_holder_count": (conc or {}).get("holder_count"),
            "ath_multiplier": (signals[0].get("ath_multiplier") if signals else None),
            "tg_channel_count": (signals[0].get("tg_channel_count") if signals else None),
            "call_market_cap_usd": (signals[0].get("call_market_cap_usd") if signals else None),
            "current_market_cap_usd": (signals[0].get("current_market_cap_usd") if signals else None),
        },
        "first_buyers": actor_rows[:12],
        "cluster_edges": cluster["edges"],
        "signals": signals[:3],
        "latest_concentration": conc,
        "holder_resolution": holder_resolution,
        "holder_resolution_error": holder_resolution_error,
        "enrich_results": enrich_results,
        "boundary": "research only; no execution/alerts/signing/posting",
    }
    return result


def render_md(result: dict[str, Any]) -> str:
    m = result["metrics"]
    lines = [
        f"# Mint Cluster Query — {result.get('symbol') or result['mint']}",
        "",
        f"Generated: {result['generated_at']}",
        f"Mint: `{result['mint']}`",
        f"Verdict: **{result['verdict']}**",
        f"Score: `{result['score']}`",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for k, v in m.items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## Positives", ""] + [f"- {x}" for x in result["positives"] or ["none"]]
    lines += ["", "## Negatives", ""] + [f"- {x}" for x in result["negatives"] or ["none"]]
    lines += ["", "## First buyers", "", "| Handle | Wallet | Actor | Helius | MC | SOL | Time |", "|---|---|---|---|---:|---:|---|"]
    for r in result["first_buyers"][:12]:
        lines.append(f"| {r.get('handle') or ''} | `{short(r.get('wallet'))}` | {r.get('actor_label') or ''} | {r.get('helius_copyability') or ''} | {r.get('market_cap_usd')} | {r.get('amount_sol')} | {r.get('time')} |")
    lines += ["", "## Cluster edges", "", "| A | B | Shared mints |", "|---|---|---:|"]
    for e in result["cluster_edges"][:10]:
        lines.append(f"| {e.get('a_handle') or short(e.get('src_wallet'))} | {e.get('b_handle') or short(e.get('dst_wallet'))} | {e.get('shared_mints')} |")
    lines += ["", "## Answer", "", f"{result['verdict']}. Secondary evidence is one rail only; require Helius/live confirmation before using."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-source mint cluster query")
    ap.add_argument("mint")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--enrich-wallets", action="store_true", help="Run read-only Helius tracker on first buyers")
    ap.add_argument("--max-enrich-wallets", type=int, default=5)
    ap.add_argument("--enrich-limit", type=int, default=60)
    ap.add_argument("--enrich-pages", type=int, default=1)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db).expanduser())
    con.row_factory = sqlite3.Row
    result = score_mint(con, args.mint, enrich_wallets=args.enrich_wallets, enrich_limit=args.enrich_limit, enrich_pages=args.enrich_pages, max_enrich_wallets=args.max_enrich_wallets)
    con.close()
    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"{args.mint}.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
        (OUT_DIR / f"{args.mint}.md").write_text(render_md(result))
    if args.raw:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(render_md(result))


if __name__ == "__main__":
    main()
