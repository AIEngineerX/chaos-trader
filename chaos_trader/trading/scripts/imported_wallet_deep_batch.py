#!/usr/bin/env python3
"""Deep-pass selected imported wallets through smart_wallet_tracker.

Read-only. No signing, sending, swapping, alerting, or posting.
"""
from __future__ import annotations
import os

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import smart_wallet_tracker as swt  # noqa: E402

DEFAULT_SELECTION = PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_deep_pass_selection.json"
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
OUT = PROFILE_HOME / "trading" / "alpha" / "secondary" / "wallet_deep_pass.json"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run read-only deep pass for selected imported wallets")
    ap.add_argument("--selection", default=str(DEFAULT_SELECTION))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--include-wallet-api", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    sel = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    wallets = sel.get("wallets", [])
    con = sqlite3.connect(Path(args.db).expanduser())
    con.execute("PRAGMA foreign_keys=ON")
    swt.ensure_db(con)
    results = []
    for i, item in enumerate(wallets, 1):
        w = item["wallet"]
        try:
            res = swt.enrich_wallet(con, w, max(1, min(args.limit, 100)), max(1, args.pages), args.include_wallet_api)
            res.update({
                "label": item.get("label"),
                "initial_score": item.get("score"),
                "initial_tier": item.get("tier"),
                "initial_activity_tier": item.get("activity_tier"),
                "initial_last_active_age_days": item.get("last_active_age_days"),
                "initial_sol_balance": item.get("sol_balance"),
                "secondary_overlap": item.get("secondary_overlap"),
                "secondary_score": item.get("secondary_score"),
                "secondary_tier": item.get("secondary_tier"),
            })
        except Exception as exc:  # noqa: BLE001
            res = {"wallet": w, "label": item.get("label"), "ok": False, "error": repr(exc)[:1000]}
        results.append(res)
        print(json.dumps({"i": i, "n": len(wallets), "wallet": w, "label": item.get("label"), "copyability": res.get("copyability"), "score": res.get("score"), "txs": res.get("txs"), "positions": res.get("positions"), "error": res.get("error")}, ensure_ascii=False), flush=True)
    con.close()

    payload = {
        "generated_at": now_utc(),
        "mode": "read_only_imported_wallet_deep_pass",
        "selection": str(Path(args.selection).expanduser()),
        "db": str(Path(args.db).expanduser()),
        "limit": args.limit,
        "pages": args.pages,
        "include_wallet_api": args.include_wallet_api,
        "count": len(results),
        "results": results,
        "notes": [
            "Transfer-adjusted sample only; classification can change with deeper pagination and external price data.",
            "Copyability is a research bucket, not execution authority.",
            "Read-only: no signing, sending, swapping, alerting, posting, or wallet management.",
        ],
    }
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out), "count": len(results)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
