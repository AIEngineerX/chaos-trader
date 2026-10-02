#!/usr/bin/env python3
"""Read-only paper-trade journal for Chaos.

Creates local paper plans, outcomes, and reviews. Does not place orders, connect
venues, sign transactions, or fetch private account data.
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
JOURNAL_ROOT = Path(os.environ.get("CHAOS_JOURNAL_ROOT", PROFILE_HOME / "trading" / "journals")).expanduser()
LEDGER_PATH = JOURNAL_ROOT / "paper_trades.jsonl"
SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|bearer|private[_-]?key|seed|mnemonic)\s*[:=]\s*[^\s]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(value: str, fallback: str = "trade") -> str:
    cleaned = SLUG_RE.sub("-", value.strip()).strip("-._")[:64]
    return cleaned or fallback


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<REDACTED_SECRET>", redacted)
    return redacted


def ensure_dirs() -> None:
    JOURNAL_ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("plans", "outcomes", "reviews"):
        (JOURNAL_ROOT / name).mkdir(parents=True, exist_ok=True)


def append_ledger(record: dict[str, Any]) -> None:
    ensure_dirs()
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read_ledger() -> list[dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    rows = []
    for line in LEDGER_PATH.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"type": "corrupt_line", "raw": line[:240]})
    return rows


def plan_markdown(record: dict[str, Any]) -> str:
    return f"""# Paper Trade Plan — {record['symbol']}

- ID: `{record['id']}`
- Created: {record['created_at_utc']}
- Status: planned
- Venue: {record['venue']}
- Side: {record['side']}
- Timeframe: {record['timeframe']}
- Max Risk R: {record['max_r']}

## Thesis

{record['thesis']}

## Evidence

{record['evidence']}

## Entry Trigger

{record['entry_trigger']}

## Invalidation

{record['invalidation']}

## Targets

{record['targets']}

## Size Logic

{record['size_logic']}

## Kill Switch

{record['kill_switch']}

## Notes

{record['notes']}

## Boundary

Paper plan only. No order was placed, signed, routed, or prepared for execution.
"""


def outcome_markdown(record: dict[str, Any]) -> str:
    return f"""# Paper Trade Outcome — {record['trade_id']}

- Outcome ID: `{record['outcome_id']}`
- Recorded: {record['recorded_at_utc']}
- Result R: {record['result_r']}
- Outcome: {record['outcome']}
- Error Type: {record['error_type']}

## What Happened

{record['what_happened']}

## Lesson

{record['lesson']}

## Boundary

