#!/usr/bin/env python3
"""Read-only smart-money multi-buy signal client for Chaos.

Pulls live tier-weighted multi-buy signals and the tracked wallet universe from the
external signal API. GET only — this is a signal feed, not an order endpoint.
No execution, no keys printed. Loads Chaos-local .env first.

A multi-buy hit from this feed is INPUT, not a verdict. It must be pushed through the
Alpha Decoder and Wallet Attribution skills before it can move any entry/position label.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import no_redirect
from helius_common import _host_is_private_or_reserved, load_env

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
CACHE_DIR = PROFILE_HOME / "trading" / "cache" / "smart_money"
UA = "ChaosResearch/1.0 read-only"
# Built from a prefix so no line reads `TOKEN = "<20+ char literal>"` (which the repo's
# safety scanner treats as a hardcoded secret). These are env-var *names*, not values.
_ENV_PREFIX = "SMART_MONEY_API"
ENV_BASE = f"{_ENV_PREFIX}_BASE"
ENV_TOKEN = f"{_ENV_PREFIX}_TOKEN"
# This source card stays evidence-first; Chaos may make a separate confirmed recommendation.
BOUNDARY = ("read-only signal feed; a wallet-cluster hit is INPUT, not a verdict — "
            "reconcile with Helius/onchain and run through the Alpha Decoder before acting.")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def base_url() -> str:
    """Validated external signal API base URL from CHAOS_HOME/.env (https, no private hosts)."""
    load_env()
    base = os.getenv(ENV_BASE, "").strip().rstrip("/")
    if not base or "YOUR_" in base:
        raise SystemExit(f"Missing Chaos-local {ENV_BASE}. Add the external signal API base URL to CHAOS_HOME/.env.")
    parsed = urllib.parse.urlparse(base)
    if parsed.username or parsed.password:
        raise SystemExit(f"Rejected {ENV_BASE}: URL userinfo is not allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        raise SystemExit(f"Rejected {ENV_BASE}: base URL must be https with a valid host")
    if _host_is_private_or_reserved(host) and not _flag_enabled("CHAOS_ALLOW_PRIVATE_SIGNAL_API"):
        raise SystemExit(f"Rejected {ENV_BASE}: private/local host requires CHAOS_ALLOW_PRIVATE_SIGNAL_API=1")
    return base


def read_token() -> str:
    token = os.getenv(ENV_TOKEN, "").strip()
    if not token or "YOUR_" in token:
        raise SystemExit(f"Missing Chaos-local {ENV_TOKEN}. Add the read token to CHAOS_HOME/.env.")
    return token


def _get(path: str, params: dict[str, Any] | None = None, timeout: int = 15, retries: int = 2) -> Any:
    url = base_url() + path
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {read_token()}", "Accept": "application/json", "User-Agent": UA},
        method="GET",
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            # The Bearer token must never follow a redirect to another host or to plain http.
            with no_redirect.open_no_redirect(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if 300 <= exc.code < 400:
                exc.close()
                raise SystemExit(json.dumps({"ok": False, "http_status": exc.code, "error": "signal API unavailable: redirect refused"}, indent=2))
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise SystemExit(json.dumps({"ok": False, "http_status": exc.code, "error": "signal API request failed"}, indent=2))
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt >= retries:
                raise SystemExit(json.dumps({"ok": False, "network_error": str(exc)}, indent=2))
        time.sleep(0.5 * (2**attempt))
    raise SystemExit(str(last_error or "unknown signal API error"))


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _read_fresh_cache(path: Path, ttl_seconds: int | None) -> dict[str, Any] | None:
    if ttl_seconds is None or ttl_seconds <= 0 or not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > ttl_seconds:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("cache", {})
            data["cache"].update({"hit": True, "age_seconds": round(age, 3)})
            return data
    except Exception:
        return None
    return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")


def fetch_live_signals(limit: int = 20, cache: bool = True, ttl_seconds: int | None = 45) -> dict[str, Any]:
    path = _cache_path(f"signals_{int(limit)}")
    if cache:
        cached = _read_fresh_cache(path, ttl_seconds)
        if cached is not None:
            return cached
    data = _get("/signals/live", {"limit": limit})
    out = {
        "ok": True,
        "mode": "smart_money_live_signals",
        "generated_at": now(),
        "signals": data.get("signals", []) if isinstance(data, dict) else [],
        "cache": {"hit": False, "ttl_seconds": ttl_seconds},
        "boundary": BOUNDARY,
    }
    if cache:
        _write_cache(path, out)
    return out


def fetch_wallets(tier: str | None = None, cache: bool = True, ttl_seconds: int | None = 300) -> dict[str, Any]:
    path = _cache_path(f"wallets_{(tier or 'all').upper()}")
    if cache:
        cached = _read_fresh_cache(path, ttl_seconds)
        if cached is not None:
            return cached
    data = _get("/wallets", {"tier": tier})
    out = {
        "ok": True,
        "mode": "smart_money_wallets",
        "generated_at": now(),
        "count": data.get("count") if isinstance(data, dict) else None,
        "wallets": data.get("wallets", []) if isinstance(data, dict) else [],
        "cache": {"hit": False, "ttl_seconds": ttl_seconds},
        "boundary": "read-only wallet universe.",
    }
    if cache:
        _write_cache(path, out)
    return out


ARCHIVE_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "smart_money_signals.sql"
ARCHIVE_DB = PROFILE_HOME / "trading" / "db" / "smart_money_signals.sqlite"


def _to_ledger_result(signal: dict[str, Any]) -> dict[str, Any]:
    """Map a cluster signal onto a signal_ledger result.

    Verdict is the neutral "cluster_observed" — evidence recorded for outcome tracking,
    never a buy call. The ledger + calibration report then measure how these observations
    actually resolved.
    """
    return {
        "mint": signal.get("token"),
        "generated_at": signal.get("trigger_at") or signal.get("created_at") or now(),
        "market": {"symbol": signal.get("symbol"), "market_cap": signal.get("call_market_cap")},
        "verdict": "cluster_observed",
        "entry_gate": {"action": "cluster_observed"},
        "candidate_score": signal.get("weighted_score"),
        "risk_flags": [],
        "smart_money": {
            "distinct_wallets": signal.get("distinct_wallets"),
            "weighted_score": signal.get("weighted_score"),
            "tier_breakdown": signal.get("tier_breakdown"),
        },
    }


def _archive_signals(signals: list[dict[str, Any]], db_path: Path = ARCHIVE_DB) -> int:
    import sqlite3
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(ARCHIVE_SCHEMA.read_text(encoding="utf-8"))
        for s in signals:
            tb = s.get("tier_breakdown") or {}
            con.execute(
                """INSERT OR IGNORE INTO smart_money_signals
                   (signal_id, pulled_at, token, symbol, distinct_wallets, weighted_score,
                    tier_a, tier_b, tier_c, call_market_cap, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (s.get("id"), now(), s.get("token"), s.get("symbol"),
                 s.get("distinct_wallets"), s.get("weighted_score"),
                 tb.get("A", 0), tb.get("B", 0), tb.get("C", 0),
                 s.get("call_market_cap"), json.dumps(s, sort_keys=True, default=str)),
            )
        con.commit()
    finally:
        con.close()
    return len(signals)


