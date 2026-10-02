#!/usr/bin/env python3
"""Read-only Chaos trending-token sweep.

Fetches capped Dexscreener boost/profile candidates, ranks Solana token mints,
runs the token event analyzer on the top slice, and writes sweep artifacts.
Designed for manual dry-runs first and hourly cron later.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from dexscreener_client import fetch_token  # noqa: E402
from fast_lane_writers import write_ranked  # noqa: E402
from helius_common import safe_print  # noqa: E402
from signal_ledger import record_signal  # noqa: E402
import x_provider  # noqa: E402
from token_event_analyzer import analyze  # noqa: E402

OUT_ROOT = PROFILE_HOME / "trading" / "alpha" / "sweeps"
DEX_BASE = "https://api.dexscreener.com"
UA = "ChaosTrendingSweep/1.0 read-only"
BOUNDARY = "read-only research sweep; no alerts/trading/execution/posting"
TRAP_LABEL_TERMS = ("avoid", "exit", "ignore", "failed", "dead/fake")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def fetch_json(path: str, timeout: int = 20) -> tuple[Any | None, str | None]:
    url = DEX_BASE + path
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:
        return None, str(exc)


def normalize_rows(payload: Any, source: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("pairs") or payload.get("tokens") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        chain = row.get("chainId") or row.get("chain")
        token = row.get("tokenAddress") or row.get("address") or row.get("baseToken", {}).get("address")
        if chain != "solana" or not token:
            continue
        out.append({
            "source": source,
            "mint": token,
            "url": row.get("url"),
            "description": row.get("description"),
            "icon": row.get("icon"),
            "header": row.get("header"),
            "boost_amount": row.get("amount"),
            "boost_total_amount": row.get("totalAmount"),
            "raw": row,
        })
    return out


def discovery_candidates() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    endpoints = {
        "boosts_top": "/token-boosts/top/v1",
        "boosts_latest": "/token-boosts/latest/v1",
        "profiles_latest": "/token-profiles/latest/v1",
    }
    candidates: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    counts: dict[str, int] = {}
    for source, path in endpoints.items():
        payload, err = fetch_json(path)
        if err:
            errors[source] = err
            continue
        rows = normalize_rows(payload, source)
        counts[source] = len(rows)
        for row in rows:
            mint = row["mint"]
            existing = candidates.setdefault(mint, {"mint": mint, "sources": [], "raw_candidates": []})
            existing["sources"].append(source)
            existing["raw_candidates"].append(row)
            existing["url"] = existing.get("url") or row.get("url")
            existing["boost_total_amount"] = max(float(existing.get("boost_total_amount") or 0), float(row.get("boost_total_amount") or 0))
            existing["boost_amount"] = max(float(existing.get("boost_amount") or 0), float(row.get("boost_amount") or 0))
    return list(candidates.values()), {"endpoint_counts": counts, "endpoint_errors": errors}


def market_score(summary: dict[str, Any]) -> float:
    def f(v: Any) -> float:
        try:
            return float(v or 0)
        except Exception:
            return 0.0
    liq = f(summary.get("liquidity_usd"))
    vol_h1 = f(summary.get("volume_h1"))
    vol_h24 = f(summary.get("volume_h24"))
    tx_h1 = summary.get("txns_h1") or {}
    tx_h24 = summary.get("txns_h24") or {}
    buys_h1 = f(tx_h1.get("buys") if isinstance(tx_h1, dict) else 0)
    sells_h1 = f(tx_h1.get("sells") if isinstance(tx_h1, dict) else 0)
    buys_h24 = f(tx_h24.get("buys") if isinstance(tx_h24, dict) else 0)
    return liq * 0.08 + vol_h1 * 0.7 + vol_h24 * 0.03 + (buys_h1 + sells_h1) * 14 + buys_h24 * 1.5


def deep_read_gate_label(read: dict[str, Any]) -> str:
    """Return the strongest available human-facing gate label for a deep read."""
    entry_gate = read.get("entry_gate") or {}
    gate = read.get("gate") or {}
    cls = read.get("classification") or {}
    return str(entry_gate.get("action") or gate.get("gate") or cls.get("verdict") or "").strip().lower()


def is_trap_read(read: dict[str, Any]) -> bool:
    """Classify avoid/exit-liquidity deep reads as trap-radar material, not alpha candidates."""
    if read.get("error"):
        return False
    label = deep_read_gate_label(read)
    cls = read.get("classification") or {}
    legacy = str(cls.get("verdict") or "").strip().lower()
    phase = str(cls.get("attention_phase") or "").strip().lower()
    haystack = " ".join([label, legacy, phase])
    return any(term in haystack for term in TRAP_LABEL_TERMS)


def should_display_deep_read(read: dict[str, Any], mode: str) -> bool:
    """Fail closed: default alpha sweeps do not present traps as top opportunities."""
    trap = is_trap_read(read)
    if mode == "trap":
        return trap
    if mode == "all":
        return True
    return not trap


def compact_filtered_read(read: dict[str, Any]) -> dict[str, Any]:
    """Keep enough filtered-trap context for artifacts without crowding compact output for CLI and chat callers."""
    market = read.get("market") or {}
    cls = read.get("classification") or {}
    flow = cls.get("flow") or {}
    return {
        "mint": read.get("mint"),
        "symbol": market.get("symbol"),
        "label": deep_read_gate_label(read) or cls.get("verdict"),
        "verdict": cls.get("verdict"),
        "attention_phase": cls.get("attention_phase"),
        "why_not_watch": cls.get("why_not_watch"),
        "risk_flags": (cls.get("risk_flags") or [])[:4],
        "flow": {
            "volume_liquidity_ratio": flow.get("volume_liquidity_ratio"),
            "avg_tx_usd": flow.get("avg_tx_usd"),
            "tx_count": flow.get("tx_count"),
            "flags": (flow.get("flags") or [])[:3],
        },
        "markdown_path": read.get("markdown_path"),
    }


def enrich(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = []
    for c in candidates[: max(limit * 3, limit)]:
        mint = c["mint"]
        try:
            dex = fetch_token("solana", mint, cache=True)
            summary = dex.get("summary") or {}
            pair_count = dex.get("pair_count")
            err = None
        except Exception as exc:
            summary = {}
            pair_count = 0
            err = str(exc)
        boost_score = float(c.get("boost_total_amount") or 0) * 3 + float(c.get("boost_amount") or 0)
        source_score = len(set(c.get("sources") or [])) * 25
        score = market_score(summary) + boost_score + source_score
        rows.append({
            "mint": mint,
            "sources": sorted(set(c.get("sources") or [])),
            "discovery_url": c.get("url"),
            "boost_total_amount": c.get("boost_total_amount"),
            "boost_amount": c.get("boost_amount"),
            "pair_count": pair_count,
            "summary": summary,
            "candidate_score": round(score, 4),
            "error": err,
        })
        time.sleep(0.12)
    rows.sort(key=lambda r: r.get("candidate_score") or 0, reverse=True)
    return rows[:limit]


def render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Chaos Trending Token Sweep",
        "",
        f"Generated: {payload['generated_at']}",
        f"Boundary: {BOUNDARY}",
        "",
        "## Summary",
        "",
        f"- Candidates discovered: {payload['candidate_count']}",
        f"- Ranked candidates: {len(payload['ranked_candidates'])}",
        f"- Deep reads: {len(payload['deep_reads'])}",
        f"- Filtered traps: {len(payload.get('filtered_deep_reads') or [])}",
        f"- Sweep mode: {payload.get('sweep_mode') or 'alpha'}",
        f"- X enabled: {payload['x_enabled']}",
        "",
        "## Ranked candidates",
        "",
        "| Rank | Score | Token | Mint | Sources | Liq | MC | h1 Vol | h1 Txns | Link |",
        "|---:|---:|---|---|---|---:|---:|---:|---|---|",
    ]
    for i, c in enumerate(payload.get("ranked_candidates") or [], 1):
        s = c.get("summary") or {}
        tx = s.get("txns_h1") or {}
        txs = ""
        if isinstance(tx, dict):
            txs = f"{tx.get('buys',0)}/{tx.get('sells',0)}"
        lines.append(
            f"| {i} | {c.get('candidate_score')} | {s.get('dexId') or ''}:{s.get('priceUsd') or ''} | `{c['mint']}` | {','.join(c.get('sources') or [])} | {s.get('liquidity_usd')} | {s.get('marketCap')} | {s.get('volume_h1')} | {txs} | {s.get('url') or c.get('discovery_url') or ''} |"
        )
    if payload.get("deep_reads"):
        lines += ["", "## Deep reads", ""]
        for r in payload["deep_reads"]:
            cls = r.get("classification") or {}
            market = r.get("market") or {}
            lines.append(f"- **{market.get('symbol') or 'UNKNOWN'}** `{r['mint']}` — {cls.get('verdict')} / {cls.get('attention_phase')} / score {cls.get('score')} — {r.get('markdown_path')}")
    if payload.get("filtered_deep_reads"):
        lines += ["", "## Filtered trap-radar reads", ""]
        for r in payload["filtered_deep_reads"]:
            lines.append(f"- **{r.get('symbol') or 'UNKNOWN'}** `{r.get('mint')}` — {r.get('label') or r.get('verdict')} — {r.get('why_not_watch') or 'filtered from alpha mode'}")
    lines += ["", "## Non-goals", "", "- No trade execution, no wallet action, no public/social action.", "- Hourly cron should stay local-only until thresholds prove useful.", ""]
    return "\n".join(lines)


def candidate_signal_payload(candidate: dict[str, Any], generated_at: str, json_path: Path, md_path: Path) -> dict[str, Any]:
    """Convert a ranked-but-not-deep-read sweep candidate into a low-grade signal row."""
    s = candidate.get("summary") or {}
    return {
        "ok": True,
        "mode": "chaos_sweep_ranked_candidate",
        "generated_at": generated_at,
        "mint": candidate.get("mint"),
        "fact_grade": "D",
        "market": {
            "dex_id": s.get("dexId"),
            "url": s.get("url") or candidate.get("discovery_url"),
            "price_usd": s.get("priceUsd"),
            "fdv": s.get("fdv"),
            "market_cap": s.get("marketCap"),
            "liquidity_usd": s.get("liquidity_usd"),
            "volume_h1": s.get("volume_h1"),
            "volume_h24": s.get("volume_h24"),
            "txns_h1": s.get("txns_h1"),
            "txns_h24": s.get("txns_h24"),
            "price_change_h1": s.get("priceChange_h1"),
            "price_change_h24": s.get("priceChange_h24"),
        },
        "classification": {
            "verdict": "sweep-candidate-unread",
            "attention_phase": "unread",
            "score": candidate.get("candidate_score"),
            "risk_flags": ["ranked by Dexscreener trend/boost surface only; not a deep token read"],
        },
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "boundary": BOUNDARY,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only trending token sweep for Chaos. No execution.")
    p.add_argument("--limit", type=int, default=10, help="Max ranked trending candidates")
    p.add_argument("--deep", type=int, default=3, help="Max candidates to run through token_event_analyzer")
    p.add_argument("--tx-limit", type=int, default=20, help="Helius sample limit per deep read")
    p.add_argument("--x", action="store_true", help="Include x_search attention scan on deep reads")
    p.add_argument("--x-days", type=int, default=2)
    p.add_argument("--out-dir", help="Artifact output directory")
    p.add_argument("--raw", action="store_true", help="Print full JSON")
    p.add_argument(
        "--mode",
        choices=("alpha", "trap", "all"),
        default="alpha",
        help="alpha hides avoid/exit traps; trap shows only trap-radar reads; all shows every deep read",
    )
    args = p.parse_args()
    x_on = bool(args.x) and x_provider.provider_name() != "none"  # D3: no provider is X off
    if args.x and not x_on:
        print(x_provider.no_provider_notice(), file=sys.stderr)

    limit = max(1, min(25, args.limit))
    deep = max(0, min(args.deep, min(5, limit)))
    tx_limit = max(1, min(100, args.tx_limit))
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else OUT_ROOT / now_utc().strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_at = now_utc().isoformat(timespec="seconds")
    candidates, meta = discovery_candidates()
    ranked = enrich(candidates, limit)
    deep_reads = []
    filtered_deep_reads = []
    for c in ranked:
        if len(deep_reads) >= deep:
            break
        try:
            r = analyze(c["mint"], tx_limit=tx_limit, x_enabled=x_on, x_days=max(1, min(14, args.x_days)), out_dir=out_dir, source_command=f"sweep_deep_{args.mode}")
            # Keep sweep JSON smaller; full artifacts are written separately.
            read = {
                "mint": r["mint"],
                "market": r.get("market"),
                "classification": r.get("classification"),
                "fact_grade": r.get("fact_grade"),
                "mode_context": r.get("mode_context"),
                "gate": r.get("gate"),
                "secondary_evidence": r.get("secondary_evidence"),
                "ledger": r.get("ledger"),
                "entry_gate": r.get("entry_gate"),
                "position_context": r.get("position_context"),
                "wallet_timing": r.get("wallet_timing"),
                "social_catalyst": r.get("social_catalyst"),
                "flow_conversion": r.get("flow_conversion"),
                "delta": r.get("delta"),
                "owner_exposure": r.get("owner_exposure"),
                "x_enabled": r.get("x_enabled"),
                "x_citation_count": len(((r.get("x_attention") or {}).get("citations") or [])) + len(((r.get("x_attention") or {}).get("inline_citations") or [])) if isinstance(r.get("x_attention"), dict) else 0,
                "json_path": r.get("json_path"),
                "markdown_path": r.get("markdown_path"),
                "card": r.get("card"),
            }
            if should_display_deep_read(read, args.mode):
                deep_reads.append(read)
            else:
                filtered_deep_reads.append(compact_filtered_read(read))
        except Exception as exc:
            read = {"mint": c["mint"], "error": str(exc)}
            if should_display_deep_read(read, args.mode):
                deep_reads.append(read)
            else:
                filtered_deep_reads.append(compact_filtered_read(read))

    prefix = f"trending_token_sweep_{stamp()}"
    json_path = out_dir / f"{prefix}.json"
    md_path = out_dir / f"{prefix}.md"
    deep_mints = {str(r.get("mint")) for r in deep_reads if r.get("mint") and not r.get("error")}
    candidate_ledgers = []
    for c in ranked:
        if str(c.get("mint")) in deep_mints:
            continue
        try:
            candidate_ledgers.append(record_signal(candidate_signal_payload(c, generated_at, json_path, md_path), source_command="sweep_candidate"))
        except Exception as exc:
            candidate_ledgers.append({"ok": False, "mint": c.get("mint"), "error": str(exc)})
    # After the deep reads, so a mint they read uses the holder sample they cached.
    fast_lane = write_ranked(ranked)

    payload = {
        "ok": True,
        "mode": "chaos_readonly_trending_token_sweep",
        "generated_at": generated_at,
        "boundary": BOUNDARY,
        "candidate_count": len(candidates),
        "discovery": meta,
        "ranked_candidates": ranked,
        "deep_reads": deep_reads,
        "filtered_deep_reads": filtered_deep_reads,
        "candidate_ledgers": candidate_ledgers,
        "fast_lane": fast_lane,
        "x_enabled": x_on,
        "sweep_mode": args.mode,
        "caps": {"limit": limit, "deep": deep, "tx_limit": tx_limit, "x_days": max(1, min(14, args.x_days))},
    }
    payload["json_path"] = str(json_path)
    payload["markdown_path"] = str(md_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_md(payload), encoding="utf-8")

    if args.raw:
        safe_print(payload)
        return
    print("☄️ Chaos Trending Token Sweep")
    print(f"Mode: {args.mode} · Candidates: {len(candidates)} · ranked={len(ranked)} · deep={len(deep_reads)} · filtered={len(filtered_deep_reads)} · x={x_on}")
    for i, c in enumerate(ranked[: min(5, len(ranked))], 1):
        s = c.get("summary") or {}
        print(f"{i}. `{c['mint']}` score={c.get('candidate_score')} liq={s.get('liquidity_usd')} mc={s.get('marketCap')} src={','.join(c.get('sources') or [])}")
    if deep_reads:
        print("\nDeep reads:")
        for r in deep_reads:
            if r.get("error"):
                print(f"- `{r['mint']}` error={r['error']}")
                continue
            market = r.get("market") or {}
            cls = r.get("classification") or {}
            print(f"- {market.get('symbol') or 'UNKNOWN'} `{r['mint']}` -> {cls.get('verdict')} phase={cls.get('attention_phase')} score={cls.get('score')} x_cites={r.get('x_citation_count')}")
    print(f"\nFast lane: {fast_lane['token_signals']} token signals, {fast_lane['concentration_snapshots']} concentration snapshots")
    print(f"JSON: {json_path}")
    print(f"MD:   {md_path}")
    print("Advisory + paper only. No wallet, signing, routing, or live execution.")


if __name__ == "__main__":
    main()
