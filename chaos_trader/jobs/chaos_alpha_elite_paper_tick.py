#!/usr/bin/env python3
"""One-shot locked no-agent tick for the Alpha Elite paper cohort."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows dev/test hosts
    fcntl = None
    import msvcrt

BOUNDARY = "paper/simulated Alpha Elite cohort only; no signing, orders, swaps, or live execution"
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


def consumer_path() -> Path:
    return SCRIPTS / "elite_paper_cohort.py"


def run_cycle_once(profile: Path) -> int:
    path = consumer_path()
    script_dir = str(path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("_elite_paper_cohort_tick", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load paper consumer: {path}")
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)

    evidence_path = profile / "trading" / "db" / "smart_wallets.sqlite"
    if not evidence_path.exists():
        print(consumer.NO_INGEST)
        return 0
    started = consumer.now_utc()
    con = consumer.connect(profile / "trading" / "db" / "alpha_elite_paper.sqlite")
    evidence = None
    try:
        roster = consumer.load_roster(consumer.DEFAULT_ROSTER)
        evidence = consumer.connect_evidence(evidence_path)
        result = consumer.run_cycle(
            con,
            evidence,
            roster,
            checked_at=started,
            from_event_id=None,
            limit=5,
        )
        result["receipt_path"] = consumer.save_receipt(
            con,
            profile / "trading" / "reports" / "alpha_elite_paper",
            "cycle",
            started,
            result,
        )
    finally:
        if evidence is not None:
            evidence.close()
        con.close()
    print(consumer.compact(result, "cycle"))
    return 0 if result.get("ok") else 2


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    profile = profile_home()
    lock_path = profile / "trading" / "db" / "alpha_elite_paper.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        if not acquire_lock(lock):
            print(f"☄️ ALPHA ELITE PAPER · skipped; prior tick active\n{BOUNDARY}")
            return 0
        return run_cycle_once(profile)


if __name__ == "__main__":
    raise SystemExit(main())
