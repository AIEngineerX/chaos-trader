#!/usr/bin/env python3
"""Live check that every program id literal in the pool-detection scripts is an executable program.

Collects each base58 literal (32-44 chars) that sits on a line mentioning a program, pool or AMM
name, or inside a PROGRAM/HINTS constant block, in the three scripts that classify pools. Calls
getAccountInfo for each on SOLANA_RPC_URL (default: the public mainnet endpoint) and prints one
line per id. Exits non-zero if any id is not an executable program. Needs the network, so it is
not part of CI: run it by hand after editing a program id.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "chaos_trader" / "trading" / "scripts"
FILES = ("holder_resolver.py", "pumpfun_launch_read.py", "wallet_graph.py")
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

LITERAL = re.compile(r"[\"']([1-9A-HJ-NP-Za-km-z]{32,44})[\"']")
NAME_WORDS = re.compile(r"program|pool|amm|clmm|cpmm|pump|raydium|orca|meteora|jupiter|whirlpool", re.I)
BLOCK_START = re.compile(r"^[A-Z_]*(PROGRAM|HINTS)[A-Z_]*\s*(:[^=]+)?=.*[{\[(]\s*$")


def collect() -> dict[str, list[str]]:
    """Map each id to the file:line places it appears."""
    found: dict[str, list[str]] = {}
    for name in FILES:
        in_block = False
        for number, line in enumerate((SCRIPTS / name).read_text(encoding="utf-8").splitlines(), 1):
            if BLOCK_START.match(line):
                in_block = True
            wanted = in_block or bool(NAME_WORDS.search(line))
            if wanted:
                for literal in LITERAL.findall(line):
                    found.setdefault(literal, []).append(f"{name}:{number}")
            if in_block and re.match(r"^[}\])]", line):
                in_block = False
    return found


def get_account_info(rpc: str, address: str) -> dict | None:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [address, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}],
    }).encode()
    request = urllib.request.Request(rpc, data=body, headers={"Content-Type": "application/json", "User-Agent": "chaos-trader/check-program-ids"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if "error" in payload:
        raise RuntimeError(payload["error"].get("message", payload["error"]))
    return payload["result"]["value"]


def main() -> int:
    rpc = os.environ.get("SOLANA_RPC_URL") or DEFAULT_RPC
    found = collect()
    if not found:
        print("no program ids found; the collector patterns no longer match the scripts")
        return 1
    bad = 0
    for address, places in sorted(found.items(), key=lambda item: item[1][0]):
        try:
            value = get_account_info(rpc, address)
        except Exception as exc:  # network and RPC errors are reported per id, not hidden
            status = f"ERROR {exc}"
            bad += 1
        else:
            if value is None:
                status = "MISSING"
                bad += 1
            elif value.get("executable"):
                status = "executable"
            else:
                status = "not executable"
                bad += 1
        print(f"{status:<14} {address}  {', '.join(places)}")
        time.sleep(0.3)
    print(f"{len(found) - bad}/{len(found)} executable")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
