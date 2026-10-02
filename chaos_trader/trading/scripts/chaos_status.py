#!/usr/bin/env python3
"""Emit a compact machine-readable chaos-trader status."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from chaos_home import chaos_home  # noqa: E402
import x_provider  # noqa: E402
PROFILE_HOME = chaos_home()


def _has_env_key(name: str) -> bool:
    env_path = PROFILE_HOME / ".env"
    if not env_path.exists():
        return bool(os.environ.get(name))
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().startswith(f"{name}="):
            return bool(line.split("=", 1)[1].strip())
    return bool(os.environ.get(name))


def _latest_artifacts() -> list[str]:
    root = PROFILE_HOME / "trading" / "alpha" / "sweeps"
    if not root.exists():
        return []
    files = sorted(root.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p.relative_to(PROFILE_HOME)) for p in files[:8]]


def _stale_docs() -> list[str]:
    stale = []
    for name in ("AUDIT_STATUS.md", "CONTROL_CONTRACT.md"):
        p = PROFILE_HOME / name
        if p.exists() and "Status: current" not in p.read_text(encoding="utf-8", errors="ignore")[:400]:
            stale.append(name)
    return stale


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        return sum(1 for line in fh if line.strip())


def _event_tape_status() -> dict:
    path = PROFILE_HOME / "trading" / "alpha" / "event_tape.jsonl"
    return {
        "path": str(path),
        "exists": path.exists(),
        "rows": _count_lines(path),
        "modified_at_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.exists() else None,
    }


def _signal_ledger_status() -> dict:
    path = PROFILE_HOME / "trading" / "db" / "signal_ledger.sqlite"
    out = {"path": str(path), "exists": path.exists(), "signals": 0, "outcomes": 0}
    if not path.exists():
        return out
    try:
        con = sqlite3.connect(path)
        names = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "signals" in names:
            out["signals"] = con.execute("SELECT count(*) FROM signals").fetchone()[0]
        if "outcomes" in names:
            out["outcomes"] = con.execute("SELECT count(*) FROM outcomes").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    return out


def _freshness_status() -> dict:
    try:
        import alpha_tape
        con = alpha_tape.connect_ro(alpha_tape.DB_PATH)
        try:
            return alpha_tape.tape_freshness(con)
        finally:
            if con is not None:
                con.close()
    except Exception as exc:
        return {"status": "unknown", "error": str(exc)}


def _paper_status() -> dict:
    try:
        import alpha_paper_trade
        payload = alpha_paper_trade.run(limit=500)
        return {
            "ok": payload.get("ok"),
            "paper_trades": payload.get("paper_trades"),
            "summary": payload.get("summary") or {},
            "error": payload.get("error"),
            "caveat": payload.get("caveat"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cron_jobs": "none" if not (PROFILE_HOME / "cron" / "jobs.json").exists() else "check cron list",
        "rpc_configured": _has_env_key("SOLANA_RPC_URL") or _has_env_key("HELIUS_API_KEY"),
        "helius_key_present": _has_env_key("HELIUS_API_KEY"),
        "x_provider": x_provider.provider_name(),
        "latest_artifacts": _latest_artifacts(),
        "alpha_grade": {
            "fast_lane": "local alpha tape with freshness downgrade",
            "freshness": _freshness_status(),
            "event_tape": _event_tape_status(),
            "signal_ledger": _signal_ledger_status(),
            "paper_trade": _paper_status(),
            "live_ingest_required_for_current_alpha": True,
        },
        "stale_docs": _stale_docs(),
        "boundary": "read-only; no execution",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
