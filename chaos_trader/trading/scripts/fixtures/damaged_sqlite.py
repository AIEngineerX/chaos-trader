"""Damaged SQLite files in the shapes that reach different code on different platforms.

A plain junk file fails at the first statement on some SQLite builds and only at the first table read on
others. These two shapes make the late failure happen on every platform, so a test sees the path a Linux
runner sees.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def junk_with_header(path: Path) -> None:
    """A valid 16-byte SQLite header followed by garbage."""
    path.write_bytes(b"SQLite format 3\x00" + bytes(range(256)) * 24)


def damage_table(path: Path, table: str) -> None:
    """Overwrite one table's root page in an existing, closed database. Page 1, with the header and the
    schema, stays intact, so the file opens and lists its tables everywhere; the first read of that table
    fails with "database disk image is malformed"."""
    with closing(sqlite3.connect(path)) as con:
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        root = con.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    data = bytearray(path.read_bytes())
    data[(root - 1) * page_size:root * page_size] = b"\xff" * page_size
    path.write_bytes(bytes(data))
