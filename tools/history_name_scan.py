#!/usr/bin/env python3
"""Scan every blob in the git history of the current directory's repository for the private names
the vendor gate bans and for the secret patterns the safety check bans.

Prints one line per hit, `<kind> <short sha> <first path seen for that blob>`, and exits 1; with no hit
it prints `history scan: clean (<N> blobs)` and exits 0. The checks are imported from the gates
themselves, so the history is held to the same rules as the tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor = _load("test_no_vendor_names", ROOT / "tests" / "test_no_vendor_names.py")
safety = _load("safety_check", ROOT / "tools" / "safety_check.py")


def _git(args: list[str], data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def blob_paths() -> dict[str, str]:
    """Every blob reachable from any ref, mapped to the first path rev-list names it under."""
    first_path: dict[str, str] = {}
    for line in _git(["rev-list", "--objects", "--all"]).decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        if path:
            first_path.setdefault(sha, path)
    ids = "".join(f"{sha}\n" for sha in first_path).encode()
    kinds = _git(["cat-file", "--batch-check=%(objectname) %(objecttype)"], ids).decode().split()
    blobs = {sha for sha, kind in zip(kinds[::2], kinds[1::2]) if kind == "blob"}
    return {sha: path for sha, path in first_path.items() if sha in blobs}


def read_blobs(shas: list[str]):
    """Yield (sha, content) for each blob, read in one `git cat-file --batch` stream."""
    out = _git(["cat-file", "--batch"], "".join(f"{sha}\n" for sha in shas).encode())
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        sha, _kind, size = out[pos:header_end].decode().split()
        start = header_end + 1
        yield sha, out[start:start + int(size)]
        pos = start + int(size) + 1


def hits_in(text: str) -> list[str]:
    kinds = set(vendor.banned_kinds(text))
    if any(pat.search(text) for pat in safety.SECRET_PATTERNS):
        kinds.add("secret-like material")
    return sorted(kinds)


def main() -> int:
    paths = blob_paths()
    hits: list[tuple[str, str, str]] = []
    scanned = 0
    for sha, data in read_blobs(sorted(paths)):
        path = paths[sha]
        if not safety.is_text(Path(path)) and b"\0" in data:
            continue
        scanned += 1
        for kind in hits_in(data.decode("utf-8", "replace")):
            hits.append((path, kind, sha))
    if not hits:
        print(f"history scan: clean ({scanned} blobs)")
        return 0
    for path, kind, sha in sorted(hits):
        print(f"{kind} {sha[:10]} {path}")
    print(f"history scan: {len(hits)} hits in {len({h[2] for h in hits})} blobs ({scanned} blobs scanned)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
