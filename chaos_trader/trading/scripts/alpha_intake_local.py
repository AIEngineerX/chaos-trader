#!/usr/bin/env python3
"""Read-only alpha intake scorer for Chaos.

Accepts local text/JSON exports or pasted notes. Does not connect to Telegram/X,
join channels, send messages, mark reads, or post. It converts raw alpha chatter
into claims requiring verification.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$[A-Za-z][A-Za-z0-9_]{1,12}\b")
URL_RE = re.compile(r"https?://\S+")
HYPE_WORDS = re.compile(r"\b(send|sending|moon|100x|ape|gem|alpha|insane|guaranteed|easy|pump)\b", re.I)
RISK_WORDS = re.compile(r"\b(dev|bundle|bundled|sniper|rug|mint auth|freeze|lp|locked|unlocked|insider|cto|migration|graduated)\b", re.I)
PRIMARY_HINTS = re.compile(r"\b(contract|ca|mint|tx|signature|deployer|wallet|source|announcement|docs|github|snapshot)\b", re.I)
MINT_CONTEXT_RE = re.compile(r"\b(ca|contract|mint|token)\b", re.I)
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|bearer|private[_-]?key|seed|mnemonic)\s*[:=]\s*[^\s]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"),
)


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<REDACTED_SECRET>", redacted)
    return redacted


def contextual_mints(text: str, candidates: list[str]) -> list[str]:
    mints = []
    for candidate in candidates:
        pattern = re.compile(rf"\b(ca|contract|mint|token)\b\s*[:=#-]?\s*{re.escape(candidate)}\b", re.I)
        if pattern.search(text):
            mints.append(candidate)
    return sorted(set(mints))


def load_items(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(errors="ignore")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        if isinstance(data, list):
            return [normalize_json_item(x) for x in data]
        if isinstance(data, dict):
            if isinstance(data.get("messages"), list):
                return [normalize_json_item(x) for x in data["messages"]]
            return [normalize_json_item(data)]
    items = []
    for idx, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if line:
            items.append({"source": path.name, "time": None, "text": line, "line": idx})
    return items


def normalize_json_item(x: Any) -> dict[str, Any]:
    if not isinstance(x, dict):
        return {"source": "json", "time": None, "text": str(x)}
    text = x.get("text") or x.get("message") or x.get("content") or x.get("body") or ""
    if isinstance(text, list):
        text = " ".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in text)
    return {
        "source": redact_text(str(x.get("source") or x.get("from") or x.get("author") or x.get("channel") or "json")),
        "time": redact_text(str(x.get("date") or x.get("time") or x.get("timestamp") or "")) or None,
        "text": str(text),
    }


def score_item(item: dict[str, Any]) -> dict[str, Any]:
    text = redact_text(item["text"])
    candidate_addresses = sorted(set(MINT_RE.findall(text)))
    mints = contextual_mints(text, candidate_addresses)
    tickers = TICKER_RE.findall(text)
    urls = [redact_text(url) for url in URL_RE.findall(text)]
    hype = len(HYPE_WORDS.findall(text))
    risk = len(RISK_WORDS.findall(text))
    primary = len(PRIMARY_HINTS.findall(text)) + len(mints) + len(urls)
    score = primary * 2 + risk - hype
    return {
        **item,
        "text": text,
        "tickers": sorted(set(tickers)),
        "candidate_base58_addresses": candidate_addresses,
        "mints": sorted(set(mints)),
        "urls": urls[:5],
        "hype_terms": hype,
        "risk_terms": risk,
        "primary_evidence_hints": primary,
        "claim_score": score,
        "classification": "verify_first" if primary == 0 else ("useful_claim" if score >= 2 else "weak_claim"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score local X/TG alpha notes without connecting to any platform.")
    parser.add_argument("inputs", nargs="+", help="Local .txt/.json export files")
    parser.add_argument("--raw", action="store_true", help="Print JSON envelope")
    parser.add_argument("--top", type=int, default=20, help="Max claims in digest")
    args = parser.parse_args()
    top = min(50, max(1, args.top))

    scored = []
    for raw_path in args.inputs:
        path = Path(raw_path).expanduser()
        if not path.exists() or not path.is_file():
            raise SystemExit(json.dumps({"ok": False, "error": "input file not found", "path": str(path)}, indent=2))
        scored.extend(score_item(item) for item in load_items(path))

    scored.sort(key=lambda x: (x["claim_score"], x["primary_evidence_hints"]), reverse=True)
    ticker_counts = Counter(t for item in scored for t in item["tickers"])
    mint_counts = Counter(m for item in scored for m in item["mints"])
    source_counts = Counter(item["source"] for item in scored)
    classes = Counter(item["classification"] for item in scored)

    result = {
        "ok": True,
        "mode": "read_only_alpha_intake_local",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "item_count": len(scored),
        "class_counts": dict(classes),
        "top_tickers": ticker_counts.most_common(20),
        "top_mints": mint_counts.most_common(20),
        "sources": source_counts.most_common(20),
        "top_claims": scored[:top],
        "boundary": "local read-only scoring only; redacts common secret patterns before output; no X/TG connection, posting, joining, forwarding, marking read, or alerts",
    }
    if args.raw:
        print(json.dumps(result, indent=2))
        return
    print("## Alpha Intake Digest")
    print(f"- Items: {result['item_count']}")
    print(f"- Class counts: {result['class_counts']}")
    print(f"- Top tickers: {result['top_tickers'][:10]}")
    print(f"- Top mints: {result['top_mints'][:10]}")
    print("\n## Top Claims")
    for item in result["top_claims"][:top]:
        print(f"- [{item['classification']}] score={item['claim_score']} source={item['source']} tickers={item['tickers']} mints={item['mints']} candidates={item['candidate_base58_addresses']} :: {item['text'][:240]}")
    print("\n## Next")
    print("Verify high-scoring claims with Helius/onchain reads before treating them as signal.")


if __name__ == "__main__":
    main()
