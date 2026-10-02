#!/usr/bin/env python3
"""Locked no-agent wrapper for a bounded Alpha Elite paper burn-in."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev/test hosts
    fcntl = None
    import msvcrt

SCRIPTS = Path(__file__).resolve().parents[1] / "trading" / "scripts"


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


def profile_home() -> Path:
    for name in ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME"):
        raw = os.environ.get(name, "")
        if raw and raw.strip():
            return Path(raw.strip()).expanduser().resolve()
    return (Path.home() / ".chaos-trader").resolve()


def command(*, limit: int, from_event_id: int | None, raw: bool) -> list[str]:
    cmd = [
        sys.executable,
        str(SCRIPTS / "elite_paper_cohort.py"),
        "cycle",
        "--limit",
        str(limit),
    ]
    if from_event_id is not None:
        cmd.extend(["--from-event-id", str(from_event_id)])
    if raw:
        cmd.append("--raw")
    return cmd


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Run a locked bounded Alpha Elite paper burn-in")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--from-event-id", type=int)
    ap.add_argument("--max-cycles", type=int, default=1)
    ap.add_argument("--interval-seconds", type=int, default=300)
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    profile = profile_home()
    lock_path = profile / "trading" / "db" / "alpha_elite_paper.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        if not acquire_lock(lock):
            print("Alpha Elite paper cycle already running; skipped")
            return 0
        cycles = max(1, min(args.max_cycles, 10_000))
        interval = max(1, min(args.interval_seconds, 86_400))
        for index in range(cycles):
            completed = subprocess.run(
                command(
                    limit=max(1, min(args.limit, 100)),
                    from_event_id=args.from_event_id if index == 0 else None,
                    raw=args.raw,
                ),
                check=False,
            )
            if completed.returncode != 0:
                return int(completed.returncode)
            if index + 1 < cycles:
                time.sleep(interval)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
