#!/usr/bin/env python3
"""Chaos Event Tape v1 scaffold.

Local, read-only event tape for later outcome learning.  The live fetch path uses
only public DEXScreener API endpoints and writes local JSONL; it does not connect
wallets, sign, trade, create alerts/webhooks, scrape Telegram, or configure any
external service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_OUT = PROFILE_HOME / "trading" / "alpha" / "event_tape.jsonl"
DEX_BASE = "https://api.dexscreener.com"
UA = "ChaosEventTape/1.0 read-only"
SCHEMA = "chaos_event_tape.v1"
BOUNDARY = "read-only official/public market event tape; no execution, wallets, scraping, alerts, webhooks, or credentials"

OFFICIAL_DEX_ENDPOINTS = {
    "dex_profiles_latest": "/token-profiles/latest/v1",
    "dex_boosts_latest": "/token-boosts/latest/v1",
    "dex_boosts_top": "/token-boosts/top/v1",
    "dex_ads_latest": "/ads/latest/v1",
}
VALID_EVENT_TYPES = {"dex_profile_update", "dex_boost", "dex_ad", "dex_paid_order"}
SUMMARY_TEXT_LIMIT = 500
SUMMARY_LIST_LIMIT = 10


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha_id(parts: dict[str, Any]) -> str:
    return "cet1_" + hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()[:32]


def ts_to_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    # DEXScreener paymentTimestamp values are milliseconds; tolerate seconds too.
    if ts > 10_000_000_000:
        ts = ts / 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def safe_text(value: Any, limit: int = SUMMARY_TEXT_LIMIT) -> str | None:
    """Return display/LLM-safe summary text from untrusted public metadata.

    Raw public rows may still be stored under `raw` when explicitly requested;
    normalized summaries are intentionally inert and bounded.
    """
    if value is None:
        return None
    text = str(value)
    text = "".join(ch if (ch == "\n" or ch == "\t" or ord(ch) >= 32) else " " for ch in text)
    text = text.replace("[", "(").replace("]", ")").replace("<", "‹").replace(">", "›")
    text = text.replace("tg://", "tg_//").replace("t.me/", "t_me/")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or None


def safe_url(value: Any) -> str | None:
    """Allow only deterministic/public DEXScreener URLs in normalized summaries."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() != "dexscreener.com":
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", "", ""))


def safe_summary_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in {"url", "icon", "header", "openGraph"}:
        return safe_url(value)
    if key == "links":
        # Keep public-link presence as non-clickable, sanitized text only.
        if not isinstance(value, list):
            return None
        safe_links = []
        for item in value[:SUMMARY_LIST_LIMIT]:
            if not isinstance(item, dict):
                continue
            safe_links.append({
                "type": safe_text(item.get("type") or item.get("label"), 80),
                "label": safe_text(item.get("label"), 80),
            })
        return [link for link in safe_links if any(link.values())] or None
    if isinstance(value, str):
        return safe_text(value)
    return value


def fetch_json(path: str, timeout: int = 20) -> Any:
    url = DEX_BASE + path
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ordered_endpoint(chain_id: str, token_address: str) -> str:
    chain = urllib.parse.quote(chain_id, safe="")
    token = urllib.parse.quote(token_address, safe="")
    return f"/orders/v1/{chain}/{token}"


def parse_order_token(value: str) -> tuple[str, str]:
    if ":" in value:
        chain, token = value.split(":", 1)
    elif "/" in value:
        chain, token = value.split("/", 1)
    else:
        raise argparse.ArgumentTypeError("expected CHAIN:TOKEN or CHAIN/TOKEN")
    chain = chain.strip()
    token = token.strip()
    if not chain or not token:
        raise argparse.ArgumentTypeError("expected non-empty CHAIN and TOKEN")
    return chain, token


def load_json_or_jsonl(path: Path) -> Any:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line_no, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
        return rows
    return json.loads(text)


