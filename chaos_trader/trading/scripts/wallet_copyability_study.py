#!/usr/bin/env python3
"""Copyability study for wallet edge ledgers.

Reads wallet_edge_study JSON and enriches tokens with GeckoTerminal pools/OHLCV.
Simulates delayed follower entries using minute OHLCV after wallet first_seen.
Read-only. No signing/trading.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home

ROOT = chaos_home() / "trading"
DELAYS = [30, 120, 300, 600]
HORIZONS = [900, 1800, 3600, 7200]


def http_json(url: str, timeout: int = 20, retries: int = 2) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ChaosWalletCopyability/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            msg = str(exc)
            if "429" not in msg and "Too Many" not in msg and attempt >= 1:
                break
            time.sleep(min(12.0, 1.5 * (2 ** attempt)))
    raise last or RuntimeError("http_json failed")


def gecko_token(mint: str) -> dict[str, Any]:
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{urllib.parse.quote(mint)}"
    try:
        return http_json(url)
    except Exception as exc:
        return {"error": str(exc)[:200]}


def gecko_pools(mint: str) -> dict[str, Any]:
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{urllib.parse.quote(mint)}/pools"
    try:
        return http_json(url)
    except Exception as exc:
        return {"error": str(exc)[:200], "data": []}


def gecko_ohlcv(pool: str, limit: int = 1000) -> list[list[float]]:
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{urllib.parse.quote(pool)}/ohlcv/minute?aggregate=1&limit={limit}"
    try:
        data = http_json(url, timeout=25)
        rows = (((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        # GT returns newest first; sort ascending.
        return sorted(rows, key=lambda r: r[0])
    except Exception:
        return []


def parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def pick_pool(pools_payload: dict[str, Any]) -> dict[str, Any] | None:
    pools = pools_payload.get("data") or []
    if not pools:
        return None
    def liq(p: dict[str, Any]) -> float:
        attrs = p.get("attributes") or {}
        try:
            return float(attrs.get("reserve_in_usd") or attrs.get("liquidity_usd") or 0)
        except Exception:
            return 0.0
    return max(pools, key=liq)


def candle_at_or_after(ohlcv: list[list[float]], ts: int) -> list[float] | None:
    for row in ohlcv:
        if int(row[0]) >= ts:
            return row
    return None


def candles_between(ohlcv: list[list[float]], start: int, end: int) -> list[list[float]]:
    return [r for r in ohlcv if start <= int(r[0]) <= end]


def simulate(first_seen: str | None, ohlcv: list[list[float]]) -> dict[str, Any]:
    first_ts = parse_iso(first_seen)
    if not first_ts or not ohlcv:
        return {"ok": False, "reason": "missing_first_seen_or_ohlcv"}
    out: dict[str, Any] = {"ok": True, "first_ts": first_ts, "delays": {}}
    for delay in DELAYS:
        entry_ts = first_ts + delay
        entry = candle_at_or_after(ohlcv, entry_ts)
        if not entry:
            out["delays"][str(delay)] = {"ok": False, "reason": "no_candle_after_delay"}
            continue
        entry_price = float(entry[4])  # close of first available minute after delay
        dres: dict[str, Any] = {
            "ok": True,
            "entry_ts": int(entry[0]),
            "entry_iso": datetime.fromtimestamp(int(entry[0]), timezone.utc).isoformat(),
            "entry_price": entry_price,
        }
        for horizon in HORIZONS:
            window = candles_between(ohlcv, int(entry[0]), int(entry[0]) + horizon)
            if not window or entry_price <= 0:
                dres[f"max_roi_{horizon}s"] = None
                dres[f"min_roi_{horizon}s"] = None
                dres[f"close_roi_{horizon}s"] = None
                continue
            max_high = max(float(r[2]) for r in window)
            min_low = min(float(r[3]) for r in window)
            close = float(window[-1][4])
            dres[f"max_roi_{horizon}s"] = round((max_high / entry_price - 1) * 100, 3)
            dres[f"min_roi_{horizon}s"] = round((min_low / entry_price - 1) * 100, 3)
            dres[f"close_roi_{horizon}s"] = round((close / entry_price - 1) * 100, 3)
        out["delays"][str(delay)] = dres
    return out


def classify(row: dict[str, Any], sim: dict[str, Any], market: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    pnl = float(row.get("clean_realized_pnl_sol") or 0)
    buy = float(row.get("buy_sol") or 0)
    open_cost = float(row.get("open_cost_sol") or 0)
    wins = int(row.get("clean_wins") or 0)
    losses = int(row.get("clean_losses") or 0)
    if pnl > 3:
        reasons.append("wallet_realized_good")
    elif pnl < -2:
        reasons.append("wallet_realized_bad")
    if buy >= 5:
        reasons.append("meaningful_wallet_size")
    if open_cost > buy * 0.5 and buy > 0:
        reasons.append("large_open_bag")
    if wins > losses:
        reasons.append("clean_lots_positive_ratio")
    elif losses > wins:
        reasons.append("clean_lots_negative_ratio")

    copy_ok = 0
    copy_bad = 0
    if sim.get("ok"):
        for delay in ["120", "300"]:
            d = (sim.get("delays") or {}).get(delay) or {}
            roi = d.get("max_roi_3600s")
            dd = d.get("min_roi_3600s")
            close = d.get("close_roi_3600s")
            if roi is not None and roi >= 50:
                copy_ok += 1
            if close is not None and close > 10:
                copy_ok += 1
            if dd is not None and dd <= -35:
                copy_bad += 1
            if close is not None and close <= -25:
                copy_bad += 1
    else:
        reasons.append("copyability_unresolved")

    liq = market.get("liquidity_usd")
    try:
        liq_f = float(liq) if liq is not None else None
    except Exception:
        liq_f = None
    if liq_f is not None and liq_f < 10000:
        reasons.append("thin_current_liquidity")

    if pnl > 0 and copy_ok > copy_bad and open_cost < max(2, buy * 0.4):
        return "copyable_candidate", reasons
    if pnl > 0 and copy_ok >= copy_bad:
        return "watch_candidate", reasons
    if pnl > 0 or copy_ok > 0:
        return "discovery_only", reasons
    return "ignore_or_low_priority", reasons


def main() -> None:
    ap = argparse.ArgumentParser(description="Wallet copyability study")
    ap.add_argument("ledger_json")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--sleep", type=float, default=0.35)
    args = ap.parse_args()

    src = Path(args.ledger_json)
    payload = json.loads(src.read_text())
    ledger = payload.get("ledger") or []
    selected = ledger[: max(1, args.top)]
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(selected, 1):
        mint = row["mint"]
        token_payload: dict[str, Any] = {}
        pools_payload = gecko_pools(mint)
        pool = pick_pool(pools_payload)
        market: dict[str, Any] = {}
        ohlcv: list[list[float]] = []
        if pool:
            attrs = pool.get("attributes") or {}
            pool_addr = attrs.get("address") or (pool.get("id") or "").replace("solana_", "")
            market = {
                "pool": pool_addr,
                "name": attrs.get("name"),
                "symbol_guess": attrs.get("name"),
                "price_usd": attrs.get("token_price_usd") or attrs.get("base_token_price_usd"),
                "liquidity_usd": attrs.get("reserve_in_usd"),
                "fdv_usd": attrs.get("fdv_usd"),
                "market_cap_usd": attrs.get("market_cap_usd"),
                "volume_h24": (attrs.get("volume_usd") or {}).get("h24"),
                "price_change_h24": (attrs.get("price_change_percentage") or {}).get("h24"),
                "pool_created_at": attrs.get("pool_created_at"),
            }
            ohlcv = gecko_ohlcv(pool_addr)
            market["ohlcv_points"] = len(ohlcv)
            if not ohlcv:
                market["ohlcv_error"] = "no_ohlcv_returned"
            time.sleep(args.sleep)
        else:
            market = {"pool": None, "market_error": pools_payload.get("error") or token_payload.get("error")}
        sim = simulate(row.get("first_seen"), ohlcv)
        label, reasons = classify(row, sim, market)
        rows.append({"rank": i, "mint": mint, "wallet": row, "market": market, "copyability": sim, "label": label, "reasons": reasons})

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outdir = ROOT / "alpha" / "wallet_studies" / day
    outdir.mkdir(parents=True, exist_ok=True)
    stem = src.stem.replace("wallet_edge", "wallet_copyability")
    json_path = outdir / f"{stem}.json"
    md_path = outdir / f"{stem}.md"
    out = {"source": str(src), "generated_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
    json_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1

    def short(m: str) -> str:
        return f"{m[:8]}…{m[-4:]}"

    lines = [
        "# Wallet Copyability Study",
        "",
        f"Source: `{src}`",
        "",
        "## Summary",
        "",
    ]
    for k, v in sorted(counts.items()):
        lines.append(f"- {k}: **{v}**")
    lines += [
        "",
        "## Ranked Results",
        "",
        "| Label | Token | Wallet PnL | Buy SOL | Open Cost | +2m max/close 1h | +5m max/close 1h | Liquidity | Why |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        w = r["wallet"]
        d120 = ((r.get("copyability") or {}).get("delays") or {}).get("120") or {}
        d300 = ((r.get("copyability") or {}).get("delays") or {}).get("300") or {}
        m = r.get("market") or {}
        def roi_pair(d: dict[str, Any]) -> str:
            if not d.get("ok"):
                return "?"
            return f"{d.get('max_roi_3600s')} / {d.get('close_roi_3600s')}%"
        liq = m.get("liquidity_usd")
        try:
            liq_s = f"${float(liq):,.0f}" if liq is not None else "?"
        except Exception:
            liq_s = "?"
        lines.append(
            f"| {r['label']} | `{short(r['mint'])}` | {w.get('clean_realized_pnl_sol')} | {w.get('buy_sol')} | {w.get('open_cost_sol')} | {roi_pair(d120)} | {roi_pair(d300)} | {liq_s} | {', '.join(r.get('reasons') or [])} |"
        )
    lines += ["", "## Copy Blocks", "", "Copyable/watch candidates:", "", "```text"]
    lines += [r["mint"] for r in rows if r["label"] in {"copyable_candidate", "watch_candidate"}]
    lines += ["```", "", "Discovery-only candidates:", "", "```text"]
    lines += [r["mint"] for r in rows if r["label"] == "discovery_only"]
    lines += ["```", "", "## Method", "", "Uses GeckoTerminal current top pool and minute OHLCV. Simulates follower entries at +30s, +2m, +5m, +10m after wallet first_seen, then computes max/min/close ROI over 15m/30m/1h/2h. This is an approximation; pre-graduation pump curve data can be missing if no OHLCV exists before pool creation."]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"ok": True, "json": str(json_path), "md": str(md_path), "counts": counts, "top_labels": [{"mint": r["mint"], "label": r["label"], "reasons": r["reasons"]} for r in rows[:10]]}, indent=2))


if __name__ == "__main__":
    main()
