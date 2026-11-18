#!/usr/bin/env python3
"""Scan repository history and ref names for private names and secret patterns.

By default scan all refs; --pre-push reads Git's update records on stdin and scans
non-deletion source/destination names and the full history of outgoing object IDs.
Prints redacted findings and exits 1, or `history scan: clean (<N> blobs)` and exits 0.
The checks are imported from the tree gates.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
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
    return subprocess.run(["git", "--no-replace-objects", *args], input=data,
                          capture_output=True, check=True).stdout


def object_list(tips: list[str] | None = None, *, names: bool = True) -> bytes:
    args = ["rev-list", "--objects"]
    if not names:
        args.append("--no-object-names")
    if tips is None:
        return _git([*args, "--all"])
    # An empty push (including deletion-only pushes) has no objects to scan.
    if not tips:
        return b""
    return _git([*args, "--stdin"], "".join(f"{sha}\n" for sha in tips).encode())


def push_inputs() -> tuple[list[str], list[str]]:
    """Read Git's pre-push protocol; never resolve the source expression ourselves."""
    tips, refs = set(), set()
    oid_size = len(_git(["hash-object", "--stdin"], b"").strip())
    for line in sys.stdin:
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("invalid pre-push record")
        local_ref, local_oid, remote_ref, remote_oid = fields
        if any(re.fullmatch(r"[0-9a-f]{" + str(oid_size) + "}", oid) is None
               for oid in (local_oid, remote_oid)):
            raise ValueError("invalid pre-push object ID")
        _git(["check-ref-format", remote_ref])
        if local_oid == "0" * oid_size:
            # Permit cleanup of a private destination: deletion sends no objects.
            continue
        tips.add(local_oid)
        refs.update((local_ref, remote_ref))
    return sorted(tips), sorted(refs)


def blob_paths(tips: list[str] | None = None) -> dict[str, str]:
    """Reachable blobs, including direct blob tips with no rev-list path."""
    first_path: dict[str, str] = {}
    for line in object_list(tips).decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        first_path.setdefault(sha, path or "<unnamed blob>")
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


def commit_scan_text(data: bytes) -> str:
    """Mask only the exact public display name in its paired commit identity.

    Use a case-sensitive digest, unlike the tree gate's lowercase fingerprints.
    Preserve emails, other headers, and the entire message for ordinary scanning.
    """
    headers, separator, message = data.partition(b"\n\n")
    lines = headers.split(b"\n")
    for index, line in enumerate(lines):
        match = re.fullmatch(
            rb"(author|committer) ([^<>\n]+) "
            rb"<195990077\+AIEngineerX@users\.noreply\.github\.com> [0-9]+ [+-][0-9]{4}",
            line,
        )
        if (match and hashlib.sha256(match[2]).hexdigest()
                == "df1bc4984a055e10b2863eb03abf11701cb63a1e2371dc398889e8291812fb80"):
            lines[index] = line[:match.start(2)] + b"Public Account" + line[match.end(2):]
    return (b"\n".join(lines) + separator + message).decode("utf-8", "replace")


def metadata_hits(tips: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Scan commit/tag metadata and tree names without echoing private values."""
    ids = object_list(tips, names=False)
    objects = _git(["cat-file", "--batch-check=%(objectname) %(objecttype)"], ids)
    selected = {sha: kind for sha, kind in
                (line.split() for line in objects.decode().splitlines())
                if kind in {"commit", "tag", "tree"}}
    hits = []
    for sha, data in read_blobs(sorted(selected)):
        text = commit_scan_text(data) if selected[sha] == "commit" else data.decode("utf-8", "replace")
        for kind in hits_in(text):
            hits.append(("<" + selected[sha] + " metadata>", kind, sha))
    return hits


def scan(tips: list[str] | None, refs: list[str]) -> int:
    paths = blob_paths(tips)
    hits: list[tuple[str, str, str]] = []
    ref_kinds = sorted({kind for ref in refs for kind in hits_in(ref)})
    scanned = 0
    for sha, data in read_blobs(sorted(paths)):
        path = paths[sha]
        if not safety.is_text(Path(path)) and b"\0" in data:
            continue
        scanned += 1
        for kind in hits_in(data.decode("utf-8", "replace")):
            hits.append((path, kind, sha))
    hits.extend(metadata_hits(tips))
    if not hits and not ref_kinds:
        print(f"history scan: clean ({scanned} blobs)")
        return 0
    for kind in ref_kinds:
        print(f"{kind} <redacted ref>")
    for path, kind, sha in sorted(hits):
        safe_path = "<redacted path>" if hits_in(path) else path
        print(f"{kind} {sha[:10]} {safe_path}")
    summary = f"history scan: {len(hits)} hits in {len({h[2] for h in hits})} blobs ({scanned} blobs scanned)"
    if ref_kinds:
        summary += f"; {len(ref_kinds)} ref finding kinds"
    print(summary)
    return 1


def main() -> int:
    try:
        if sys.argv[1:] == ["--pre-push"]:
            tips, refs = push_inputs()
        elif not sys.argv[1:]:
            tips = None
            refs = _git(["for-each-ref", "--format=%(refname)"]).decode("utf-8", "replace").splitlines()
        else:
            raise ValueError("invalid arguments")
        return scan(tips, refs)
    except (ValueError, OSError, subprocess.CalledProcessError):
        # Git errors and input records can contain private paths/ref names.
        print("history scan: unable to validate history or push input; refusing", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