def source_payloads(payload: Any, default_source: str) -> list[tuple[str, str | None, Any]]:
    """Return (source_key, source_endpoint, payload) groups from common fixture shapes."""
    if isinstance(payload, dict):
        endpoint = payload.get("endpoint") or payload.get("source_endpoint")
        source = payload.get("source") or payload.get("source_key") or default_source
        if any(k in payload for k in ("payload", "data", "rows", "events")) and endpoint:
            nested = payload.get("payload", payload.get("data", payload.get("rows", payload.get("events"))))
            return [(str(source), str(endpoint), nested)]
        # Endpoint map fixture, e.g. {"/token-boosts/latest/v1": [...]}.
        endpoint_groups = []
        for key, value in payload.items():
            if isinstance(key, str) and (key.startswith("/") or key in OFFICIAL_DEX_ENDPOINTS or "dex_" in key):
                endpoint_groups.append((key, OFFICIAL_DEX_ENDPOINTS.get(key, key if key.startswith("/") else None), value))
        if endpoint_groups:
            return endpoint_groups
    return [(default_source, None, payload)]


def row_chain(row: dict[str, Any]) -> str | None:
    return row.get("chainId") or row.get("chain") or row.get("chain_id")


def row_token(row: dict[str, Any]) -> str | None:
    base_value = row.get("baseToken")
    base = base_value if isinstance(base_value, dict) else {}
    return row.get("tokenAddress") or row.get("token_address") or row.get("address") or base.get("address")


def row_pair(row: dict[str, Any]) -> str | None:
    return row.get("pairAddress") or row.get("pair_address")


