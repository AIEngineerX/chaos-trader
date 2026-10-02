#!/usr/bin/env python3
"""Initial read-only review for imported Solana tracked-wallet watchlist.

Builds a first pass: activity recency, recent failure/churn sample,
SOL balance, overlap with known secondary wallet scores, inactive filtering, and
priority tiers. No signing, sending, swapping, alert creation, or wallet control.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
from helius_common import rpc_endpoint  # noqa: E402
import no_redirect  # noqa: E402
PROFILE_HOME = chaos_home()
WATCHLIST = PROFILE_HOME / "trading" / "watchlists" / "wallets.json"
SECONDARY_WALLETS = PROFILE_HOME / "trading" / "watchlists" / "secondary_wallets.json"
ALPHA_ROOT = PROFILE_HOME / "trading" / "alpha"
# token_event_analyzer reads the review from here as secondary evidence.
REVIEW_ROOT = ALPHA_ROOT / "secondary"
WATCHLIST_ROOT = PROFILE_HOME / "trading" / "watchlists"

SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
LAMPORTS_PER_SOL = 1_000_000_000


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_from_block_time(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def rpc_batch(calls: list[dict[str, Any]], *, timeout: int = 45, retries: int = 3) -> list[dict[str, Any]]:
    if not calls:
        return []
    body = json.dumps(calls).encode("utf-8")
    req = urllib.request.Request(
        rpc_endpoint(),
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ChaosReadOnly/1.0"},
        method="POST",
    )
    last_error = None
    for attempt in range(retries + 1):
        try:
            # The endpoint URL can carry the key, so no redirect is followed: a 3xx raises HTTPError.
            with no_redirect.open_no_redirect(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload if isinstance(payload, list) else [payload]
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                exc.close()
                raise RuntimeError(f"HTTP {exc.code}: RPC unavailable: redirect refused")
            try:
                body_text = exc.read().decode("utf-8", "replace")[:500]
            finally:
                exc.close()
            last_error = f"HTTP {exc.code}: {body_text}"
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                break
        except Exception as exc:  # noqa: BLE001
            last_error = repr(exc)
            if attempt >= retries:
                break
        time.sleep(0.75 * (2**attempt))
    raise RuntimeError(last_error or "RPC batch failed")


def chunked(xs: list[Any], n: int) -> list[list[Any]]:
    return [xs[i : i + n] for i in range(0, len(xs), n)]


def load_imported(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("wallets", []) if isinstance(data, dict) else data
    out = []
    seen = set()
    for r in rows:
        w = r.get("wallet") or r.get("trackedWalletAddress")
        if not isinstance(w, str) or not SOL_RE.match(w) or w in seen:
            continue
        seen.add(w)
        out.append(r)
    return out


def load_secondary(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {r["wallet"]: r for r in data.get("wallets", []) if r.get("wallet")}


def safe_label(label: Any, wallet: str) -> str:
    if label is None or str(label).strip() == "":
        return f"Anon {wallet[:4]}…{wallet[-4:]}"
    return str(label).replace("|", "/").strip()


def activity_tier(age_days: float | None, sample_count: int) -> str:
    if age_days is None:
        return "unresolved"
    if age_days <= 7:
        return "active_7d"
    if age_days <= 30:
        return "warm_30d"
    if age_days <= 90:
        return "stale_90d"
    return "inactive_90d_plus"


def recency_points(age_days: float | None) -> float:
    if age_days is None:
        return 0.0
    if age_days <= 1:
        return 38
    if age_days <= 3:
        return 34
    if age_days <= 7:
        return 30
    if age_days <= 14:
        return 22
    if age_days <= 30:
        return 15
    if age_days <= 90:
        return 5
    return -15


def tier_from_score(score: float, age_days: float | None, failures: float, sample_count: int, secondary_match: bool) -> str:
    if age_days is None:
        return "unresolved"
    if age_days > 90:
        return "inactive"
    if score >= 70 and age_days <= 14 and failures <= 0.35:
        return "deep-first"
    if score >= 52 and age_days <= 30:
        return "watch"
    if score >= 35 and age_days <= 30:
        return "sample"
    if secondary_match and age_days <= 90:
        return "study"
    if sample_count == 0:
        return "unresolved"
    return "ignore"


def main() -> None:
    parser = argparse.ArgumentParser(description="Initial review of imported wallet watchlist.")
    parser.add_argument("--input", default=str(WATCHLIST))
    parser.add_argument("--signature-limit", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--out-prefix", default="wallet_initial_review")
    args = parser.parse_args()

    started = now_utc()
    rows = load_imported(Path(args.input).expanduser())
    secondary = load_secondary(SECONDARY_WALLETS)
    addresses = [r.get("wallet") or r.get("trackedWalletAddress") for r in rows]

    sig_results: dict[str, Any] = {}
    balance_results: dict[str, Any] = {}
    rpc_errors: list[dict[str, Any]] = []

    # getSignaturesForAddress batch
    call_id = 1
    for batch in chunked(addresses, args.batch_size):
        calls = []
        id_to_wallet = {}
        for w in batch:
            cid = call_id
            call_id += 1
            id_to_wallet[cid] = w
            calls.append({"jsonrpc": "2.0", "id": cid, "method": "getSignaturesForAddress", "params": [w, {"limit": max(1, min(args.signature_limit, 100))}]})
        for resp in rpc_batch(calls):
            w = id_to_wallet.get(resp.get("id"))
            if not w:
                continue
            if "error" in resp:
                rpc_errors.append({"wallet": w, "method": "getSignaturesForAddress", "error": resp["error"]})
                sig_results[w] = []
            else:
                sig_results[w] = resp.get("result") or []

    # getBalance batch
    for batch in chunked(addresses, args.batch_size):
        calls = []
        id_to_wallet = {}
        for w in batch:
            cid = call_id
            call_id += 1
            id_to_wallet[cid] = w
            calls.append({"jsonrpc": "2.0", "id": cid, "method": "getBalance", "params": [w]})
        for resp in rpc_batch(calls):
            w = id_to_wallet.get(resp.get("id"))
            if not w:
                continue
            if "error" in resp:
                rpc_errors.append({"wallet": w, "method": "getBalance", "error": resp["error"]})
                balance_results[w] = None
            else:
                balance_results[w] = ((resp.get("result") or {}).get("value") or 0) / LAMPORTS_PER_SOL

    now = now_utc()
    reviewed = []
    for src in rows:
        w = src.get("wallet") or src.get("trackedWalletAddress")
        sigs = sig_results.get(w) or []
        last_bt = sigs[0].get("blockTime") if sigs and isinstance(sigs[0], dict) else None
        age_days = None
        if last_bt is not None:
            age_days = max(0.0, (now.timestamp() - int(last_bt)) / 86400)
        failed = sum(1 for s in sigs if isinstance(s, dict) and s.get("err") is not None)
        sample_count = len(sigs)
        failure_rate = failed / sample_count if sample_count else None
        programs = Counter()
        # getSignaturesForAddress does not expose program labels; leave schema hook for later parse pass.
        oc = secondary.get(w)
        sol_balance = balance_results.get(w)
        score = 0.0
        score += recency_points(age_days)
        score += min(sample_count, args.signature_limit) / max(1, args.signature_limit) * 18
        if failure_rate is not None:
            score += max(0.0, (1.0 - failure_rate)) * 12
            if failure_rate > 0.45:
                score -= 12
        if sol_balance is not None:
            score += min(math.log10(sol_balance + 1) * 8, 18)
        if src.get("alerts_on") or src.get("alertsOn"):
            score += 4
        if oc:
            score += 22 if oc.get("tier") == "deep-first" else 14
            try:
                score += min(float(oc.get("score") or 0) / 10, 12)
            except Exception:
                pass
        tier = tier_from_score(score, age_days, failure_rate if failure_rate is not None else 0, sample_count, bool(oc))
        notes = []
        if oc:
            notes.append(f"secondary_overlap:{oc.get('tier')}")
        if failure_rate is not None and failure_rate > 0.35:
            notes.append("high_recent_fail_rate")
        if sample_count >= args.signature_limit:
            notes.append("full_recent_tx_sample")
        if age_days is not None and age_days > 90:
            notes.append("inactive_90d_plus")
        if sol_balance is not None and sol_balance < 0.01:
            notes.append("low_sol_balance")
        reviewed.append({
            "wallet": w,
            "label": safe_label(src.get("label") or src.get("name"), w),
            "emoji": src.get("emoji"),
            "alerts_on": bool(src.get("alerts_on") if "alerts_on" in src else src.get("alertsOn")),
            "tier": tier,
            "activity_tier": activity_tier(age_days, sample_count),
            "score": round(score, 3),
            "last_active_at": iso_from_block_time(last_bt),
            "last_active_age_days": round(age_days, 3) if age_days is not None else None,
            "recent_tx_sample": sample_count,
            "recent_failed_txs": failed,
            "recent_failure_rate": round(failure_rate, 3) if failure_rate is not None else None,
            "sol_balance": round(sol_balance, 9) if sol_balance is not None else None,
            "latest_signature": sigs[0].get("signature") if sigs and isinstance(sigs[0], dict) else None,
            "secondary_overlap": bool(oc),
            "secondary_score": oc.get("score") if oc else None,
            "secondary_tier": oc.get("tier") if oc else None,
            "secondary_pnl_all": oc.get("pnl_all") if oc else None,
            "secondary_events": oc.get("events") if oc else None,
            "notes": notes,
        })

    reviewed.sort(key=lambda r: (r["score"], -(r["last_active_age_days"] or 99999)), reverse=True)

    counts = Counter(r["tier"] for r in reviewed)
    activity_counts = Counter(r["activity_tier"] for r in reviewed)
    active_rows = [r for r in reviewed if r["activity_tier"] in {"active_7d", "warm_30d"}]
    inactive_rows = [r for r in reviewed if r["activity_tier"] == "inactive_90d_plus"]
    unresolved_rows = [r for r in reviewed if r["activity_tier"] == "unresolved"]
    top = reviewed[:40]

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "mode": "read_only_imported_wallet_initial_review",
        "source_watchlist": str(Path(args.input).expanduser()),
        "wallet_count": len(reviewed),
        "signature_limit": args.signature_limit,
        "counts_by_tier": dict(counts),
        "counts_by_activity": dict(activity_counts),
        "active_or_warm_count": len(active_rows),
        "inactive_90d_plus_count": len(inactive_rows),
        "unresolved_count": len(unresolved_rows),
        "rpc_error_count": len(rpc_errors),
        "rpc_errors_sample": rpc_errors[:10],
        "wallets": reviewed,
        "notes": [
            "Initial pass uses getSignaturesForAddress + getBalance only; it proves recency and rough account posture, not realized edge.",
            "Secondary overlap boosts score only where local secondary watchlist already had a wallet; imported list has no native PnL field.",
            "Inactive means no observed recent signature in the sampled RPC response or last signature older than 90 days; deep parse can revise classifications.",
            "Read-only: no signing, sending, swapping, posting, or alert creation.",
        ],
    }

    REVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    WATCHLIST_ROOT.mkdir(parents=True, exist_ok=True)
    json_path = REVIEW_ROOT / f"{args.out_prefix}.json"
    csv_path = REVIEW_ROOT / f"{args.out_prefix}.csv"
    md_path = REVIEW_ROOT / f"{args.out_prefix}.md"
    active_path = WATCHLIST_ROOT / f"{args.out_prefix}.active.json"
    inactive_path = WATCHLIST_ROOT / f"{args.out_prefix}.inactive.json"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    active_path.write_text(json.dumps({"generated_at": payload["generated_at"], "wallets": active_rows}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    inactive_path.write_text(json.dumps({"generated_at": payload["generated_at"], "wallets": inactive_rows}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    cols = ["tier", "activity_tier", "score", "label", "wallet", "alerts_on", "last_active_at", "last_active_age_days", "recent_tx_sample", "recent_failure_rate", "sol_balance", "secondary_overlap", "secondary_score", "notes"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for r in reviewed:
            row = {c: r.get(c) for c in cols}
            row["notes"] = ";".join(r.get("notes") or [])
            writer.writerow(row)

    def table(rows_: list[dict[str, Any]], n: int = 20) -> list[str]:
        lines = ["| Tier | Label | Wallet | Score | Last active | Fail | SOL | Notes |", "|---|---|---|---:|---:|---:|---:|---|"]
        for r in rows_[:n]:
            notes = ", ".join(r.get("notes") or []) or "clean"
            fail = "" if r.get("recent_failure_rate") is None else f"{r['recent_failure_rate']:.1%}"
            sol = "" if r.get("sol_balance") is None else f"{r['sol_balance']:.3f}"
            age = "unresolved" if r.get("last_active_age_days") is None else f"{r['last_active_age_days']:.1f}d"
            lines.append(f"| {r['tier']} | {r['label']} | `{r['wallet']}` | {r['score']} | {age} | {fail} | {sol} | {notes} |")
        return lines

    md = [
        "# Imported Wallet Initial Review",
        "",
        f"Generated: {payload['generated_at']}",
        f"Source: `{payload['source_watchlist']}`",
        "",
        "## Verdict",
        "",
        "This is an activity and overlap filter, not a smart-money verdict. The list is now separated into active/warm candidates, stale/inactive inventory, and secondary-overlap wallets worth deeper parse.",
        "",
        "## Counts",
        "",
        f"- Wallets reviewed: {payload['wallet_count']}",
        f"- Active/warm ≤30d: {payload['active_or_warm_count']}",
        f"- Inactive >90d: {payload['inactive_90d_plus_count']}",
        f"- Unresolved/no signatures returned: {payload['unresolved_count']}",
        f"- RPC errors: {payload['rpc_error_count']}",
        f"- Tier counts: `{dict(counts)}`",
        f"- Activity counts: `{dict(activity_counts)}`",
        "",
        "## Filters applied",
        "",
        "- Recency: last signature age buckets: ≤7d active, ≤30d warm, ≤90d stale, >90d inactive.",
        "- Churn: recent signature sample count from RPC, capped by configured sample limit.",
        "- Friction: recent failed transaction rate penalizes noisy/botched wallets.",
        "- Capital posture: SOL balance is a weak positive, not proof of edge.",
        "- Secondary overlap: prior local secondary score/tier boosts ranking where wallet matched existing corpus.",
        "",
        "## Top candidates",
        "",
        *table(top, 30),
        "",
        "## Secondary-overlap candidates",
        "",
        *table([r for r in reviewed if r.get("secondary_overlap")], 20),
        "",
        "## Inactive sample",
        "",
        *table(inactive_rows, 20),
        "",
        "## Next pass",
        "",
        "1. Deep-parse top active/watch/deep-first wallets through Helius transaction history.",
        "2. Measure realized entry/exit behavior per mint; do not copy on recency alone.",
        "3. Cluster wallets with shared funders or same-token early entries before promoting to alerts.",
        "",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")

    summary = {
        "ok": True,
        "generated_at": payload["generated_at"],
        "wallet_count": len(reviewed),
        "active_or_warm_count": len(active_rows),
        "inactive_90d_plus_count": len(inactive_rows),
        "unresolved_count": len(unresolved_rows),
        "counts_by_tier": dict(counts),
        "counts_by_activity": dict(activity_counts),
        "artifacts": [str(json_path), str(csv_path), str(md_path), str(active_path), str(inactive_path)],
        "top_wallets": [{k: r[k] for k in ["tier", "label", "wallet", "score", "last_active_age_days", "recent_failure_rate", "sol_balance"]} for r in reviewed[:10]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
