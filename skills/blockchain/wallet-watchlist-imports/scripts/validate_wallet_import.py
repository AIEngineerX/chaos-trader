#!/usr/bin/env python3
"""Validate a bare-array Solana wallet import.

Usage:
  chaos run validate_wallet_import FILE.json
  chaos run validate_wallet_import FILE.json --fields trackedWalletAddress,name,emoji,alertsOn --max-name 48 --unique-emoji
"""

import argparse
import json
from pathlib import Path

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        if char not in ALPHABET:
            raise ValueError(f"non-Base58 character: {char!r}")
        number = number * 58 + ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + body


def duplicates(values):
    seen, dupes = set(), set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return sorted(dupes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument(
        "--fields",
        default="trackedWalletAddress,name,emoji,alertsOn",
        help="comma-separated exact field set",
    )
    parser.add_argument("--max-name", type=int)
    parser.add_argument("--unique-emoji", action="store_true")
    args = parser.parse_args()

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    errors = []
    expected = set(args.fields.split(","))
    if not isinstance(data, list):
        errors.append("root must be a bare JSON array")
        rows = []
    else:
        rows = data

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"row {index}: must be an object")
            continue
        if set(row) != expected:
            errors.append(
                f"row {index}: fields {sorted(row)} != expected {sorted(expected)}"
            )
        address = row.get("trackedWalletAddress")
        if not isinstance(address, str):
            errors.append(f"row {index}: trackedWalletAddress must be a string")
        else:
            try:
                if len(b58decode(address)) != 32:
                    errors.append(f"row {index}: address does not decode to 32 bytes")
            except ValueError as exc:
                errors.append(f"row {index}: invalid address: {exc}")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"row {index}: name must be a non-empty string")
        elif args.max_name is not None and len(name) > args.max_name:
            errors.append(f"row {index}: name exceeds {args.max_name} characters")
        if "alertsOn" in expected and not isinstance(row.get("alertsOn"), bool):
            errors.append(f"row {index}: alertsOn must be boolean")
        if "emoji" in expected and not isinstance(row.get("emoji"), str):
            errors.append(f"row {index}: emoji must be a string")

    for field in ("trackedWalletAddress", "name"):
        dupes = duplicates([row.get(field) for row in rows if isinstance(row, dict)])
        if dupes:
            errors.append(f"duplicate {field}: {dupes}")
    if args.unique_emoji:
        dupes = duplicates([row.get("emoji") for row in rows if isinstance(row, dict)])
        if dupes:
            errors.append(f"duplicate emoji: {dupes}")

    report = {
        "ok": not errors,
        "rows": len(rows),
        "expected_fields": sorted(expected),
        "max_name": args.max_name,
        "unique_emoji_required": args.unique_emoji,
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
