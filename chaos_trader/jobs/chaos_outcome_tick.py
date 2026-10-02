#!/usr/bin/env python3
"""Locked no-agent tick for the outcome tracker: mark every read whose horizon is due, stamp a clean run."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev/test hosts
    fcntl = None
    import msvcrt

BOUNDARY = "read-only repricing; no execution"
SCRIPTS = Path(__file__).resolve().parents[1] / "trading" / "scripts"
TIMEOUT_SECONDS = 540
# One line. The next tick marks what is due now; a mark it reaches more than 5 minutes after its horizon is late.
SKIPPED = "☄️ OUTCOME TICK · skipped: a prior tick still holds the lock, so no mark was taken this time"


def profile_home() -> Path:
    for name in ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME"):
        raw = os.environ.get(name, "")
        if raw and raw.strip():
            return Path(raw.strip()).expanduser().resolve()
    return (Path.home() / ".chaos-trader").resolve()


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
    profile = profile_home()
    lock_path = profile / "trading" / "db" / "outcome_tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        if not acquire_lock(lock):
            print(SKIPPED)
            return 0
        env = {**os.environ, "CHAOS_HOME": str(profile), "HERMES_HOME": str(profile)}
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "signal_outcome_tracker.py")],
            cwd=str(profile / "trading"),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
        output = (proc.stdout or proc.stderr).strip()
        print(output or f"☄️ OUTCOME TICK · empty output · exit {proc.returncode}\n{BOUNDARY}")
        if proc.returncode != 0 and (proc.stderr or "").strip() and proc.stdout.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        if proc.returncode == 0:
            stamp = profile / "trading" / "state" / "outcome_tick_last_run"
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n", encoding="utf-8")
        return proc.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print(f"☄️ OUTCOME TICK · bounded {TIMEOUT_SECONDS}s timeout\n{BOUNDARY}")
        raise SystemExit(124)
