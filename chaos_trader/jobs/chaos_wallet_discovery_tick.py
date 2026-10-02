#!/usr/bin/env python3
"""No-agent cron wrapper: enrich a bounded batch of never-scored edge-wallets, then re-rank."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BOUNDARY = "read-only wallet discovery; no signing, orders, swaps, or execution"
SCRIPTS = Path(__file__).resolve().parents[1] / "trading" / "scripts"


def profile_home() -> Path:
    for name in ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME"):
        raw = os.environ.get(name, "")
        if raw and raw.strip():
            return Path(raw.strip()).expanduser().resolve()
    return (Path.home() / ".chaos-trader").resolve()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    profile = profile_home()
    env = {**os.environ, "CHAOS_HOME": str(profile), "HERMES_HOME": str(profile)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "chaos_cmd.py"), "wallets", "--discover", "5"],
        cwd=str(profile / "trading"),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=540,
    )
    output = (proc.stdout or proc.stderr).strip()
    print(output or f"☄️ WALLET DISCOVERY · empty output · exit {proc.returncode}\n{BOUNDARY}")
    return proc.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print(f"☄️ WALLET DISCOVERY · bounded 540s timeout\n{BOUNDARY}")
        raise SystemExit(124)
