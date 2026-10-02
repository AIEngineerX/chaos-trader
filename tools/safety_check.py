#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".sql", ".sh", ".toml", ".cfg", ""}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "venv", ".venv", ".tmp", "node_modules"}
PRIVATE_DIR_NAMES = {"db", "owner", "alpha", "watchlists", "journal", "intake", "cache", "memories", "cron", "logs"}
# Files inside a private-named folder that the Hermes distribution ships on purpose.
SHIPPED_PATHS = {Path("cron/jobs.json")}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
]
BASE58_LONG = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{44,88}\b")
FORBIDDEN_EXECUTION_HOST = "trade.padre.gg"
# sha256 of the owner's lowercase macOS home path ("/users/<name>"); only the digest is kept here.
OWNER_HOME_DIGEST = "3bdc2354f4c7802c95986fedb0c85a28214651bc1e92edc4e078ad49bdb5be2a"
USERS_PATH = re.compile(r"/users/[A-Za-z0-9_.-]+", re.I)

ALLOW_BASE58_IN = (
    "test_",
    "SKILL.md",
    "README.md",
    "BOUNDARY.md",
    "ARCHITECTURE.md",
    "card_contract.md",
)


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def iter_files():
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel


def main() -> int:
    errors: list[str] = []
    for p, rel in iter_files():
        if set(rel.parts) & PRIVATE_DIR_NAMES and rel not in SHIPPED_PATHS:
            errors.append(f"private/runtime path committed: {rel}")
            continue
        if not is_text(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"non-text file in source tree: {rel}")
            continue
        if any(hashlib.sha256(m.group(0).lower().encode()).hexdigest() == OWNER_HOME_DIGEST for m in USERS_PATH.finditer(text)):
            errors.append(f"absolute local user path in source file: {rel}")
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                errors.append(f"secret-like material in {rel}: {pat.pattern}")
        # Padre/trade terminal URLs are forbidden in production/source docs. Tests and this scanner may name the host only to block it.
        if FORBIDDEN_EXECUTION_HOST in text and not (rel.name.startswith("test_") or rel == Path("tools/safety_check.py")):
            errors.append(f"execution-adjacent host outside tests: {rel}")
        if rel.parts[:2] != ("chaos_trader", "trading") and rel.parts[0] != "skills":
            continue
        if rel.name.startswith(ALLOW_BASE58_IN):
            continue
        # Do not block mints in code; block obvious owner/private wallet labels near raw base58.
        for m in BASE58_LONG.finditer(text):
            window = text[max(0, m.start()-60):m.end()+60].lower()
            if "owner" in window or "private" in window:
                errors.append(f"possible raw owner/private wallet literal in {rel}")
                break
    if errors:
        print("SAFETY CHECK FAILED", file=sys.stderr)
        for e in errors:
            print(f"- {e}", file=sys.stderr)
        return 1
    print("safety_check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
