#!/usr/bin/env python3
"""Read-only Chaos token event analyzer.

Manual first, cron-safe later. No trading execution, signing, wallet connection, or posting. Produces a
compact plain-text card plus JSON/Markdown artifacts for scoring over time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
HERMES_SRC = Path(os.environ["HERMES_AGENT_SRC"]) if os.environ.get("HERMES_AGENT_SRC") else None
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import x_provider  # noqa: E402
from helius_common import BASE58_RE, rpc_request, safe_print  # noqa: E402
from dexscreener_client import fetch_token, resolve_best_token_market  # noqa: E402
from flow_conversion import classify_flow_conversion  # noqa: E402
from gate_classifier import classify_gate  # noqa: E402
from gmgn_readonly_adapter import query_token_bundle  # noqa: E402
from mode_classifier import classify_mode  # noqa: E402
from secondary_evidence import compact_secondary_evidence  # noqa: E402
from position_context import analyze_position_context, normalize_entry_gate  # noqa: E402
from signal_ledger import fact_grade, record_signal  # noqa: E402
from social_catalyst_classifier import classify_social_catalyst  # noqa: E402
from token_delta_tracker import track_token_delta  # noqa: E402
from wallet_position_timing import enrich_wallet_timing  # noqa: E402

MINT_RE = BASE58_RE
OUT_ROOT = PROFILE_HOME / "trading" / "alpha" / "sweeps"
NO_EXECUTION_BOUNDARY = (
    "read-only source acquisition; advisory and policy-governed paper decisions allowed; "
    "no wallet, signing, routing, live execution, or posting"
)
ROSTER_PATH = PROFILE_HOME / "trading" / "config" / "roster.json"
WATCHLIST_FILES = [
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_study_set.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "actor_cluster_deep_pass.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_scores.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "hidden_alpha_findings.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_actor_map.json",
    PROFILE_HOME / "trading" / "alpha" / "smart_wallet_promotions.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_initial_review.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_all_active_review.json",
    PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_deep_pass_review.json",
    ROSTER_PATH,
]
_ROSTER_CACHE: dict[str, Any] = {}
OWNER_WALLETS_PATH = PROFILE_HOME / "trading" / "watchlists" / "owner_wallets.json"
POSITIVE_WALLET_ROLES = {"copyable-candidate", "deep-watch", "deep-first", "watch", "sample-watch", "smart-wallet-candidate"}
SCOUT_WALLET_ROLES = {"high-churn-scout", "sample", "scout", "cluster-sensor"}
NEGATIVE_WALLET_ROLES = {"avoid", "no-signal", "noisy-churn", "ignore"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def slug(value: str | None, fallback: str) -> str:
    raw = value or fallback
    out = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_")
    return (out[:48] or fallback)


def clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def run_json(cmd: list[str], timeout: int = 75) -> tuple[dict[str, Any] | None, str | None]:
    env = dict(os.environ)
    env.setdefault("CHAOS_HOME", str(PROFILE_HOME))
    env.setdefault("HERMES_HOME", str(PROFILE_HOME))
    env.setdefault("PYTHONPATH", os.pathsep.join(x for x in [str(SCRIPT_DIR), str(HERMES_SRC) if HERMES_SRC is not None else "", env.get("PYTHONPATH", "")] if x))
    env["PYTHONIOENCODING"] = "utf-8"  # children write utf-8; decoded as utf-8 below
    try:
        proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return None, f"timeout after {timeout}s: {' '.join(cmd)}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:1200]
        return None, f"exit {proc.returncode}: {err}"
    text = (proc.stdout or "").strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"json decode failed: {exc}; output={text[:500]}"


def holder_data_of(result: dict[str, Any]) -> str | None:
    """The marker a holder read leaves when it is not live: `cached <N>m`, or `unavailable (...)` when holders were not read."""
    hr = (result.get("token_scan") or {}).get("holder_resolution")
    return hr.get("holder_data") if isinstance(hr, dict) else None


def mark_holder_data(result: dict[str, Any]) -> None:
    """Put the marker in the result JSON; `holder_data_unavailable: true` only when holders were not read.

    A cached sample is a real sample and scores like a live one; its marker is shown as a caveat."""
    marker = holder_data_of(result)
    if marker:
        result["holder_data"] = marker
        if marker.startswith("unavailable"):
            result["holder_data_unavailable"] = True


def dex_summary(mint: str) -> tuple[dict[str, Any], str | None]:
    try:
        return fetch_token("solana", mint, cache=True), None
    except Exception as exc:
        return {"ok": False, "token": mint, "summary": {}, "pairs": []}, str(exc)


def best_market(dex: dict[str, Any]) -> dict[str, Any]:
    summary = dex.get("summary") or {}
    pair = (dex.get("pairs") or [{}])[0] if isinstance(dex.get("pairs"), list) and dex.get("pairs") else {}
    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    return {
        "symbol": base.get("symbol") or summary.get("symbol"),
        "name": base.get("name") or summary.get("name"),
        "dex_id": summary.get("dexId"),
        "url": summary.get("url"),
        "pair_address": summary.get("best_pair"),
        "price_usd": summary.get("priceUsd"),
        "fdv": summary.get("fdv"),
        "market_cap": summary.get("marketCap"),
        "liquidity_usd": summary.get("liquidity_usd"),
        "volume_h1": summary.get("volume_h1"),
        "volume_h6": summary.get("volume_h6"),
        "volume_h24": summary.get("volume_h24"),
        "txns_m5": summary.get("txns_m5"),
        "txns_h1": summary.get("txns_h1"),
        "txns_h6": summary.get("txns_h6"),
        "txns_h24": summary.get("txns_h24"),
        "price_change_m5": summary.get("priceChange_m5"),
        "price_change_h1": summary.get("priceChange_h1"),
        "price_change_h6": summary.get("priceChange_h6"),
        "price_change_h24": summary.get("priceChange_h24"),
        "pair_created_at": summary.get("pairCreatedAt"),
        "socials": info.get("socials") or [],
        "websites": info.get("websites") or [],
    }


def wallet_role(row: dict[str, Any], source_name: str) -> tuple[str | None, str, float | None]:
    """Classify local wallet rows as confirmation-quality, scout-only, or ignore.

    This is deliberately conservative: negative/avoid rows never become quality hits;
    high-churn scouts are discovery sensors, not conviction confirmation.
    """
    raw_role = str(row.get("actor_label") or row.get("tier") or row.get("verdict") or row.get("quality_label") or "").strip()
    role_l = raw_role.lower().replace("_", "-")
    score = as_float(row.get("score") or row.get("hidden_score") or row.get("tracker_score"), default=-999.0)
    if role_l in NEGATIVE_WALLET_ROLES:
        return None, raw_role or "negative", score
    if row.get("hard_flags"):
        return None, raw_role or "hard-flagged", score
    # Explicit high-quality labels win unless contradicted above.
    if role_l in POSITIVE_WALLET_ROLES:
        return "quality", raw_role, score
    if role_l in SCOUT_WALLET_ROLES:
        return "scout", raw_role, score
    # The user's home roster: tiers A and B confirm, tier C is a sensor.
    if "roster" in source_name:
        tier = str(row.get("tier") or "").strip().lower()
        if tier in ("a", "b"):
            return "quality", f"roster-{tier.upper()}", score
        if tier == "c":
            return "scout", "roster-C", score
        return None, "roster-unknown", score
    # Secondary score tiers: high score = quality prior; middling score = scout/sensor.
    if "wallet_scores" in source_name:
        if score >= 70:
            return "quality", raw_role or "secondary-score>=70", score
        if score >= 50:
            return "scout", raw_role or "secondary-score>=50", score
        return None, raw_role or "secondary-score-low", score
    # Hidden alpha file is already post-filtered; require a high hidden score.
    if "hidden_alpha_findings" in source_name and score >= 60:
        return "quality", raw_role or "hidden-alpha", score
    # Final study set is manually curated; keep as quality unless marked negative.
    if "final_study_set" in source_name:
        return "quality", raw_role or "final-study", score
    return None, raw_role or "unqualified", score


def rowsets_from_data(data: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return rows
    for key in ("wallets", "results", "hidden_wallets", "rows", "actors", "study", "watch_first"):
        value = data.get(key)
        if isinstance(value, dict):
            value = list(value.values())
        if isinstance(value, list):
            rows.extend(r for r in value if isinstance(r, dict))
    return rows


def read_roster() -> Any:
    """The home roster's parsed JSON, or None when it is missing or unreadable.

    Addresses are stripped, so a hand-edited padded address still matches sampled holders and the veto set.
    """
    if not ROSTER_PATH.exists():
        return None
    try:
        data = json.loads(ROSTER_PATH.read_text())
    except Exception:
        return None
    for row in rowsets_from_data(data):
        if isinstance(row.get("address"), str):
            row["address"] = row["address"].strip()
    return data


def roster_data() -> Any:
    """The home roster, read once per run; later analyses in the same process reuse it."""
    if "data" not in _ROSTER_CACHE:
        _ROSTER_CACHE["data"] = read_roster()
    return _ROSTER_CACHE["data"]


def negative_row(row: dict[str, Any]) -> bool:
    """A row whose own label is a negative role, or that carries hard flags."""
    raw_role = str(row.get("actor_label") or row.get("tier") or row.get("verdict") or row.get("quality_label") or "").strip()
    return raw_role.lower().replace("_", "-") in NEGATIVE_WALLET_ROLES or bool(row.get("hard_flags"))


def load_watch_wallets() -> dict[str, dict[str, Any]]:
    watches: dict[str, dict[str, Any]] = {}
    loaded: list[tuple[Path, list[dict[str, Any]]]] = []
    for path in WATCHLIST_FILES:
        if path == ROSTER_PATH:
            data = roster_data()
        elif not path.exists():
            continue
        else:
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
        loaded.append((path, rowsets_from_data(data)))
    # A negative or hard-flagged row in any watch file vetoes the roster's entry for that address.
    # Entries from the other files merge exactly as before.
    vetoed: set[str] = set()
    for _, rows in loaded:
        for row in rows:
            wallet = row.get("wallet") or row.get("address") or row.get("owner")
            if wallet and negative_row(row):
                vetoed.add(str(wallet))
    for path, rows in loaded:
        for row in rows:
            wallet = row.get("wallet") or row.get("address") or row.get("owner")
            label = row.get("label") or row.get("handle") or row.get("display_name") or row.get("name") or "watch"
            if not wallet:
                continue
            if path == ROSTER_PATH and str(wallet) in vetoed:
                continue
            kind, role, score = wallet_role(row, path.name)
            if not kind:
                continue
            existing = watches.get(str(wallet))
            entry = {
                "wallet": str(wallet),
                "label": str(label),
                "kind": kind,
                "role": role,
                "score": score,
                "source": path.name,
            }
            if not existing or (existing.get("kind") != "quality" and kind == "quality") or (score or -999) > (existing.get("score") or -999):
                watches[str(wallet)] = entry
    return watches


def load_owner_wallets() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(OWNER_WALLETS_PATH.read_text()) if OWNER_WALLETS_PATH.exists() else {}
    except Exception:
        data = {}
    rows = data.get("wallets") or {}
    if isinstance(rows, list):
        return {str(r.get("wallet") or r.get("address")): r for r in rows if isinstance(r, dict) and (r.get("wallet") or r.get("address"))}
    if isinstance(rows, dict):
        return {str(k): {"wallet": str(k), **(v if isinstance(v, dict) else {})} for k, v in rows.items()}
    return {}


def token_balance_for_owner(owner: str, mint: str) -> tuple[float, int, str | None]:
    """Read current owner balance for a mint via RPC. Handles Token and Token-2022 parsed accounts."""
    try:
        resp = rpc_request("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}], timeout=20, retries=1)
    except SystemExit as exc:
        return 0.0, 0, str(exc)[:300]
    except Exception as exc:
        return 0.0, 0, str(exc)[:300]
    total = 0.0
    count = 0
    for item in (resp or {}).get("value") or []:
        try:
            info = (((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amt = (info.get("tokenAmount") or {}).get("uiAmount")
            if amt is not None:
                total += float(amt)
                count += 1
        except Exception:
            continue
    return total, count, None


def owner_wallet_exposure(mint: str) -> dict[str, Any]:
    owners = load_owner_wallets()
    hits = []
    errors = []
    for wallet, meta in owners.items():
        amount, account_count, err = token_balance_for_owner(wallet, mint)
        if err:
            errors.append({"wallet": wallet, "label": meta.get("label"), "error": err})
        if amount > 0:
            hits.append({
                "wallet": wallet,
                "label": meta.get("label") or wallet[:6],
                "role": meta.get("role") or "owner_wallet",
                "amount": amount,
                "account_count": account_count,
            })
    return {
        "owner_wallet_file": str(OWNER_WALLETS_PATH),
        "owner_wallet_count": len(owners),
        "owner_wallet_hits": hits,
        "owner_wallet_hit_count": len(hits),
        "errors": errors[:5],
        "note": "owner wallets are style/exposure context only; not public alpha or replication authority",
    }


def mask_wallet(value: Any) -> str:
    text = str(value or "")
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


def sanitize_private_context(result: dict[str, Any]) -> dict[str, Any]:
    """Redact owner wallet identifiers before token/sweep artifacts leave owner scope.

    Owner wallets are read from trading/watchlists/owner_wallets.json. Token event artifacts are
    candidate research artifacts, so they may carry owner exposure counts but not
    exact private wallet addresses or owner watchlist paths.
    """
    clean = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    owner_wallets = set(load_owner_wallets().keys())
    owner = clean.get("owner_exposure") if isinstance(clean, dict) else None
    if isinstance(owner, dict):
        owner_wallets.update(str(item.get("wallet")) for key in ("owner_wallet_hits", "errors") for item in (owner.get(key) or []) if isinstance(item, dict) and item.get("wallet"))
        for key in ("owner_wallet_hits", "errors"):
            for item in owner.get(key) or []:
                if isinstance(item, dict) and item.get("wallet"):
                    item["wallet"] = mask_wallet(item.get("wallet"))
        if owner.get("owner_wallet_file"):
            owner["owner_wallet_file"] = "[redacted-owner-wallet-file]"
    wallet_timing = clean.get("wallet_timing") if isinstance(clean, dict) else None
    if isinstance(wallet_timing, dict):
        for item in wallet_timing.get("wallet_timing") or []:
            if isinstance(item, dict) and item.get("wallet") and (item.get("private_owner_context") or str(item.get("wallet")) in owner_wallets):
                item["wallet"] = mask_wallet(item.get("wallet"))
        for key in ("watch_wallet_hits", "quality_wallet_hits", "scout_wallet_hits"):
            for item in wallet_timing.get(key) or []:
                if isinstance(item, dict) and str(item.get("wallet") or "") in owner_wallets:
                    item["wallet"] = mask_wallet(item.get("wallet"))
        for item in wallet_timing.get("top_holders_sample") or []:
            if isinstance(item, dict) and str(item.get("owner") or "") in owner_wallets:
                item["owner"] = mask_wallet(item.get("owner"))
        for item in wallet_timing.get("top_signers_sample") or []:
            if isinstance(item, dict) and str(item.get("wallet") or "") in owner_wallets:
                item["wallet"] = mask_wallet(item.get("wallet"))
    return clean


def extract_wallet_touch(token_scan: dict[str, Any] | None, pump: dict[str, Any] | None) -> dict[str, Any]:
    watch = load_watch_wallets()
    seen: set[str] = set()
    holders = []
    if token_scan:
        for h in (((token_scan.get("token_accounts_summary") or {}).get("holder_sample") or [])):
            owner = h.get("owner")
            if owner:
                seen.add(str(owner))
                entry = watch.get(str(owner))
                holders.append({"owner": owner, "amount": h.get("amount"), "watch_label": (entry or {}).get("label"), "watch_kind": (entry or {}).get("kind")})
        hr = token_scan.get("holder_resolution") or {}
        for h in (hr.get("holders") or hr.get("resolved_holders") or [])[:20] if isinstance(hr, dict) else []:
            owner = h.get("owner") or h.get("address")
            if owner:
                seen.add(str(owner))
    signers = []
    if pump:
        for wallet, count in pump.get("top_signers_sample") or []:
            if wallet:
                seen.add(str(wallet))
                entry = watch.get(str(wallet))
                signers.append({"wallet": wallet, "count": count, "watch_label": (entry or {}).get("label"), "watch_kind": (entry or {}).get("kind")})
    hits = [{"wallet": w, **watch[w]} for w in sorted(seen) if w in watch]
    quality_hits = [h for h in hits if h.get("kind") == "quality"]
    scout_hits = [h for h in hits if h.get("kind") == "scout"]
    return {
        "watch_wallet_hits": hits,
        "watch_wallet_hit_count": len(quality_hits),
        "quality_wallet_hits": quality_hits,
        "quality_wallet_hit_count": len(quality_hits),
        "scout_wallet_hits": scout_hits,
        "scout_wallet_hit_count": len(scout_hits),
        "wallet_hit_count_total": len(hits),
        "holder_sample_count": len(holders),
        "top_holders_sample": holders[:10],
        "top_signers_sample": signers[:8],
        "watch_wallet_file_count": len(watch),
    }


# Errors x_provider.search() returns before any request leaves the machine: no provider or no key
# (error_type None), its own input checks, a malformed key or unbuildable request, and a Hermes
# tree that fails to import for any reason.
PRE_REQUEST_ERROR_TYPES = {None, "bad_dates", "empty_query", "bad_key", "bad_request", "hermes_import"}


def x_request_made(x: dict[str, Any]) -> bool:
    """D2: whether the search reached the provider, so an X budget unit was really spent.
    An answer counts, and so does an HTTP, transport or malformed-response failure."""
    return bool(x.get("available")) or x.get("error_type") not in PRE_REQUEST_ERROR_TYPES


def x_evidence(x: dict[str, Any]) -> tuple[bool, str]:
    """D1: only a sourced answer is X evidence. Returns (is evidence, why not).

    Unavailable, degraded (a date window was set and nothing was cited, so the answer is the
    model's own knowledge) and citation-free answers, including a subscription login's, are not.
    """
    if not x.get("available"):
        return False, str(x.get("error") or "X search unavailable")
    if x.get("degraded"):
        return False, str(x.get("degraded_reason") or "degraded answer")
    if not (x.get("citations") or x.get("inline_citations")):
        if x.get("credential_detail") == "xai-oauth":
            return False, "subscription login answers without citations"
        return False, "answer had no citations"
    return True, ""


def attention_query(mint: str, market: dict[str, Any]) -> str:
    parts = [mint]
    symbol = market.get("symbol")
    name = market.get("name")
    if symbol:
        parts.append(f"${symbol}")
        parts.append(str(symbol))
    if name and name != symbol:
        parts.append(str(name))
    joined = " OR ".join(dict.fromkeys(parts))
    return (
        f"Search X for recent posts, replies, quote context, and source ignition around: {joined}. "
        "Focus on whether attention is early, live, late, caller-spam, or exit-liquidity. "
        "Return concise evidence with citations. Do not give trading advice."
    )


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def txn_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    return int(as_float(value.get("buys")) + as_float(value.get("sells")))


def x_text(x: Any) -> str:
    if not isinstance(x, dict):
        return ""
    parts = [str(x.get("answer") or "")]
    for key in ("citations", "inline_citations"):
        for item in x.get(key) or []:
            if isinstance(item, dict):
                parts.append(str(item.get("title") or "")); parts.append(str(item.get("url") or ""))
            else:
                parts.append(str(item))
    return "\n".join(parts).lower()


def flow_profile(market: dict[str, Any]) -> dict[str, Any]:
    liq = as_float(market.get("liquidity_usd"))
    candidates = []
    for window, volume, txs in [
        ("h24", as_float(market.get("volume_h24")), txn_count(market.get("txns_h24"))),
        ("h6", as_float(market.get("volume_h6")), txn_count(market.get("txns_h6"))),
        ("h1", as_float(market.get("volume_h1")), txn_count(market.get("txns_h1"))),
        ("m5", 0.0, txn_count(market.get("txns_m5"))),
    ]:
        if volume <= 0 or txs <= 0:
            continue
        vol_liq = volume / liq if liq > 0 and volume > 0 else None
        avg_tx = volume / txs if txs > 0 and volume > 0 else None
        flags: list[str] = []
        severity = 0
        if vol_liq is not None:
            if vol_liq >= 100:
                severity += 3; flags.append(f"extreme volume/liquidity churn {vol_liq:.1f}x")
            elif vol_liq >= 25:
                severity += 2; flags.append(f"high volume/liquidity churn {vol_liq:.1f}x")
            elif vol_liq >= 20:
                severity += 1; flags.append(f"elevated volume/liquidity churn {vol_liq:.1f}x")
        if avg_tx is not None:
            if txs >= 50_000 and avg_tx <= 150:
                severity += 3; flags.append(f"extreme micro-churn: {txs:,} txns avg ${avg_tx:.0f}")
            elif txs >= 10_000 and avg_tx <= 125:
                severity += 2; flags.append(f"micro-churn: {txs:,} txns avg ${avg_tx:.0f}")
            elif txs >= 5_000 and avg_tx <= 75:
                severity += 1; flags.append(f"small-ticket churn: {txs:,} txns avg ${avg_tx:.0f}")
        candidates.append({
            "window": window,
            "volume_usd": round(volume, 3) if volume else None,
            "liquidity_usd": round(liq, 3) if liq else None,
            "volume_liquidity_ratio": round(vol_liq, 3) if vol_liq is not None else None,
            "tx_count": txs,
            "avg_tx_usd": round(avg_tx, 3) if avg_tx is not None else None,
            "flags": flags,
            "severity": severity,
        })
    if not candidates:
        return {
            "window": "unknown",
            "volume_usd": None,
            "liquidity_usd": round(liq, 3) if liq else None,
            "volume_liquidity_ratio": None,
            "tx_count": 0,
            "avg_tx_usd": None,
            "flags": [],
            "severity": 0,
            "windows": [],
        }
    selected = dict(max(candidates, key=lambda c: (int(c.get("severity") or 0), float(c.get("volume_liquidity_ratio") or 0), int(c.get("tx_count") or 0))))
    selected["windows"] = candidates
    return selected


def x_risk_profile(x: Any) -> dict[str, Any]:
    text = x_text(x)
    flags: list[str] = []
    if any(term in text for term in ("phishing", "drainer", "malware")):
        flags.append("phishing/drainer claims on X — unverified")
    if any(term in text for term in ("spam", "raid", "raiding", "bot", "bots")):
        flags.append("social-spam/raid claims on X — unverified")
    if any(term in text for term in ("vote", "voting", "moonshot", "boost", "boosted", "paid 100x")):
        flags.append("boost/vote-push activity on X — unverified")
    if any(term in text for term in ("scam", "rug", "dev sold", "fud")):
        flags.append("scam/rug/dev-sell claims on X — unverified")
    confidence = "low"
    if len(flags) >= 2 or any("phishing" in f or "scam" in f for f in flags):
        confidence = "medium"
    if len(flags) >= 3:
        confidence = "high"
    return {"confidence": confidence, "flags": flags, "note": "X risk is social evidence only; do not treat as hard truth without corroboration."}


def validation_profile(market: dict[str, Any], wallet: dict[str, Any], x: Any, flow: dict[str, Any]) -> dict[str, Any]:
    text = x_text(x)
    wallet_hits = int(wallet.get("watch_wallet_hit_count") or 0)
    liq = as_float(market.get("liquidity_usd"))
    ratio = flow.get("volume_liquidity_ratio")
    dex_id = str(market.get("dex_id") or "").lower()
    credible_catalyst = any(term in text for term in (
        "source ignition", "official", "creator", "major accounts", "phantom", "raydium", "orca", "solana mobile", "jup verification"
    ))
    organic_source = bool(credible_catalyst and not any(term in text for term in ("spam", "raid", "bot", "phishing")))
    healthier_venue = liq >= 100_000 and (ratio is None or ratio < 20) and dex_id not in {"pumpswap", "pumpfun"}
    positives: list[str] = []
    if wallet_hits:
        positives.append(f"quality wallet/sample hits: {wallet_hits}")
    if credible_catalyst:
        positives.append("credible catalyst/source ignition present")
    if organic_source:
        positives.append("organic-source evidence present")
    if healthier_venue:
        positives.append("healthier venue/liquidity confirmation")
    missing: list[str] = []
    if not wallet_hits:
        missing.append("no watch-wallet validation")
    if not healthier_venue:
        missing.append("no healthier venue/liquidity confirmation")
    if not positives:
        missing.append("no catalyst/organic-source validation")
    return {
        "quality_wallet_hits": wallet_hits,
        "credible_catalyst": credible_catalyst,
        "organic_source": organic_source,
        "healthier_venue_liquidity": healthier_venue,
        "positives": positives,
        "missing": missing,
    }


def classify(result: dict[str, Any]) -> dict[str, Any]:
    market = result.get("market") or {}
    wallet = result.get("wallet_timing") or {}
    x = result.get("x_attention") or {}
    token_scan = result.get("token_scan") or {}
    pump = result.get("pumpfun") or {}

    reasons: list[str] = []
    flags: list[str] = []
    score = 0

    liq_f = as_float(market.get("liquidity_usd"))
    if liq_f >= 25_000:
        score += 2; reasons.append("liquidity >= 25k")
    elif liq_f and liq_f < 5_000:
        score -= 2; flags.append("thin liquidity")

    if wallet.get("watch_wallet_hit_count"):
        score += 4; reasons.append("watch wallet touched sample")
    if (pump or {}).get("pumpfun_activity_visible"):
        score += 1; reasons.append("pump.fun activity visible")
    if (pump or {}).get("raydium_activity_visible"):
        reasons.append("raydium activity visible")

    hr = token_scan.get("holder_resolution") or {}
    adj = hr.get("adjusted_discretionary_pct") if isinstance(hr, dict) else None
    adj_f = as_float(adj, default=-1.0) if adj is not None else None
    if adj_f is not None and adj_f >= 45:
        score -= 2; flags.append("high adjusted discretionary concentration")

    x_success = bool(x.get("success")) if isinstance(x, dict) else False
    x_cites = len((x or {}).get("citations") or []) + len((x or {}).get("inline_citations") or []) if isinstance(x, dict) else 0
    # x_attention is set only for a sourced answer (D1); no evidence moves nothing here.
    if x_success and x_cites:
        score += 2; reasons.append(f"X citations present ({x_cites})")

    flow = flow_profile(market)
    x_risk = x_risk_profile(x)
    validation = validation_profile(market, wallet, x, flow)
    fake_flow = bool(flow["flags"] and flow["severity"] >= 2)
    if flow["flags"]:
        flags.extend(flow["flags"])
        score -= min(3, int(flow["severity"]))
    if x_risk["flags"]:
        flags.extend(x_risk["flags"])
        score -= 1 if x_risk["confidence"] in {"medium", "high"} else 0

    pc_h1_f = as_float(market.get("price_change_h1"), default=0.0)
    pc_h6_f = as_float(market.get("price_change_h6"), default=0.0)
    pc_h24_f = as_float(market.get("price_change_h24"), default=0.0)
    severe_drawdown = pc_h1_f <= -50 or pc_h6_f <= -70 or pc_h24_f <= -80
    if severe_drawdown:
        score -= 4; flags.append("severe drawdown; likely failed/exit-liquidity tape")
    if pc_h1_f > 150 and not wallet.get("watch_wallet_hit_count"):
        flags.append("large move without watch-wallet evidence; possible late/exit-liquidity read")
        score -= 1

    why_watch = ""
    why_not_watch = ""
    if fake_flow:
        if validation["quality_wallet_hits"] or validation["healthier_venue_liquidity"]:
            why_watch = "fake-flow flags exist, but wallet/venue validation is present; keep manual-review before watch."
        elif validation["credible_catalyst"] or validation["organic_source"]:
            why_not_watch = "credible catalyst exists, but fake-flow flags still need wallet or healthier-liquidity confirmation."
        else:
            why_not_watch = "surface momentum lacks organic-flow validation."
        if validation["missing"]:
            flags.append("validation missing: " + ", ".join(validation["missing"][:2]))
    elif score >= 3 and not flags:
        why_watch = "surface momentum has no current fake-flow gate flags."
    elif flags:
        why_not_watch = "risk flags require manual review before watch."

    if severe_drawdown and fake_flow:
        verdict = "avoid"
        phase = "failed"
    elif fake_flow and flow["severity"] >= 5 and not (validation["quality_wallet_hits"] or validation["healthier_venue_liquidity"]):
        verdict = "exit-liquidity-watch"
        phase = "late"
    elif fake_flow and not (validation["quality_wallet_hits"] or validation["healthier_venue_liquidity"]):
        verdict = "manual-review" if validation["credible_catalyst"] and score >= 3 else "study-caution"
        phase = "live" if validation["credible_catalyst"] else "unknown"
    elif score >= 6:
        verdict = "manual-review"
        phase = "live"
    elif "large move without watch-wallet evidence; possible late/exit-liquidity read" in flags:
        verdict = "exit-liquidity-watch"
        phase = "late"
    elif score >= 3:
        verdict = "watch"
        phase = "live"
    elif flags:
        verdict = "study-caution"
        phase = "unknown"
    else:
        verdict = "study"
        phase = "unknown"

    # Added after the verdict is chosen: a missing holder read is shown, but no gate moves because of it.
    holder_marker = holder_data_of(result)
    risk_flags = flags + [f"holder data {holder_marker}"] if holder_marker else flags
    return {
        "score": score,
        "verdict": verdict,
        "attention_phase": phase,
        "reasons": reasons,
        "risk_flags": risk_flags,
        "flow": flow,
        "x_risk": x_risk,
        "validation": validation,
        "why_watch": why_watch,
        "why_not_watch": why_not_watch,
    }


def render_markdown(result: dict[str, Any]) -> str:
    market = result.get("market") or {}
    cls = result.get("classification") or {}
    wallet = result.get("wallet_timing") or {}
    x = result.get("x_attention") or {}
    lines = [
        "# Chaos Token Event Read",
        "",
        f"Generated: {result['generated_at']}",
        f"Mint: `{result['mint']}`",
        f"Token: {market.get('symbol') or 'unknown'} / {market.get('name') or 'unknown'}",
        "",
        "## Verdict",
        "",
        f"- Entry gate: **{((result.get('entry_gate') or {}).get('action') or (result.get('gate') or {}).get('gate') or cls.get('verdict'))}**",
        f"- Structural gate: **{((result.get('gate') or {}).get('gate') or cls.get('verdict'))}**",
        f"- Position action: **{((result.get('position_context') or {}).get('position_action') or 'no-position')}**",
        f"- Catalyst: **{((result.get('social_catalyst') or {}).get('catalyst_type') or 'none')}** / {((result.get('social_catalyst') or {}).get('catalyst_subtype') or 'none')}",
        f"- Flow conversion: **{((result.get('flow_conversion') or {}).get('conversion_status') or 'unknown')}**",
        f"- Legacy classifier: **{cls.get('verdict')}**",
        f"- Attention phase: `{cls.get('attention_phase')}`",
        f"- Score: {cls.get('score')}",
        f"- Mode: {((result.get('mode_context') or {}).get('mode'))}",
        f"- Gate: {((result.get('gate') or {}).get('gate'))}",
        f"- Venue: {((result.get('mode_context') or {}).get('venue'))} / {((result.get('mode_context') or {}).get('venue_regime'))}",
        f"- Fact grade: {result.get('fact_grade')}",
        f"- Why watch: {cls.get('why_watch') or 'not cleared'}",
        f"- Why not watch: {cls.get('why_not_watch') or 'not blocked by current gate'}",
        f"- Boundary: {NO_EXECUTION_BOUNDARY}",
        "",
        "## Market",
        "",
        f"- DEX: {market.get('dex_id')}",
        f"- Liquidity: {market.get('liquidity_usd')}",
        f"- Market cap / FDV: {market.get('market_cap')} / {market.get('fdv')}",
        f"- Volume h1/h6/h24: {market.get('volume_h1')} / {market.get('volume_h6')} / {market.get('volume_h24')}",
        f"- Txns m5/h1/h24: {market.get('txns_m5')} / {market.get('txns_h1')} / {market.get('txns_h24')}",
        f"- Price change h1/h24: {market.get('price_change_h1')} / {market.get('price_change_h24')}",
        f"- Link: {market.get('url') or ''}",
        "",
        "## Flow / validation gate",
        "",
        f"- Flow window: {(cls.get('flow') or {}).get('window')}",
        f"- Volume/liquidity: {(cls.get('flow') or {}).get('volume_liquidity_ratio')}x",
        f"- Avg tx / tx count: ${((cls.get('flow') or {}).get('avg_tx_usd'))} / {(cls.get('flow') or {}).get('tx_count')}",
        f"- Flow conversion: {((result.get('flow_conversion') or {}).get('conversion_status') or 'unknown')} / fake-flow {((result.get('flow_conversion') or {}).get('fake_flow_severity') or 'unknown')}",
        f"- Catalyst: {((result.get('social_catalyst') or {}).get('catalyst_type') or 'none')} / {((result.get('social_catalyst') or {}).get('source_quality') or 'none')}",
        f"- Flow flags: {', '.join((cls.get('flow') or {}).get('flags') or []) or 'none'}",
        f"- X-risk confidence: {(cls.get('x_risk') or {}).get('confidence')} ({', '.join((cls.get('x_risk') or {}).get('flags') or []) or 'no specific X-risk flags'})",
        f"- Validation present: {', '.join((cls.get('validation') or {}).get('positives') or []) or 'none'}",
        f"- Validation missing: {', '.join((cls.get('validation') or {}).get('missing') or []) or 'none'}",
        "",
        "## Wallet / holder sample",
        "",
        f"- Watch wallet hits: {wallet.get('watch_wallet_hit_count')} / watch file count {wallet.get('watch_wallet_file_count')}",
        f"- Wallet timing: {wallet.get('timing_label') or 'unknown'} / {wallet.get('timing_summary') or {}}",
        f"- Scout wallet hits: {wallet.get('scout_wallet_hit_count', 0)} / total qualified hits {wallet.get('wallet_hit_count_total', wallet.get('watch_wallet_hit_count'))}",
        f"- Owner wallet exposure: {((result.get('owner_exposure') or {}).get('owner_wallet_hit_count', 0))} / {((result.get('owner_exposure') or {}).get('owner_wallet_count', 0))}",
        f"- Position action: {((result.get('position_context') or {}).get('position_action') or 'no-position')} / value ${((result.get('position_context') or {}).get('position_value_usd'))}",
    ]
    for hit in wallet.get("watch_wallet_hits") or []:
        lines.append(f"  - `{hit['wallet']}` — {hit.get('label')} ({hit.get('kind')}/{hit.get('role')})")
    for hit in (result.get("owner_exposure") or {}).get("owner_wallet_hits") or []:
        wallet_id = str(hit.get("wallet") or "")
        masked = f"{wallet_id[:6]}…{wallet_id[-4:]}" if len(wallet_id) > 12 else wallet_id
        lines.append(f"  - OWNER `{masked}` — {hit.get('label')} amount={hit.get('amount')}")
    lines += [
        f"- Holder sample count: {wallet.get('holder_sample_count')}",
        "",
        "## X attention",
        "",
    ]
    if isinstance(x, dict) and x.get("success"):
        lines.append(f"- Degraded: {x.get('degraded')}")
        answer = str(x.get("answer") or "").strip()
        if answer:
            lines.append("")
            lines.append(answer[:2400])
        cites = (x.get("inline_citations") or x.get("citations") or [])[:8]
        if cites:
            lines.append("")
            lines.append("### Citations")
            for c in cites:
                if isinstance(c, dict):
                    lines.append(f"- {c.get('title') or c.get('url')}: {c.get('url')}")
                else:
                    lines.append(f"- {c}")
    elif result.get("x_enabled"):
        lines.append(f"- X: no sourced evidence ({result.get('x_error')})")
    else:
        lines.append(f"- X unavailable/inconclusive: {result.get('x_error')}")
    lines += [
        "",
        "## Reasons",
        "",
    ]
    for reason in cls.get("reasons") or []:
        lines.append(f"- {reason}")
    if cls.get("risk_flags"):
        lines += ["", "## Risk flags", ""]
        for flag in cls.get("risk_flags") or []:
            lines.append(f"- {flag}")
    lines += ["", "## Non-goals", "", "- No execution, no alerts, no trade instruction.", "- This is evidence capture for later scoring.", ""]
    return "\n".join(lines)


def compact_card(result: dict[str, Any]) -> str:
    market = result.get("market") or {}
    cls = result.get("classification") or {}
    wallet = result.get("wallet_timing") or {}
    x = result.get("x_attention") or {}
    flow = cls.get("flow") or {}
    x_risk = cls.get("x_risk") or {}
    flow_conv = result.get("flow_conversion") or {}
    catalyst = result.get("social_catalyst") or {}
    entry_gate = result.get("entry_gate") or {}
    position = result.get("position_context") or {}
    cites = len((x or {}).get("citations") or []) + len((x or {}).get("inline_citations") or []) if isinstance(x, dict) else 0
    flow_line = "Flow: n/a"
    if flow:
        ratio = flow_conv.get("volume_liquidity_ratio") if flow_conv.get("volume_liquidity_ratio") is not None else flow.get("volume_liquidity_ratio")
        avg = flow.get("avg_tx_usd")
        txs = flow.get("tx_count")
        flow_line = f"Flow: {flow_conv.get('conversion_status') or 'unknown'} · V/L {ratio}x · avg tx ${avg} · txs {txs}"
    risk_bits = (cls.get("risk_flags") or [])[:2]
    why = cls.get("why_not_watch") or cls.get("why_watch") or "No gate note."
    x_line = [f"X: no sourced evidence ({str(result.get('x_error'))[:160]})"] if result.get("x_enabled") and not x else []
    return "\n".join([
        "☄️ Chaos Token Event Read",
        f"{market.get('symbol') or 'UNKNOWN'} · `{result['mint']}`",
        f"Entry: {entry_gate.get('action') or (result.get('gate') or {}).get('gate') or cls.get('verdict')} · Position: {position.get('position_action') or 'no-position'}",
        f"Catalyst: {catalyst.get('catalyst_type') or 'none'} · Fact: {result.get('fact_grade') or '?'} · Ledger: {'ok' if result.get('ledger') else 'miss'}",
        f"Liq: {market.get('liquidity_usd')} · MC: {market.get('market_cap')} · h1 Δ: {market.get('price_change_h1')}",
        flow_line,
        f"X-risk: {x_risk.get('confidence', 'low')} · X citations: {cites}",
        *x_line,
        f"Watch hits: {wallet.get('watch_wallet_hit_count')} · Why: {why}",
        f"Risk: {'; '.join(risk_bits) if risk_bits else 'none flagged'}",
        f"Artifact: {result.get('markdown_path')}",
        "Advisory + paper only. No wallet, signing, routing, or live execution.",
    ])


def attach_gmgn_enrichment(
    result: dict[str, Any],
    mint: str,
    *,
    enabled: bool,
    fetcher: Any = None,
) -> dict[str, Any]:
    """Attach secondary GMGN evidence after primary classification is complete."""
    result["gmgn_enabled"] = bool(enabled)
    if not enabled:
        return result
    fetch = fetcher or query_token_bundle
    try:
        result["gmgn"] = fetch(mint)
    except Exception:
        result["gmgn"] = {
            "available": False,
            "source": "gmgn",
            "chain": "sol",
            # Never reflect provider/credential exception text into artifacts or chat.
            "errors": [{"kind": "adapter_failure", "message": "GMGN read adapter failed closed"}],
            "boundary": "read-only GMGN enrichment; primary Chaos evidence unchanged",
        }
    return result


def analyze(mint: str, *, tx_limit: int, x_enabled: bool, x_days: int, gmgn_enabled: bool = False, out_dir: Path | None = None, source_command: str = "token") -> dict[str, Any]:
    if not MINT_RE.match(mint):
        raise SystemExit(json.dumps({"ok": False, "error": "invalid Solana mint shape", "length": len(mint)}, indent=2))
    generated = now_utc().isoformat(timespec="seconds")
    out_dir = out_dir or (OUT_ROOT / now_utc().strftime("%Y-%m-%d"))
    out_dir.mkdir(parents=True, exist_ok=True)

    dex, dex_error = dex_summary(mint)
    market = resolve_best_token_market(mint) if not dex_error else best_market(dex)
    py = sys.executable
    token_scan, token_err = run_json([py, str(SCRIPT_DIR / "token_scan.py"), mint, "--limit", str(tx_limit), "--raw"])
    pump, pump_err = run_json([py, str(SCRIPT_DIR / "pumpfun_launch_read.py"), mint, "--limit", str(tx_limit), "--raw"])
    wallet_timing = extract_wallet_touch(token_scan, pump)
    owner_exposure = owner_wallet_exposure(mint)

    # D3: asking for X with no provider configured is X off. D1: only a sourced answer is stored
    # as x_attention, so no classifier, gate, ledger or paper rule ever reads an unsourced one.
    x_enabled = x_enabled and x_provider.provider_name() != "none"
    x_payload = None
    x_error = None
    x_made = False
    if x_enabled:
        to_date = now_utc().date().isoformat()
        from_date = (now_utc().date() - timedelta(days=max(1, x_days))).isoformat()
        x_result = x_provider.search(attention_query(mint, market), from_date=from_date, to_date=to_date)
        x_made = x_request_made(x_result)
        sourced, why_not = x_evidence(x_result)
        if sourced:
            x_payload = x_result
        else:
            x_error = why_not

    result: dict[str, Any] = {
        "ok": True,
        "mode": "chaos_readonly_token_event_analyzer",
        "generated_at": generated,
        "mint": mint,
        "boundary": NO_EXECUTION_BOUNDARY,
        "market": market,
        "dex": {"ok": dex.get("ok"), "pair_count": dex.get("pair_count"), "summary": dex.get("summary"), "error": dex_error},
        "token_scan": token_scan,
        "token_scan_error": token_err,
        "pumpfun": pump,
        "pumpfun_error": pump_err,
        "wallet_timing": wallet_timing,
        "owner_exposure": owner_exposure,
        "x_enabled": x_enabled,
        "x_attention": x_payload,
        "x_error": x_error,
        "x_request_made": x_made,
    }
    mark_holder_data(result)
    result["classification"] = classify(result)
    result["secondary_evidence"] = compact_secondary_evidence(mint)
    result["mode_context"] = classify_mode(result)
    result["gate"] = classify_gate(result)
    result["entry_gate"] = normalize_entry_gate(result.get("gate"), result.get("classification"))
    result["social_catalyst"] = classify_social_catalyst(result)
    result["flow_conversion"] = classify_flow_conversion(result)
    result["position_context"] = analyze_position_context(result)
    result["wallet_timing"] = enrich_wallet_timing(wallet_timing, result)
    result["delta"] = track_token_delta(result, OUT_ROOT)
    result["fact_grade"] = fact_grade(result)
    symbol_slug = slug(market.get("symbol"), mint[:8])
    prefix = f"token_event_{symbol_slug}_{mint[:8]}_{stamp()}"
    json_path = out_dir / f"{prefix}.json"
    md_path = out_dir / f"{prefix}.md"
    result["json_path"] = str(json_path)
    result["markdown_path"] = str(md_path)
    try:
        result["ledger"] = record_signal(result, source_command=source_command)
    except Exception as exc:
        result["ledger_error"] = str(exc)
    artifact_result = sanitize_private_context(result)
    md = render_markdown(artifact_result)
    json_path.write_text(json.dumps(artifact_result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    md_path.write_text(md, encoding="utf-8")
    result["card"] = compact_card(result)
    artifact_result["card"] = result["card"]
    # Re-write JSON with card included, still sanitized for token/sweep scope.
    json_path.write_text(json.dumps(artifact_result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    # Phase 1 GMGN data is response-only. Attach it strictly after ledger and
    # artifact persistence so provider payloads cannot cross the storage boundary,
    # move a verdict, clear a blocker, or replace Helius/Dex values.
    attach_gmgn_enrichment(result, mint, enabled=gmgn_enabled)
    return sanitize_private_context(result)


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only Chaos token event analyzer. No execution.")
    p.add_argument("mint", help="Solana token mint / CA")
    p.add_argument("--tx-limit", type=int, default=25, help="1-100 Helius mint tx/sample limit")
    p.add_argument("--x", action="store_true", help="Include x_search attention scan")
    p.add_argument("--x-days", type=int, default=2, help="X search lookback days")
    p.add_argument("--gmgn", action="store_true", help="Attach secondary read-only GMGN token enrichment")
    p.add_argument("--out-dir", help="Artifact output directory")
    p.add_argument("--source-command", default="token", help="Ledger source label")
    p.add_argument("--raw", action="store_true", help="Print full JSON result")
    args = p.parse_args()
    if args.x and x_provider.provider_name() == "none":
        print(x_provider.no_provider_notice(), file=sys.stderr)
    result = analyze(
        args.mint,
        tx_limit=clamp_int(args.tx_limit, 1, 100),
        x_enabled=bool(args.x),
        x_days=clamp_int(args.x_days, 1, 14),
        gmgn_enabled=bool(args.gmgn),
        out_dir=Path(args.out_dir).expanduser() if args.out_dir else None,
        source_command=args.source_command,
    )
    if args.raw:
        safe_print(result)
    else:
        print(result["card"])


if __name__ == "__main__":
    main()
