"""Resolve the single state root every chaos-trader script reads and writes under."""
from __future__ import annotations

import os
import sqlite3
import sys
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


def unreadable_db(path: Path, exc: sqlite3.DatabaseError) -> str:
    """One line for a SQLite file a command could not read. Only a corrupt file is worth moving aside;
    a lock or a permission error passes, so it says to try again and shows SQLite's own words."""
    if corrupt_db(exc):
        return f"{path} is not a readable SQLite database. Move it aside and run the ingest again."
    return f"Could not read {path}: {exc}. Try again; if it keeps failing, check that the file is readable and no other process holds it."


ensure_utf8_stdio()
