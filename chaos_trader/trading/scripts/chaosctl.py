#!/usr/bin/env python3
"""Local Chaos operator workbench.

Examples:
  chaosctl.py status
  chaosctl.py test
  chaosctl.py sweep -- --limit 5 --deep 1 --no-x
  chaosctl.py token <mint> -- --no-x
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
PY = os.environ.get("CHAOS_PYTHON", sys.executable)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("CHAOS_HOME", str(PROFILE_HOME))
    env.setdefault("HERMES_HOME", str(PROFILE_HOME))
    env.setdefault("PYTHONPATH", os.pathsep.join(p for p in [str(SCRIPT_DIR), os.environ.get("HERMES_AGENT_SRC"), env.get("PYTHONPATH", "")] if p))
    return env


def run(args: list[str], *, timeout: int = 600) -> int:
    proc = subprocess.run(args, cwd=str(SCRIPT_DIR), env=_env(), text=True, timeout=timeout)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Chaos local operator workbench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("test")
    sub.add_parser("gateway-status")
    sub.add_parser("last-artifacts")
    sp = sub.add_parser("sweep")
    sp.add_argument("extra", nargs=argparse.REMAINDER)
    tp = sub.add_parser("token")
    tp.add_argument("mint")
    tp.add_argument("extra", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if args.cmd == "status":
        return run([PY, "chaos_status.py"])
    if args.cmd == "test":
        return run([PY, "-m", "unittest", "test_chaos_cmd.py", "test_wallet_output.py", "-v"])
    if args.cmd == "gateway-status":
        return run(["hermes", "--profile", "chaos", "gateway", "status"], timeout=120)
    if args.cmd == "last-artifacts":
        return run([PY, "chaos_status.py"])
    if args.cmd == "sweep":
        extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
        return run([PY, "chaos_cmd.py", "sweep", *extra])
    if args.cmd == "token":
        extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
        return run([PY, "chaos_cmd.py", "token", args.mint, *extra])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