def extract_rows(payload: Any, source_key: str = "fixture", source_endpoint: str | None = None) -> list[dict[str, Any]]:
    """Flatten DEXScreener list/dict response shapes into row dicts.

    For /orders/v1 responses, rows from `orders` and `boosts` are tagged with a
    private `_container` field for event-type inference. The tag is not included
    in the raw payload unless the fixture already provided it.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    is_orders = bool(source_endpoint and "/orders/v1/" in source_endpoint) or "orders" in source_key.lower()
    if is_orders:
        rows: list[dict[str, Any]] = []
        for row in payload.get("orders") or []:
            if isinstance(row, dict):
                rows.append({**row, "_container": row.get("_container") or "orders"})
        for row in payload.get("boosts") or []:
            if isinstance(row, dict):
                rows.append({**row, "_container": row.get("_container") or "boosts"})
        if rows:
            return rows

    for key in ("pairs", "tokens", "data", "items", "rows", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    if row_chain(payload) or row_token(payload):
        return [payload]
    return []


def infer_event_type(row: dict[str, Any], source_key: str = "", source_endpoint: str | None = None) -> str:
    explicit = row.get("event_type") or row.get("_event_type")
    if explicit in VALID_EVENT_TYPES:
        return str(explicit)

    haystack = " ".join(str(v).lower() for v in (source_key, source_endpoint or "", row.get("_container") or "", row.get("type") or ""))
    order_type = str(row.get("type") or "").lower()
    if "ad" in order_type or " tokenad" in haystack or "advert" in haystack:
        return "dex_ad"
    if row.get("_container") == "boosts" or "boost" in haystack or "amount" in row or "totalAmount" in row:
        return "dex_boost"
    if row.get("_container") == "orders" or "orders/v1" in haystack or "order" in haystack or row.get("paymentTimestamp"):
        return "dex_paid_order"
    return "dex_profile_update"


def public_summary(row: dict[str, Any], event_type: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in (
        "url",
        "description",
        "icon",
        "header",
        "openGraph",
        "links",
        "amount",
        "totalAmount",
        "id",
        "type",
        "status",
        "paymentTimestamp",
        "paymentAmount",
    ):
        if key in row and row.get(key) is not None:
            safe_value = safe_summary_value(key, row.get(key))
            if safe_value is not None:
                summary[key] = safe_value
    if event_type in {"dex_ad", "dex_paid_order"}:
        order_type = safe_text(row.get("type"), 80)
        status = safe_text(row.get("status"), 80)
        if order_type is not None:
            summary["order_type"] = order_type
        if status is not None:
            summary["status"] = status
    event_time = ts_to_iso(row.get("paymentTimestamp"))
    if event_time:
        summary["payment_time_utc"] = event_time
    return summary


def dedupe_material(event_type: str, row: dict[str, Any], source_endpoint: str | None, summary: dict[str, Any]) -> dict[str, Any]:
    """Stable identity material for cross-run de-duping.

    This intentionally excludes `observed_at` and excludes raw payload ordering.
    Profile events include profile metadata in the hash so later profile edits can
    become a new tape event; paid orders/boosts prefer payment/id fields.
    """
    material: dict[str, Any] = {
        "schema": SCHEMA,
        "event_type": event_type,
        "source": "dexscreener",
        "source_endpoint": source_endpoint,
        "chain_id": row_chain(row),
        "token_address": row_token(row),
        "pair_address": row_pair(row),
    }
    if event_type == "dex_profile_update":
        material["profile"] = {k: summary.get(k) for k in ("url", "description", "icon", "header", "links") if k in summary}
    elif event_type == "dex_boost":
        material["boost"] = {k: summary.get(k) for k in ("id", "amount", "totalAmount", "paymentTimestamp") if k in summary}
    else:
        material["order"] = {k: summary.get(k) for k in ("type", "status", "paymentTimestamp", "paymentAmount") if k in summary}
    return material


def normalize_event(
    row: dict[str, Any],
    source_key: str,
    observed_at: str,
    source_endpoint: str | None = None,
    include_raw: bool = False,
) -> dict[str, Any] | None:
    chain_id = row_chain(row)
    token_address = row_token(row)
    if not chain_id or not token_address:
        return None
    event_type = infer_event_type(row, source_key, source_endpoint)
    summary = public_summary(row, event_type)
    material = dedupe_material(event_type, row, source_endpoint, summary)
    event = {
        "schema": SCHEMA,
        "event_id": sha_id(material),
        "event_type": event_type,
        "observed_at": observed_at,
        "source": "dexscreener",
        "source_key": source_key,
        "source_endpoint": source_endpoint,
        "chain_id": chain_id,
        "token_address": token_address,
        "pair_address": row_pair(row),
        "url": safe_url(row.get("url")),
        "event_time": ts_to_iso(row.get("paymentTimestamp")),
        "summary": summary,
        "dedupe_material": material,
        "raw_untrusted": bool(include_raw),
        "boundary": BOUNDARY,
    }
    if include_raw:
        event["raw"] = {k: v for k, v in row.items() if not k.startswith("_")}
    return event


def events_from_payload(
    payload: Any,
    source_key: str,
    observed_at: str,
    source_endpoint: str | None = None,
    include_raw: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = extract_rows(payload, source_key, source_endpoint)
    if limit is not None:
        rows = rows[: max(0, limit)]
    events = []
    for row in rows:
        event = normalize_event(row, source_key, observed_at, source_endpoint, include_raw)
        if event is not None:
            events.append(event)
    return events


def dedupe_events(events: Iterable[dict[str, Any]], existing_ids: set[str] | None = None) -> list[dict[str, Any]]:
    seen = set(existing_ids or set())
    out = []
    for event in events:
        event_id = event.get("event_id")
        if not event_id or event_id in seen:
            continue
        seen.add(str(event_id))
        out.append(event)
    return out


def existing_event_ids(path: Path) -> set[str]:
    if not path.exists() or str(path) == "-":
        return set()
    ids = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = row.get("event_id")
            if event_id:
                ids.add(str(event_id))
    return ids


def write_jsonl(path: Path | None, events: Iterable[dict[str, Any]]) -> int:
    count = 0
    if path is None or str(path) == "-":
        for event in events:
            print(canonical_json(event))
            count += 1
        return count
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(canonical_json(event) + "\n")
            count += 1
    return count


def collect_from_json(paths: list[Path], observed_at: str, include_raw: bool, limit: int | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in paths:
        payload = load_json_or_jsonl(path)
        for source_key, source_endpoint, nested in source_payloads(payload, path.stem):
            events.extend(events_from_payload(nested, source_key, observed_at, source_endpoint, include_raw, limit))
    return events


def collect_from_dexscreener(
    observed_at: str,
    include_raw: bool,
    limit: int,
    order_tokens: list[tuple[str, str]],
    fetch_discovery: bool = True,
    discovered_order_limit: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    discovered: list[tuple[str, str]] = []
    if fetch_discovery:
        for source_key, endpoint in OFFICIAL_DEX_ENDPOINTS.items():
            try:
                payload = fetch_json(endpoint)
                endpoint_events = events_from_payload(payload, source_key, observed_at, endpoint, include_raw, limit)
                events.extend(endpoint_events)
                for event in endpoint_events:
                    discovered.append((str(event["chain_id"]), str(event["token_address"])))
            except Exception as exc:  # network/API errors should not corrupt local tape
                errors[source_key] = str(exc)

    wanted_orders = list(order_tokens)
    if discovered_order_limit > 0:
        for chain, token in discovered:
            pair = (chain, token)
            if pair not in wanted_orders:
                wanted_orders.append(pair)
            if len(wanted_orders) >= discovered_order_limit + len(order_tokens):
                break

    for chain, token in wanted_orders:
        endpoint = ordered_endpoint(chain, token)
        source_key = f"dex_orders:{chain}:{token}"
        try:
            payload = fetch_json(endpoint)
            events.extend(events_from_payload(payload, source_key, observed_at, endpoint, include_raw, limit))
            time.sleep(0.12)
        except Exception as exc:
            errors[source_key] = str(exc)
    return events, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chaos Event Tape v1: local read-only DEXScreener event JSONL")
    parser.add_argument("--from-json", action="append", type=Path, default=[], help="Ingest fixture JSON/JSONL (repeatable)")
    parser.add_argument("--fetch-dexscreener", action="store_true", help="Fetch official public DEXScreener profile/boost endpoints")
    parser.add_argument("--order-token", action="append", type=parse_order_token, default=[], metavar="CHAIN:TOKEN", help="Also fetch DEXScreener /orders/v1 for a token (repeatable)")
    parser.add_argument("--fetch-orders-for-discovered", type=int, default=0, help="Optionally fetch /orders/v1 for the first N discovered tokens (default: 0)")
    parser.add_argument("--limit-per-endpoint", type=int, default=100, help="Max rows/events per source payload (default: 100)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output JSONL path (default: {DEFAULT_OUT}) or '-' for stdout")
    parser.add_argument("--raw", action="store_true", help="Include raw public source row in each event")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.from_json and not args.fetch_dexscreener and not args.order_token:
        parser.error("provide --from-json, --fetch-dexscreener, or --order-token")

    observed_at = now_iso()
    limit = max(1, min(int(args.limit_per_endpoint), 500))
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    if args.from_json:
        events.extend(collect_from_json(args.from_json, observed_at, args.raw, limit))
    if args.fetch_dexscreener or args.order_token:
        live_events, live_errors = collect_from_dexscreener(
            observed_at,
            args.raw,
            limit,
            args.order_token,
            args.fetch_dexscreener,
            max(0, int(args.fetch_orders_for_discovered)),
        )
        events.extend(live_events)
        errors.update(live_errors)

    out_path: Path | None = None if str(args.out) == "-" else args.out
    existing = existing_event_ids(out_path) if out_path else set()
    unique = dedupe_events(events, existing)
    written = write_jsonl(out_path, unique)
    summary = {
        "ok": True,
        "mode": "chaos_event_tape_v1",
        "observed_at": observed_at,
        "source_event_count": len(events),
        "new_event_count": written,
        "skipped_duplicate_count": len(events) - written,
        "out": str(args.out),
        "errors": errors,
        "boundary": BOUNDARY,
    }
    print(json.dumps(summary, indent=2, sort_keys=True), file=sys.stderr if str(args.out) == "-" else sys.stdout)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
