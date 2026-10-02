"""Resolve the single state root every chaos-trader script reads and writes under."""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ENV_PRECEDENCE = ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME")
DEFAULT_DIRNAME = ".chaos-trader"


def chaos_home() -> Path:
    for name in ENV_PRECEDENCE:
        raw = os.environ.get(name, "")
        if raw and raw.strip():
            return Path(raw.strip()).expanduser().resolve()
    return (Path.home() / DEFAULT_DIRNAME).resolve()


def ensure_utf8_stdio() -> None:
    """Switch stdout/stderr to UTF-8 so emoji output cannot crash on a cp1252 console or pipe.

    Streams swapped for in-memory buffers (tests, redirect_stdout) have no reconfigure and are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def corrupt_db(exc: sqlite3.DatabaseError) -> bool:
    """True when SQLite says the file itself is damaged, not merely locked or unreadable for now."""
    return "file is not a database" in str(exc) or "malformed" in str(exc)


REFILL_WALLETS = "run the ingest again"
REFILL_PAPER_BOOK = "the next paper tick starts a new book"


def corrupt_line(path: Path, refill: str = REFILL_WALLETS) -> str:
    return f"{path} is not a readable SQLite database. Move it aside and {refill}."


def unreadable_db(path: Path, exc: sqlite3.DatabaseError, refill: str = REFILL_WALLETS) -> str:
    """One line for a SQLite file a command could not read. Only a corrupt file is worth moving aside, and
    `refill` says what rebuilds it; a lock or a permission error passes, so it says to try again and shows
    SQLite's own words."""
    if corrupt_db(exc):
        return corrupt_line(path, refill)
    return f"Could not read {path}: {exc}. Try again; if it keeps failing, check that the file is readable and no other process holds it."


def intact(path: Path) -> bool:
    """False only when SQLite's quick_check finds the file damaged, on any page. A missing, locked, or
    unreadable-for-now file counts as intact: that is not damage. Where SQLite first notices damage
    differs by platform and version, so this reads every page instead of trusting the first query."""
    if not path.exists():
        return True
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as con:
            row = con.execute("PRAGMA quick_check(1)").fetchone()
    except sqlite3.DatabaseError as exc:
        return not corrupt_db(exc)
    return row is not None and row[0] == "ok"


def stop_if_corrupt(path: Path, refill: str = REFILL_WALLETS) -> None:
    """For readers that treat a file they cannot open as absent: a corrupt file stops the command here
    with one line instead. A missing, locked, or unreadable-for-now file passes, so the caller's
    nothing-yet path still runs."""
    if not intact(path):
        raise SystemExit(corrupt_line(path, refill))


def db_failure(exc: sqlite3.DatabaseError, candidates: list[tuple[Path, str]]) -> str:
    """The one line for a top-level catch, where the failing file is not known: it names the first
    candidate (path, refill) that is damaged; with none damaged, SQLite's own words and a retry."""
    for path, refill in candidates:
        if not intact(path):
            return corrupt_line(path, refill)
    return f"A database read failed: {exc}. Try again; if it keeps failing, check that the files under trading/db are readable and no other process holds them."


ensure_utf8_stdio()
