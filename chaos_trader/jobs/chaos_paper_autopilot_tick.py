#!/usr/bin/env python3
"""Locked no-agent tick for the paper autopilot: one bounded discover/decide/monitor pass."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev/test hosts
    fcntl = None
    import msvcrt

BOUNDARY = "paper/simulated autopilot only; no signing, orders, swaps, or live execution"
SCRIPTS = Path(__file__).resolve().parents[1] / "trading" / "scripts"


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


def command(*, limit: int, analyze_top: int, with_x: bool) -> list[str]:
    cmd = [
        sys.executable,
        str(SCRIPTS / "chaos_paper_autopilot.py"),
        "--once",
        "--limit",
        str(limit),
        "--analyze-top",
        str(analyze_top),
    ]
    if with_x:
        cmd.append("--with-x")
    return cmd


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Run one locked bounded paper-autopilot tick")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--analyze-top", type=int, default=2)
    ap.add_argument("--with-x", action="store_true")
    ap.add_argument("--timeout", type=int, default=540)
    args = ap.parse_args()
    profile = profile_home()
    lock_path = profile / "trading" / "db" / "paper_autopilot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        if not acquire_lock(lock):
            print(f"☄️ PAPER AUTOPILOT · skipped; prior tick active\n{BOUNDARY}")
            return 0
        env = {**os.environ, "CHAOS_HOME": str(profile), "HERMES_HOME": str(profile)}
        proc = subprocess.run(
            command(
                limit=max(1, min(args.limit, 50)),
                analyze_top=max(0, min(args.analyze_top, 10)),
                with_x=bool(args.with_x),
            ),
            cwd=str(profile / "trading"),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=max(60, min(args.timeout, 3600)),
        )
        output = (proc.stdout or proc.stderr).strip()
        print(output or f"☄️ PAPER AUTOPILOT · empty output · exit {proc.returncode}\n{BOUNDARY}")
        if proc.returncode != 0 and (proc.stderr or "").strip() and proc.stdout.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        return proc.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print(f"☄️ PAPER AUTOPILOT · bounded timeout\n{BOUNDARY}")
        raise SystemExit(124)