Paper outcome only. No execution authority exists here.
"""


def cmd_init(_args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    return {
        "ok": True,
        "mode": "paper_journal_init",
        "journal_root": str(JOURNAL_ROOT),
        "ledger": str(LEDGER_PATH),
        "boundary": "local paper journal only; no broker/exchange/wallet connection or execution",
    }


def cmd_plan(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    trade_id = f"paper-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    record = {
        "type": "plan",
        "id": trade_id,
        "created_at_utc": now_utc(),
        "symbol": redact_text(args.symbol),
        "venue": redact_text(args.venue),
        "side": args.side,
        "timeframe": redact_text(args.timeframe),
        "max_r": args.max_r,
        "thesis": redact_text(args.thesis),
        "evidence": redact_text(args.evidence),
        "entry_trigger": redact_text(args.entry_trigger),
        "invalidation": redact_text(args.invalidation),
        "targets": redact_text(args.targets),
        "size_logic": redact_text(args.size_logic),
        "kill_switch": redact_text(args.kill_switch),
        "notes": redact_text(args.notes),
        "boundary": "paper plan only; no execution",
    }
    plan_path = JOURNAL_ROOT / "plans" / f"{trade_id}-{slug(record['symbol'])}.md"
    plan_path.write_text(plan_markdown(record), encoding="utf-8")
    record["plan_path"] = str(plan_path)
    append_ledger(record)
    return {"ok": True, "mode": "paper_plan_created", "trade_id": trade_id, "plan_path": str(plan_path)}


def cmd_outcome(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    plan_ids = {row.get("id") for row in read_ledger() if row.get("type") == "plan"}
    if args.trade_id not in plan_ids:
        return {
            "ok": False,
            "mode": "paper_outcome_rejected",
            "error": "unknown trade_id; create a paper plan before recording an outcome",
            "trade_id": redact_text(args.trade_id),
        }
    outcome_id = f"outcome-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    record = {
        "type": "outcome",
        "trade_id": redact_text(args.trade_id),
        "outcome_id": outcome_id,
        "recorded_at_utc": now_utc(),
        "result_r": args.result_r,
        "outcome": args.outcome,
        "error_type": redact_text(args.error_type),
        "what_happened": redact_text(args.what_happened),
        "lesson": redact_text(args.lesson),
        "boundary": "paper outcome only; no execution",
    }
    outcome_path = JOURNAL_ROOT / "outcomes" / f"{outcome_id}-{slug(args.trade_id)}.md"
    outcome_path.write_text(outcome_markdown(record), encoding="utf-8")
    record["outcome_path"] = str(outcome_path)
    append_ledger(record)
    return {"ok": True, "mode": "paper_outcome_recorded", "trade_id": args.trade_id, "outcome_path": str(outcome_path)}


def cmd_review(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    rows = read_ledger()
    plans = [r for r in rows if r.get("type") == "plan"]
    outcomes = [r for r in rows if r.get("type") == "outcome"]
    by_trade = defaultdict(list)
    for outcome in outcomes:
        by_trade[outcome.get("trade_id")].append(outcome)
    plan_ids = {plan.get("id") for plan in plans}
    orphan_outcomes = [outcome for outcome in outcomes if outcome.get("trade_id") not in plan_ids]
    result_values = []
    for outcome in outcomes:
        try:
            result_values.append(float(outcome.get("result_r")))
        except (TypeError, ValueError):
            pass
    wins = sum(1 for value in result_values if value > 0)
    losses = sum(1 for value in result_values if value < 0)
    breakeven = sum(1 for value in result_values if value == 0)
    expectancy = (sum(result_values) / len(result_values)) if result_values else None
    review = {
        "ok": True,
        "mode": "paper_journal_review",
        "generated_at_utc": now_utc(),
        "plan_count": len(plans),
        "outcome_count": len(outcomes),
        "open_plan_count": sum(1 for plan in plans if not by_trade.get(plan.get("id"))),
        "closed_trade_count": sum(1 for plan in plans if by_trade.get(plan.get("id"))),
        "orphan_outcome_count": len(orphan_outcomes),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "expectancy_r": expectancy,
        "top_error_types": Counter(o.get("error_type") or "unspecified" for o in outcomes).most_common(12),
        "top_symbols": Counter(p.get("symbol") or "unknown" for p in plans).most_common(12),
        "boundary": "local paper review only; no execution or alerting",
    }
    if args.write:
        path = JOURNAL_ROOT / "reviews" / f"review-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(review, indent=2, sort_keys=True), encoding="utf-8")
        review["review_path"] = str(path)
    return review


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Chaos local paper-trade journal. No execution.")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create journal directories")
    init.set_defaults(func=cmd_init)

    plan = sub.add_parser("plan", help="Create a paper trade plan")
    plan.add_argument("--symbol", required=True)
    plan.add_argument("--venue", default="manual/paper")
    plan.add_argument("--side", choices=["long", "short", "watch", "avoid"], required=True)
    plan.add_argument("--timeframe", default="unspecified")
    plan.add_argument("--max-r", type=float, default=0.0)
    plan.add_argument("--thesis", required=True)
    plan.add_argument("--evidence", required=True)
    plan.add_argument("--entry-trigger", required=True)
    plan.add_argument("--invalidation", required=True)
    plan.add_argument("--targets", default="unspecified")
    plan.add_argument("--size-logic", default="paper only; no real size")
    plan.add_argument("--kill-switch", default="invalidate thesis or liquidity regime changes")
    plan.add_argument("--notes", default="")
    plan.set_defaults(func=cmd_plan)

    outcome = sub.add_parser("outcome", help="Record a paper trade outcome")
    outcome.add_argument("--trade-id", required=True)
    outcome.add_argument("--result-r", type=float, required=True)
    outcome.add_argument("--outcome", choices=["win", "loss", "scratch", "missed", "invalidated"], required=True)
    outcome.add_argument("--error-type", default="none")
    outcome.add_argument("--what-happened", required=True)
    outcome.add_argument("--lesson", required=True)
    outcome.set_defaults(func=cmd_outcome)

    review = sub.add_parser("review", help="Summarize paper journal")
    review.add_argument("--write", action="store_true", help="Write review JSON under journals/reviews")
    review.set_defaults(func=cmd_review)
    return p


def main() -> None:
    args = parser().parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
