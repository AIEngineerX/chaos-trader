#!/usr/bin/env python3
"""Paper learning report for Chaos wallet-seeded hunter.

Reads paper_autopilot.sqlite and summarizes source quality, paper entries/exits,
rejection reasons, and next rule changes. Read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import REFILL_PAPER_BOOK, chaos_home, stop_if_corrupt  # noqa: E402
from alpha_paper_trade import connect_ro  # noqa: E402
PROFILE_HOME = chaos_home()
PAPER_DB = PROFILE_HOME / "trading" / "db" / "paper_autopilot.sqlite"
REPORT_DIR = PROFILE_HOME / "trading" / "reports" / "paper_learning"
NO_ACTIVITY_NOTE = 'No paper activity yet. Run the ingest job and the paper tick first (see "Running it on a schedule" in the README).'


def jloads(s: Any) -> Any:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(PAPER_DB))
    ap.add_argument("--limit", type=int, default=250)
    args = ap.parse_args()
    con = connect_ro(Path(args.db))
    if con is None:
        stop_if_corrupt(Path(args.db), REFILL_PAPER_BOOK)  # a corrupt book must not read as "no activity yet"
    if con is None or not {"events", "paper_positions", "paper_fills"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        if con is not None:
            con.close()
        print(json.dumps({
            "ok": True,
            "mode": "paper_learning_report",
            "event_counts": {},
            "positions": {"open": 0, "closed": 0, "total": 0, "realized_r": 0},
            "fills": 0,
            "top_blockers": [],
            "latest_decisions": [],
            "recommendations": [],
            "note": NO_ACTIVITY_NOTE,
            "report": None,
            "boundary": "read-only learning report; no execution",
        }, indent=2, sort_keys=True))
        return 0
    events = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.limit,))]
    counts = Counter(e["event_type"] for e in events)
    blockers = Counter()
    wallet_seeded = 0
    decisions = []
    for e in events:
        p = jloads(e.get("payload_json"))
        if e["event_type"] == "wallet_hunter":
            wallet_seeded += int(p.get("inserted") or 0)
        if e["event_type"] in {"paper_avoid", "paper_wait", "paper_enter"}:
            for b in p.get("blockers") or []:
                blockers[b] += 1
            decisions.append({"mint": e.get("mint"), "type": e["event_type"], "symbol": p.get("symbol"), "entry_action": p.get("entry_action"), "liq": (p.get("market") or {}).get("liquidity_usd"), "x": p.get("x"), "blockers": p.get("blockers")})
    positions = [dict(r) for r in con.execute("SELECT * FROM paper_positions ORDER BY opened_at_utc DESC")]
    fills = [dict(r) for r in con.execute("SELECT * FROM paper_fills ORDER BY timestamp_utc DESC")]
    closed = [p for p in positions if p.get("state") not in {"PAPER_OPEN", "PAPER_TRIMMED"}]
    openp = [p for p in positions if p.get("state") in {"PAPER_OPEN", "PAPER_TRIMMED"}]
    realized_r = sum(float(p.get("realized_r") or 0) for p in positions)
    recommendations = []
    if counts.get("wallet_hunter", 0) and not counts.get("paper_enter", 0):
        recommendations.append("Keep wallet hunter active, but improve source ranking: require liquidity marks before X on very low-cap wallet mints.")
    if blockers.get("X/social catalyst is spam-raid", 0) >= 3:
        recommendations.append("Downrank spam-raid wallet mints unless >=2 independent strong wallets touched the same mint or liquidity > $25k.")
    if blockers.get("liquidity below paper threshold: None", 0) >= 2:
        recommendations.append("Add Dex liquidity precheck before spending X on wallet-discovered mints with missing liquidity.")
    payload = {
        "ok": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "paper_learning_report",
        "event_counts": dict(counts),
        "wallet_seeded_candidates_inserted_recent": wallet_seeded,
        "positions": {"open": len(openp), "closed": len(closed), "total": len(positions), "realized_r": round(realized_r, 6)},
        "fills": len(fills),
        "top_blockers": blockers.most_common(15),
        "latest_decisions": decisions[:20],
        "recommendations": recommendations,
        "boundary": "read-only learning report; no execution",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORT_DIR / f"paper_learning_{stamp}.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({**payload, "report": str(out)}, indent=2, sort_keys=True, default=str))
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
