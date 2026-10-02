#!/usr/bin/env python3
"""Chaos local alpha claim ledger.

Read/write local alpha accounting only. No trading, no posting, no alerts, no
network calls. Turns claims into measurable outcomes and source/wallet scores.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
ALPHA_ROOT = Path(os.environ.get("CHAOS_ALPHA_ROOT", PROFILE_HOME / "trading" / "alpha")).expanduser()
WATCHLIST_ROOT = Path(os.environ.get("CHAOS_WATCHLIST_ROOT", PROFILE_HOME / "trading" / "watchlists")).expanduser()
CLAIMS_PATH = ALPHA_ROOT / "claims.jsonl"
SOURCES_PATH = ALPHA_ROOT / "sources.json"
SOURCE_SCORES_PATH = ALPHA_ROOT / "source_scores.json"
WALLET_SCORES_PATH = ALPHA_ROOT / "wallet_scores.json"
ASSET_SCORES_PATH = ALPHA_ROOT / "asset_scores.json"

SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|bearer|private[_-]?key|seed|mnemonic)\s*[:=]\s*[^\s]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
)
VALID_LEAD = {"early", "live", "late", "exit-liquidity", "unknown"}
VALID_STATUS = {"open", "resolved", "expired", "invalid", "watch"}
VALID_OUTCOME = {"hit", "miss", "neutral", "rug", "late", "invalid", "unknown"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    out = value
    for pattern in SECRET_PATTERNS:
        out = pattern.sub("<REDACTED_SECRET>", out)
    return out


def ensure_dirs() -> None:
    ALPHA_ROOT.mkdir(parents=True, exist_ok=True)
    WATCHLIST_ROOT.mkdir(parents=True, exist_ok=True)
    for path, default in (
        (SOURCES_PATH, {}),
        (SOURCE_SCORES_PATH, {}),
        (WALLET_SCORES_PATH, {}),
        (ASSET_SCORES_PATH, {}),
        (WATCHLIST_ROOT / "wallets.json", {}),
        (WATCHLIST_ROOT / "sources.json", {}),
        (WATCHLIST_ROOT / "assets.json", {}),
        (WATCHLIST_ROOT / "banned.json", {"sources": {}, "wallets": {}, "assets": {}, "notes": []}),
    ):
        if not path.exists():
            path.write_text(json.dumps(default, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not CLAIMS_PATH.exists():
        CLAIMS_PATH.touch()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(errors="ignore").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data: Any) -> None:
    ensure_dirs()
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_claims() -> list[dict[str, Any]]:
    ensure_dirs()
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(CLAIMS_PATH.read_text(errors="ignore").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            rows.append({"type": "corrupt_line", "line": line_no, "raw": line[:240]})
    return rows


def write_claims(rows: list[dict[str, Any]]) -> None:
    ensure_dirs()
    with CLAIMS_PATH.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def append_claim(row: dict[str, Any]) -> None:
    ensure_dirs()
    with CLAIMS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def claim_id() -> str:
    return f"claim-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def clamp_score(value: float | int | None, default: float = 0.0) -> float:
    try:
        v = float(value if value is not None else default)
    except (TypeError, ValueError):
        v = default
    return max(-100.0, min(100.0, round(v, 3)))


def claim_quality(row: dict[str, Any]) -> int:
    score = 0
    if row.get("primary_link"):
        score += 2
    if row.get("mint"):
        score += 2
    if row.get("wallet"):
        score += 1
    if row.get("tx_signature"):
        score += 2
    if row.get("mechanism") and row.get("mechanism") != "unknown":
        score += 1
    lead = row.get("lead_time_class")
    score += {"early": 3, "live": 1, "late": -1, "exit-liquidity": -3}.get(str(lead), 0)
    try:
        score += int(float(row.get("confidence", 0)) * 2)
    except (TypeError, ValueError):
        pass
    return score


def recompute_scores(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    source_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"claims": 0, "resolved": 0, "hits": 0, "misses": 0, "rugs": 0, "late": 0, "total_r": 0.0, "quality_sum": 0})
    wallet_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"claims": 0, "resolved": 0, "hits": 0, "misses": 0, "total_r": 0.0, "quality_sum": 0})
    asset_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"claims": 0, "resolved": 0, "hits": 0, "misses": 0, "rugs": 0, "total_r": 0.0, "quality_sum": 0})

    for row in rows:
        if row.get("type") == "corrupt_line":
            continue
        q = claim_quality(row)
        outcome = row.get("outcome") or "unknown"
        status = row.get("status") or "open"
        try:
            r = float(row.get("result_r") or 0)
        except (TypeError, ValueError):
            r = 0.0
        keys = [str(row.get("source_id") or "unknown-source")]
        for key in keys:
            s = source_stats[key]
            s["claims"] += 1
            s["quality_sum"] += q
            if status == "resolved":
                s["resolved"] += 1
                s["total_r"] += r
            if outcome == "hit":
                s["hits"] += 1
            elif outcome == "miss":
                s["misses"] += 1
            elif outcome == "rug":
                s["rugs"] += 1
            elif outcome == "late":
                s["late"] += 1
        wallet = row.get("wallet")
        if wallet:
            s = wallet_stats[str(wallet)]
            s["claims"] += 1
            s["quality_sum"] += q
            if status == "resolved":
                s["resolved"] += 1
                s["total_r"] += r
            if outcome == "hit":
                s["hits"] += 1
            elif outcome == "miss":
                s["misses"] += 1
        asset = row.get("mint") or row.get("asset") or row.get("ticker")
        if asset:
            s = asset_stats[str(asset)]
            s["claims"] += 1
            s["quality_sum"] += q
            if status == "resolved":
                s["resolved"] += 1
                s["total_r"] += r
            if outcome == "hit":
                s["hits"] += 1
            elif outcome == "miss":
                s["misses"] += 1
            elif outcome == "rug":
                s["rugs"] += 1

    def finalize(stats: dict[str, dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, s in stats.items():
            claims = max(1, int(s["claims"]))
            resolved = int(s["resolved"])
            hit_rate = (s["hits"] / resolved) if resolved else None
            avg_r = (s["total_r"] / resolved) if resolved else None
            # Conservative score: quality helps, misses/rugs/late hurt, unresolved stays muted.
            base = (s["quality_sum"] / claims) * 4
            base += (hit_rate or 0) * 35
            base += (avg_r or 0) * 8
            base -= s.get("misses", 0) * 6
            base -= s.get("rugs", 0) * 12
            base -= s.get("late", 0) * 4
            if resolved == 0:
                base = min(base, 20)
            out[key] = {
                "kind": kind,
                **s,
                "hit_rate": None if hit_rate is None else round(hit_rate, 3),
                "avg_r": None if avg_r is None else round(avg_r, 3),
                "score": clamp_score(base),
                "updated_at_utc": now_utc(),
            }
        return dict(sorted(out.items(), key=lambda kv: kv[1].get("score", 0), reverse=True))

    return {
        "sources": finalize(source_stats, "source"),
        "wallets": finalize(wallet_stats, "wallet"),
        "assets": finalize(asset_stats, "asset"),
    }


def cmd_init(_args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    return {
        "ok": True,
        "mode": "alpha_ledger_init",
        "alpha_root": str(ALPHA_ROOT),
        "watchlist_root": str(WATCHLIST_ROOT),
        "files": [str(CLAIMS_PATH), str(SOURCES_PATH), str(SOURCE_SCORES_PATH), str(WALLET_SCORES_PATH), str(ASSET_SCORES_PATH)],
        "boundary": "local alpha accounting only; no trading, posting, alerts, or platform collection",
    }


def cmd_add(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    lead = args.lead_time_class if args.lead_time_class in VALID_LEAD else "unknown"
    row = {
        "id": args.id or claim_id(),
        "created_at_utc": now_utc(),
        "updated_at_utc": now_utc(),
        "status": "open",
        "source_id": redact(args.source_id),
        "source_type": redact(args.source_type),
        "claim": redact(args.claim),
        "asset": redact(args.asset),
        "ticker": redact(args.ticker),
        "mint": redact(args.mint),
        "wallet": redact(args.wallet),
        "tx_signature": redact(args.tx_signature),
        "mechanism": redact(args.mechanism),
        "evidence_class": redact(args.evidence_class),
        "primary_link": redact(args.primary_link),
        "first_seen_utc": redact(args.first_seen_utc or now_utc()),
        "expected_effect": redact(args.expected_effect),
        "expiry_utc": redact(args.expiry_utc),
        "confidence": args.confidence,
        "lead_time_class": lead,
        "notes": redact(args.notes),
        "outcome": "unknown",
        "result_r": None,
        "error_type": None,
        "boundary": "claim record only; no execution or alerts",
    }
    append_claim(row)
    return {"ok": True, "mode": "claim_added", "claim_id": row["id"], "quality_score": claim_quality(row)}


def cmd_resolve(args: argparse.Namespace) -> dict[str, Any]:
    rows = read_claims()
    found = False
    for row in rows:
        if row.get("id") != args.claim_id:
            continue
        found = True
        outcome = args.outcome if args.outcome in VALID_OUTCOME else "unknown"
        status = args.status if args.status in VALID_STATUS else "resolved"
        row.update({
            "status": status,
            "outcome": outcome,
            "result_r": args.result_r,
            "resolved_at_utc": args.resolved_at_utc or now_utc(),
            "updated_at_utc": now_utc(),
            "error_type": redact(args.error_type),
            "outcome_notes": redact(args.notes),
        })
    if not found:
        return {"ok": False, "mode": "claim_resolve_rejected", "error": "claim_id not found", "claim_id": redact(args.claim_id)}
    write_claims(rows)
    scores = recompute_scores(rows)
    save_json(SOURCE_SCORES_PATH, scores["sources"])
    save_json(WALLET_SCORES_PATH, scores["wallets"])
    save_json(ASSET_SCORES_PATH, scores["assets"])
    return {"ok": True, "mode": "claim_resolved", "claim_id": args.claim_id, "scores_updated": True}


def cmd_review(args: argparse.Namespace) -> dict[str, Any]:
    rows = read_claims()
    claims = [r for r in rows if r.get("type") != "corrupt_line"]
    statuses = Counter(r.get("status") or "open" for r in claims)
    outcomes = Counter(r.get("outcome") or "unknown" for r in claims)
    leads = Counter(r.get("lead_time_class") or "unknown" for r in claims)
    sources = Counter(r.get("source_id") or "unknown-source" for r in claims)
    assets = Counter(r.get("mint") or r.get("asset") or r.get("ticker") or "unknown-asset" for r in claims)
    open_claims = [r for r in claims if (r.get("status") or "open") in {"open", "watch"}]
    scores = recompute_scores(claims)
    if args.write_scores:
        save_json(SOURCE_SCORES_PATH, scores["sources"])
        save_json(WALLET_SCORES_PATH, scores["wallets"])
        save_json(ASSET_SCORES_PATH, scores["assets"])
    return {
        "ok": True,
        "mode": "alpha_ledger_review",
        "generated_at_utc": now_utc(),
        "claim_count": len(claims),
        "open_count": len(open_claims),
        "status_counts": dict(statuses),
        "outcome_counts": dict(outcomes),
        "lead_time_counts": dict(leads),
        "top_sources": sources.most_common(args.top),
        "top_assets": assets.most_common(args.top),
        "top_source_scores": list(scores["sources"].items())[: args.top],
        "top_wallet_scores": list(scores["wallets"].items())[: args.top],
        "top_asset_scores": list(scores["assets"].items())[: args.top],
        "boundary": "local review only; no alerts or execution",
    }


def cmd_score(args: argparse.Namespace) -> dict[str, Any]:
    rows = [r for r in read_claims() if r.get("type") != "corrupt_line"]
    scores = recompute_scores(rows)
    save_json(SOURCE_SCORES_PATH, scores["sources"])
    save_json(WALLET_SCORES_PATH, scores["wallets"])
    save_json(ASSET_SCORES_PATH, scores["assets"])
    return {
        "ok": True,
        "mode": "alpha_scores_recomputed",
        "source_count": len(scores["sources"]),
        "wallet_count": len(scores["wallets"]),
        "asset_count": len(scores["assets"]),
        "paths": {"sources": str(SOURCE_SCORES_PATH), "wallets": str(WALLET_SCORES_PATH), "assets": str(ASSET_SCORES_PATH)},
    }


def cmd_watch_add(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    path = WATCHLIST_ROOT / f"{args.kind}s.json"
    data = load_json(path, {})
    item_id = redact(args.id)
    data[item_id] = {
        "id": item_id,
        "kind": args.kind,
        "why_watched": redact(args.why),
        "first_seen_utc": args.first_seen_utc or now_utc(),
        "trust_score": args.trust_score,
        "status": args.status,
        "last_useful_signal": redact(args.last_useful_signal),
        "failure_notes": redact(args.failure_notes),
        "updated_at_utc": now_utc(),
    }
    save_json(path, data)
    return {"ok": True, "mode": "watch_added", "kind": args.kind, "id": item_id, "path": str(path)}


def emit(result: dict[str, Any], raw: bool) -> None:
    if raw:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"## {result.get('mode', 'alpha_ledger')}")
    for key, value in result.items():
        if key in {"mode"}:
            continue
        print(f"- {key}: {value}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Chaos local alpha claim ledger. No execution, alerts, posting, or platform collection.")
    p.add_argument("--raw", action="store_true", help="Print JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Create alpha ledger/watchlist files")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add", help="Add a source claim")
    s.add_argument("--id")
    s.add_argument("--source-id", required=True)
    s.add_argument("--source-type", default="unknown")
    s.add_argument("--claim", required=True)
    s.add_argument("--asset", default="")
    s.add_argument("--ticker", default="")
    s.add_argument("--mint", default="")
    s.add_argument("--wallet", default="")
    s.add_argument("--tx-signature", default="")
    s.add_argument("--mechanism", default="unknown")
    s.add_argument("--evidence-class", default="unknown")
    s.add_argument("--primary-link", default="")
    s.add_argument("--first-seen-utc", default="")
    s.add_argument("--expected-effect", default="")
    s.add_argument("--expiry-utc", default="")
    s.add_argument("--confidence", type=float, default=0.0)
    s.add_argument("--lead-time-class", choices=sorted(VALID_LEAD), default="unknown")
    s.add_argument("--notes", default="")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("resolve", help="Resolve a claim with an outcome")
    s.add_argument("claim_id")
    s.add_argument("--outcome", choices=sorted(VALID_OUTCOME), required=True)
    s.add_argument("--status", choices=sorted(VALID_STATUS), default="resolved")
    s.add_argument("--result-r", type=float, default=0.0)
    s.add_argument("--error-type", default="")
    s.add_argument("--resolved-at-utc", default="")
    s.add_argument("--notes", default="")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("review", help="Review claims and scores")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--write-scores", action="store_true")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("score", help="Recompute source/wallet/asset scores")
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("watch-add", help="Add/update watchlist entry")
    s.add_argument("kind", choices=["wallet", "source", "asset"])
    s.add_argument("id")
    s.add_argument("--why", required=True)
    s.add_argument("--trust-score", type=float, default=0.0)
    s.add_argument("--status", choices=["active", "decayed", "banned", "review"], default="active")
    s.add_argument("--first-seen-utc", default="")
    s.add_argument("--last-useful-signal", default="")
    s.add_argument("--failure-notes", default="")
    s.set_defaults(fn=cmd_watch_add)
    return p


def main() -> None:
    args = parser().parse_args()
    result = args.fn(args)
    emit(result, args.raw)


if __name__ == "__main__":
    main()
