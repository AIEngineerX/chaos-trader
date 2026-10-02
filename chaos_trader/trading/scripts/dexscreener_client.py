#!/usr/bin/env python3
"""Read-only Dexscreener client for Chaos.

No execution. Fetches token/pair market context: liquidity, txns, volume, fdv,
pair age, socials, boosts/orders where requested.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
CACHE_DIR = PROFILE_HOME / "trading" / "cache" / "dexscreener"
BASE = "https://api.dexscreener.com"
UA = "ChaosResearch/1.0 read-only"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_json(path: str, timeout: int = 20) -> Any:
    url = BASE + path
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe_cache_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:120]


def cache_path(chain: str, token: str) -> Path:
    return CACHE_DIR / f"{safe_cache_part(chain)}_{safe_cache_part(token)}.json"


def read_fresh_cache(path: Path, ttl_seconds: int | None) -> dict[str, Any] | None:
    if ttl_seconds is None or ttl_seconds <= 0 or not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > ttl_seconds:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["cache"] = {"hit": True, "age_seconds": round(age, 3), "ttl_seconds": ttl_seconds}
            return data
    except Exception:
        return None
    return None


def _pairs_from_payload(data: Any) -> list[dict[str, Any]]:
    """Normalize DexScreener pair payloads from both current endpoints."""
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    if isinstance(data, dict):
        pairs = data.get("pairs")
        if isinstance(pairs, list):
            return [p for p in pairs if isinstance(p, dict)]
    return []


def fetch_token(chain: str, token: str, cache: bool = True, ttl_seconds: int | None = None) -> dict[str, Any]:
    path = cache_path(chain, token)
    if cache:
        cached = read_fresh_cache(path, ttl_seconds)
        if cached is not None:
            return cached

    # DexScreener's /tokens/v1 endpoint can lag or omit the migrated PumpSwap
    # market during pump.fun launches. Pull all known token-pair surfaces and
    # de-dupe by pairAddress so the active liquid pair is available for ranking.
    pairs_by_addr: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    endpoints = [
        f"/latest/dex/tokens/{token}",
        f"/token-pairs/v1/{chain}/{token}",
        f"/tokens/v1/{chain}/{token}",
    ]
    for ep in endpoints:
        try:
            for pair in _pairs_from_payload(get_json(ep)):
                addr = str(pair.get("pairAddress") or "")
                if not addr:
                    continue
                pairs_by_addr[addr] = pair
        except Exception as exc:
            errors.append(f"{ep}: {type(exc).__name__}: {str(exc)[:120]}")

    pairs = list(pairs_by_addr.values())
    pairs_sorted = sorted(pairs, key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0)), reverse=True)
    out = {
        "ok": True,
        "mode": "dexscreener_token_context",
        "generated_at": now(),
        "chain": chain,
        "token": token,
        "pair_count": len(pairs_sorted),
        "pairs": pairs_sorted,
        "summary": summarize_pairs(pairs_sorted),
        "cache": {"hit": False, "ttl_seconds": ttl_seconds},
        "errors": errors,
        "boundary": "read-only market data; not holder truth; reconcile with Helius/onchain.",
    }
    if cache and pairs_sorted:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str))
    return out


def summarize_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    if not pairs:
        return {}
    p = pairs[0]
    liq = p.get("liquidity") or {}
    txns = p.get("txns") or {}
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    return {
        "best_pair": p.get("pairAddress"),
        "dexId": p.get("dexId"),
        "url": p.get("url"),
        "priceUsd": p.get("priceUsd"),
        "fdv": p.get("fdv"),
        "marketCap": p.get("marketCap"),
        "liquidity_usd": liq.get("usd"),
        "liquidity_base": liq.get("base"),
        "liquidity_quote": liq.get("quote"),
        "pairCreatedAt": p.get("pairCreatedAt"),
        "txns_m5": txns.get("m5"),
        "txns_h1": txns.get("h1"),
        "txns_h6": txns.get("h6"),
        "txns_h24": txns.get("h24"),
        "volume_h1": vol.get("h1"),
        "volume_h6": vol.get("h6"),
        "volume_h24": vol.get("h24"),
        "priceChange_m5": pc.get("m5"),
        "priceChange_h1": pc.get("h1"),
        "priceChange_h6": pc.get("h6"),
        "priceChange_h24": pc.get("h24"),
        "has_socials": bool(((p.get("info") or {}).get("socials") or [])),
        "boosts": p.get("boosts"),
    }


# Preferred active DEXes — pump.fun bonding-curve pairs are transient, not tradable.
# Pumpswap, Raydium, and Meteora pairs reflect real post-migration liquidity.
PREFERRED_DEXES = frozenset({"pumpswap", "raydium", "meteora", "orca", "phoenix"})


def _pair_score(pair: dict[str, Any]) -> tuple[float, float, float, int]:
    """Score a pair for ranking: (dex_pref, liquidity, volume, txns).

    Returns a tuple where higher = better ranking. Dex preference is
    binary: preferred dex = 1.0, other = 0.0.
    """
    dex_id = str(pair.get("dexId") or "").lower()
    dex_pref = 1.0 if (dex_id in PREFERRED_DEXES) else 0.0
    liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
    txns_h1_cnt = 0
    t = pair.get("txns") or {}
    if isinstance(t, dict):
        h1 = t.get("h1") or {}
        if isinstance(h1, dict):
            txns_h1_cnt = int(h1.get("buys") or 0) + int(h1.get("sells") or 0)
    return (dex_pref, liq_usd, vol_h1, txns_h1_cnt)


def resolve_best_token_market(mint: str, cache: bool = True, ttl_seconds: int | None = None) -> dict[str, Any]:
    """Fetch all DexScreener pairs and return the best active liquid market.

    Ranks pairs by: dex preference (pumpswap/raydium > pumpfun), liquidity, volume, txns.
    Skips caching when the best pair has zero/none liquidity to avoid stale snapshots.

    Output includes pair_selection_reason and detailed txns_m5/h1 breakdown.
    """
    dex = fetch_token("solana", mint, cache=cache, ttl_seconds=ttl_seconds)
    pairs = dex.get("pairs") or []

    if not pairs:
        return {
            "best_pair": None,
            "dex_id": None,
            "liquidity_usd": None,
            "market_cap": None,
            "volume_m5": None,
            "volume_h1": None,
            "txns_m5": None,
            "txns_h1": None,
            "pair_created_at": None,
            "pair_selection_reason": "no_pairs_found",
            "symbol": dex.get("summary", {}).get("symbol"),
            "name": None,
            "url": None,
            "fdv": None,
            "price_usd": None,
        }

    # Rank all pairs using the scoring tuple
    ranked = sorted(pairs, key=_pair_score, reverse=True)
    best = ranked[0]

    liq = best.get("liquidity") or {}
    txns = best.get("txns") or {}
    vol = best.get("volume") or {}
    base_token = best.get("baseToken") or {}
    info = best.get("info") or {}

    # Build selection reason
    dex_id = str(best.get("dexId") or "").lower()
    liq_usd = float(liq.get("usd") or 0)
    if dex_id in PREFERRED_DEXES and liq_usd > 0:
        reason = "highest_active_liquidity"
    elif len(pairs) > 1:
        reason = "only_pair_with_liquidity" if liq_usd > 0 else "all_pairs_zero_liquidity"
    else:
        reason = "sole_pair_available"

    result = {
        "best_pair": best.get("pairAddress"),
        "dex_id": best.get("dexId"),
        "liquidity_usd": liq_usd if liq_usd > 0 else None,
        "market_cap": best.get("marketCap") or best.get("fdv"),
        "volume_m5": vol.get("m5"),
        "volume_h1": vol.get("h1"),
        "volume_h6": vol.get("h6"),
        "txns_m5": txns.get("m5"),
        "txns_h1": txns.get("h1"),
        "txns_h6": txns.get("h6"),
        "pair_created_at": best.get("pairCreatedAt"),
        "pair_selection_reason": reason,
        "pair_count_total": len(pairs),
        "symbol": base_token.get("symbol") or dex.get("summary", {}).get("symbol"),
        "name": base_token.get("name"),
        "url": best.get("url"),
        "fdv": best.get("fdv"),
        "price_usd": best.get("priceUsd"),
        "socials": info.get("socials") or [],
        "websites": info.get("websites") or [],
        "price_change_m5": (best.get("priceChange") or {}).get("m5"),
        "price_change_h1": (best.get("priceChange") or {}).get("h1"),
        "price_change_h6": (best.get("priceChange") or {}).get("h6"),
        "price_change_h24": (best.get("priceChange") or {}).get("h24"),
    }

    # Do not cache zero-liquidity results — they're transient snapshots
    if not result.get("liquidity_usd"):
        result["_stale_possible"] = True
        result["_cache_skip"] = True

    return result


def render_md(out: dict[str, Any]) -> str:
    s = out.get("summary") or {}
    lines = [
        "## Dexscreener Context",
        f"- Token: `{out['token']}`",
        f"- Pairs: {out['pair_count']}",
        f"- Best pair: `{s.get('best_pair')}` on {s.get('dexId')}",
        f"- Price: {s.get('priceUsd')}",
        f"- FDV / MC: {s.get('fdv')} / {s.get('marketCap')}",
        f"- Liquidity USD: {s.get('liquidity_usd')}",
        f"- 5m txns: {s.get('txns_m5')}",
        f"- 1h txns: {s.get('txns_h1')}",
        f"- 6h volume: {s.get('volume_h6')}",
        f"- 24h price change: {s.get('priceChange_h24')}%",
        "",
        "## Caveat",
        "Dexscreener is market/liquidity context, not holder truth. Reconcile with Helius holder resolver and wallet flow.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only Dexscreener token context")
    p.add_argument("token")
    p.add_argument("--chain", default="solana")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()
    out = fetch_token(args.chain, args.token)
    if args.raw:
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
    else:
        print(render_md(out))


if __name__ == "__main__":
    main()