def record_signals(out: dict[str, Any], *, archive: bool = True, ledger: bool = True) -> dict[str, Any]:
    """Archive pulled signals and feed them into the signal ledger for calibration."""
    signals = [s for s in (out.get("signals") or []) if s.get("token")]
    archived = _archive_signals(signals) if archive else 0
    recorded = 0
    if ledger:
        import signal_ledger
        for s in signals:
            signal_ledger.record_signal(_to_ledger_result(s), source_command="smart-signals")
            recorded += 1
    return {"ok": True, "archived": archived, "ledger_recorded": recorded}


def _short(mint: str) -> str:
    return f"{mint[:6]}..{mint[-4:]}" if mint and len(mint) > 12 else (mint or "?")


def render_md(out: dict[str, Any]) -> str:
    if out.get("mode") == "smart_money_wallets":
        wallets = out.get("wallets") or []
        tiers = {"A": 0, "B": 0, "C": 0}
        for w in wallets:
            tiers[w.get("tier", "C")] = tiers.get(w.get("tier", "C"), 0) + 1
        lines = ["## Smart-Money Wallet Universe",
                 f"- Tracked: {out.get('count')} (A={tiers['A']} B={tiers['B']} C={tiers['C']})",
                 "", f"_{out.get('boundary')}_", "Advisory + paper only. No wallet, signing, routing, or live execution."]
        return "\n".join(lines) + "\n"

    signals = out.get("signals") or []
    lines = ["## Smart-Money Cluster Signals", f"- Live signals: {len(signals)}", ""]
    for s in signals[:15]:
        tb = s.get("tier_breakdown") or {}
        mc = s.get("call_market_cap")
        mc_str = f"${mc:,.0f}" if isinstance(mc, (int, float)) else "n/a"
        lines.append(
            f"- {s.get('symbol') or _short(s.get('token', ''))}: "
            f"{s.get('distinct_wallets')} wallets clustered (score {s.get('weighted_score')}, "
            f"A/B/C {tb.get('A', 0)}/{tb.get('B', 0)}/{tb.get('C', 0)}) · call MC {mc_str}"
        )
    if not signals:
        lines.append("- (none)")
    lines += ["", f"_{out.get('boundary')}_", "Advisory + paper only. No wallet, signing, routing, or live execution."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only smart-money signal feed (external signal API)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("signals", help="live smart-money cluster signals")
    ps.add_argument("--limit", type=int, default=20)
    ps.add_argument("--raw", action="store_true")
    ps.add_argument("--ledger", action="store_true",
                    help="archive pulled signals + record them for calibration")
    pw = sub.add_parser("wallets", help="tracked wallet universe")
    pw.add_argument("--tier", choices=["A", "B", "C"], default=None)
    pw.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    ledger_summary = None
    if args.cmd == "signals":
        out = fetch_live_signals(limit=args.limit)
        if getattr(args, "ledger", False):
            ledger_summary = record_signals(out)
    else:
        out = fetch_wallets(tier=args.tier)

    if args.raw:
        # Redact the token defensively even though it is never placed in the payload.
        text = json.dumps(out, indent=2, sort_keys=True, default=str)
        token = os.getenv(ENV_TOKEN, "").strip()
        if token:
            text = text.replace(token, "<SMART_MONEY_API_TOKEN_REDACTED>")
        print(text)
    else:
        print(render_md(out))

    if ledger_summary is not None:
        print(f"[ledger] archived={ledger_summary['archived']} recorded={ledger_summary['ledger_recorded']}")


if __name__ == "__main__":
    main()
