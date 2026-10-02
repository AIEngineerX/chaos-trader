#!/usr/bin/env python3
"""Backfill Chaos token-event artifacts into the signal ledger.

Reads saved token_event_*.json files and records their original verdict/market
state so outcome tracking can begin before future scans accumulate.
"""
from __future__ import annotations
import os

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from signal_ledger import record_signal  # noqa: E402

DEFAULT_GLOB_ROOT = PROFILE_HOME / "trading" / "alpha" / "sweeps"


def iter_artifacts(root: Path) -> list[Path]:
    return sorted(root.glob("**/token_event_*.json"))


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill token_event JSON artifacts into Chaos signal ledger")
    p.add_argument("--root", default=str(DEFAULT_GLOB_ROOT))
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()

    paths = iter_artifacts(Path(args.root).expanduser())[: max(1, min(args.limit, 5000))]
    rows = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.setdefault("json_path", str(path))
            if args.dry_run:
                rows.append({"ok": True, "path": str(path), "mint": payload.get("mint"), "dry_run": True})
            else:
                rows.append({**record_signal(payload, source_command="artifact_backfill"), "path": str(path), "mint": payload.get("mint")})
        except Exception as exc:
            rows.append({"ok": False, "path": str(path), "error": str(exc)})
    if args.raw:
        print(json.dumps({"ok": True, "count": len(rows), "rows": rows}, indent=2, ensure_ascii=False))
        return
    ok_count = sum(1 for r in rows if r.get("ok"))
    print(f"☄️ Backfill artifacts · {ok_count}/{len(rows)} recorded")
    for row in rows[:12]:
        status = "ok" if row.get("ok") else "error"
        mint = row.get("mint") or "unknown"
        print(f"- {status} {mint[:6]}…{mint[-4:] if len(mint) > 4 else mint} {row.get('path')}")
    print("read-only ledger backfill; no execution")


if __name__ == "__main__":
    main()
