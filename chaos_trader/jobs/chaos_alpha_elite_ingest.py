#!/usr/bin/env python3
"""Locked no-agent cron wrapper for bounded Alpha Elite ingestion."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev/test hosts
    fcntl = None
    import msvcrt

def profile_home() -> Path:
    for name in ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME"):
        raw = os.environ.get(name, "")
        if raw and raw.strip():
            return Path(raw.strip()).expanduser().resolve()
    return (Path.home() / ".chaos-trader").resolve()


PROFILE = profile_home()
PYTHON = Path(os.environ.get("CHAOS_PYTHON", sys.executable)).expanduser()
SCRIPTS = Path(__file__).resolve().parents[1] / "trading" / "scripts"
PIPELINE = SCRIPTS / "elite_wallet_pipeline.py"
LOCK = PROFILE / "trading" / "db" / ".alpha_elite_ingest.lock"
BOUNDARY = "smart-wallet DB only; no paper, X, Dex, wallet, signing, orders, routing, swaps, or execution"


def acquire_lock(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        if not acquire_lock(lock):
            print(f"☄️ ALPHA ELITE INGEST · skipped; prior bounded run still active\n{BOUNDARY}")
            return 0
        env = {
            **os.environ,
            "CHAOS_HOME": str(PROFILE),
            "HERMES_HOME": str(PROFILE),
        }
        proc = subprocess.run(
            [
                str(PYTHON),
                str(PIPELINE),
                "ingest",
                "--history-limit",
                "50",
                "--pages",
                "1",
            ],
            cwd=str(PROFILE / "trading"),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=540,
        )
        output = (proc.stdout or proc.stderr).strip()
        print(output or f"☄️ ALPHA ELITE INGEST · empty output · exit {proc.returncode}\n{BOUNDARY}")
        if proc.returncode != 0 and (proc.stderr or "").strip() and proc.stdout.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        return proc.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print(f"☄️ ALPHA ELITE INGEST · bounded 540s timeout\n{BOUNDARY}")
        raise SystemExit(124)
