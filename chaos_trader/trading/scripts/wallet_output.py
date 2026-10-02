#!/usr/bin/env python3
"""Copy-first wallet output adapter for Chaos Telegram UX.

Boundary: presentation/artifact layer only. No scoring, no wallet execution,
no signing, no swapping, no wallet-replication automation.
"""
from __future__ import annotations
import os

import argparse
import csv
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from tg_card_policy import opaque_callback_id, validate_button_policy, validate_button_rows

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
DEFAULT_WATCHLIST_DIR = PROFILE_HOME / "trading" / "watchlists"
DEFAULT_REPORT_DIR = PROFILE_HOME / "trading" / "reports"

QUALITY_LABELS = {
    "raw_holder",
    "early_buyer",
    "top_holder",
    "top_pnl_claimed_external",
    "pnl_verified_onchain",
    "cluster_verified",
    "copyable_candidate",
    "do_not_copy",
}

CSV_FIELDS = [
    "wallet",
    "name",
    "emoji",
    "tags",
    "quality_label",
    "confidence",
    "cluster_id",
    "source",
    "source_url",
    "realized_pnl_sol",
    "realized_pnl_usd",
    "transfer_adjusted",
    "win_rate",
    "trade_count",
    "dead_bag_rate",
    "avg_hold_time",
    "first_seen",
    "last_seen",
    "funding_source",
    "copyability_score",
    "liquidity_fit",
    "min_liquidity_observed",
    "max_position_size_sol",
    "risk_flags",
    "verdict",
    "reason",
]

COPYABILITY_BLOCKING_FLAGS = {
    "sniper",
    "custom_program",
    "manipulative",
    "unsupported_strategy",
    "dev_contamination",
    "transfer_only",
    "no_realized_exits",
    "insufficient_liquidity",
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        # Preserve simple comma-delimited fields without splitting URLs.
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _clean_slug(scan_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", scan_name.strip()).strip("._-")
    return slug[:96] or "wallet_scan"


def _short_wallet(wallet: str) -> str:
    if len(wallet) <= 14:
        return wallet
    return f"{wallet[:6]}…{wallet[-4:]}"


def _has_value(record: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "verified", "transfer_adjusted"}
    return bool(value)


def _pnl_verified_evidence(record: dict[str, Any]) -> bool:
    return (
        _has_value(record, "realized_pnl_sol", "realized_pnl_usd")
        and _truthy(record.get("transfer_adjusted"))
        and _has_value(record, "win_rate")
        and _has_value(record, "trade_count")
        and _has_value(record, "dead_bag_rate")
    )


def _cluster_verified_evidence(record: dict[str, Any]) -> bool:
    return _has_value(record, "cluster_id") and (
        _has_value(record, "cluster_evidence", "cluster_links")
        or str(record.get("confidence", "")).lower() in {"medium", "high"}
    )


def _copyable_evidence(record: dict[str, Any]) -> bool:
    return (
        _pnl_verified_evidence(record)
        and _has_value(record, "copyability_score")
        and _has_value(record, "liquidity_fit", "min_liquidity_observed")
        and not (set(_as_list(record.get("risk_flags"))) & COPYABILITY_BLOCKING_FLAGS)
    )


def validate_wallet_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize one wallet row without upgrading its intelligence claims.

    This function may only downgrade/mark unsafe labels. It must never promote a
    wallet into PnL-verified, cluster-verified, or copyable status.
    """
    out = deepcopy(record)
    wallet = out.get("wallet") or out.get("address") or out.get("trackedWalletAddress")
    if not wallet or not str(wallet).strip():
        raise ValueError("wallet record missing wallet/address")
    out["wallet"] = str(wallet).strip()
    out.setdefault("address", out["wallet"])

    out["tags"] = _as_list(out.get("tags"))
    out["risk_flags"] = _as_list(out.get("risk_flags"))
    out.setdefault("emoji", "☄️")
    out.setdefault("name", _short_wallet(out["wallet"]))
    out.setdefault("confidence", "low")
    out.setdefault("verdict", "investigate")

    original_label = str(out.get("quality_label") or out.get("label") or "raw_holder").strip()
    if original_label not in QUALITY_LABELS:
        out["original_quality_label"] = original_label
        out["quality_label"] = "raw_holder"
        out["verdict"] = "investigate"
        _append_reason(out, f"invalid quality label {original_label!r}; downgraded to raw_holder")
        return out

    out["quality_label"] = original_label

    blocking = set(out["risk_flags"]) & COPYABILITY_BLOCKING_FLAGS
    if original_label == "do_not_copy" or blocking:
        out["quality_label"] = "do_not_copy"
        out["verdict"] = "avoid"
        if blocking:
            _append_reason(out, "blocking risk flags: " + ",".join(sorted(blocking)))
        return out

    if original_label == "copyable_candidate" and not _copyable_evidence(out):
        out["original_quality_label"] = original_label
        out["quality_label"] = "do_not_copy"
        out["verdict"] = "avoid"
        out["risk_flags"] = sorted(set(out["risk_flags"]) | {"insufficient_copyability_evidence"})
        _append_reason(out, "copyable_candidate blocked: missing realized PnL, transfer-adjusted, failure-rate, or liquidity-fit evidence")
        return out

    if original_label == "pnl_verified_onchain" and not _pnl_verified_evidence(out):
        out["original_quality_label"] = original_label
        out["quality_label"] = "top_pnl_claimed_external" if out.get("source") else "raw_holder"
        out["verdict"] = "investigate"
        _append_reason(out, "pnl_verified_onchain downgraded: missing realized/transfer-adjusted/failure-rate evidence")
        return out

    if original_label == "cluster_verified" and not _cluster_verified_evidence(out):
        out["original_quality_label"] = original_label
        out["quality_label"] = "raw_holder"
        out["verdict"] = "investigate"
        _append_reason(out, "cluster_verified downgraded: missing cluster evidence")
        return out

    return out


def _append_reason(record: dict[str, Any], message: str) -> None:
    current = str(record.get("reason") or "").strip()
    record["reason"] = f"{current}; {message}" if current else message


def normalize_wallet_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate/downgrade records while preserving order and de-duping wallets."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in records:
        record = validate_wallet_record(dict(row))
        wallet = record["wallet"]
        if wallet in seen:
            continue
        seen.add(wallet)
        normalized.append(record)
    return normalized


def scan_quality(records: list[dict[str, Any]]) -> str:
    labels = {r.get("quality_label") for r in records}
    if not labels:
        return "discovery only"
    high_signal = {"pnl_verified_onchain", "cluster_verified", "copyable_candidate"}
    discovery = {"raw_holder", "early_buyer", "top_holder", "top_pnl_claimed_external", "do_not_copy"}
    if labels & high_signal and labels & discovery:
        if labels & {"cluster_verified", "copyable_candidate"}:
            return "mixed; cluster/copyability evidence present"
        return "mixed; PnL-verified evidence present"
    if labels <= {"cluster_verified", "copyable_candidate"}:
        return "cluster-verified"
    if labels <= {"pnl_verified_onchain"}:
        return "PnL-verified"
    return "discovery only"


def raw_wallet_block(records: list[dict[str, Any]]) -> str:
    return "\n".join(r["wallet"] for r in records)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return "" if value is None else value


def wallet_to_tracker_import(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "trackedWalletAddress": record["wallet"],
        "name": record.get("name") or _short_wallet(record["wallet"]),
        "emoji": record.get("emoji") or "☄️",
        "alertsOn": bool(record.get("alertsOn", False)),
    }


@dataclass(frozen=True)
class WalletArtifacts:
    txt: Path
    csv: Path
    json: Path
    import_json: Path
    report: Path

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items()}


def write_wallet_artifacts(
    scan_name: str,
    records: Iterable[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    *,
    watchlist_dir: Path = DEFAULT_WATCHLIST_DIR,
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> WalletArtifacts:
    """Write copy-first TXT/CSV/JSON artifacts and a Markdown report."""
    summary = dict(summary or {})
    normalized = normalize_wallet_records(records)
    slug = _clean_slug(scan_name)
    watchlist_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    txt_path = watchlist_dir / f"{slug}.txt"
    csv_path = watchlist_dir / f"{slug}.csv"
    json_path = watchlist_dir / f"{slug}.json"
    import_path = watchlist_dir / f"{slug}.import.json"
    report_path = report_dir / f"{slug}.md"

    txt_path.write_text(raw_wallet_block(normalized) + ("\n" if normalized else ""), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in normalized:
            writer.writerow({field: _csv_value(record.get(field)) for field in CSV_FIELDS})

    json_path.write_text(json.dumps(normalized, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    import_path.write_text(json.dumps([wallet_to_tracker_import(r) for r in normalized], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    artifacts = WalletArtifacts(txt=txt_path, csv=csv_path, json=json_path, import_json=import_path, report=report_path)
    report_text = render_wallet_scan_telegram(summary, normalized, artifacts.as_dict(), include_media=False)
    report_path.write_text(report_text + "\n", encoding="utf-8")
    return artifacts


def render_wallet_scan_telegram(
    summary: dict[str, Any] | None,
    records: Iterable[dict[str, Any]],
    artifacts: dict[str, str] | None = None,
    *,
    include_media: bool = True,
    max_table_rows: int = 15,
) -> str:
    """Render a dense Telegram-bot-style token/wallet card.

    This is a presentation adapter only. It formats supplied token/wallet fields
    in the style of common Solana TG bots; it does not score, infer PnL, infer
    clusters, upgrade labels, trade, or create execution affordances.
    """
    summary = dict(summary or {})
    normalized = normalize_wallet_records(records)
    verdict = summary.get("verdict") or ("investigate" if normalized else "avoid")
    quality = summary.get("quality") or scan_quality(normalized)

    def pick(*keys: str, default: Any = "—") -> Any:
        for key in keys:
            value = summary.get(key)
            if value not in (None, "", [], {}):
                return value
        return default

    def fmt(value: Any, default: str = "—") -> str:
        if value in (None, "", [], {}):
            return default
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def compact_list(value: Any, sep: str = "•") -> str:
        values = _as_list(value)
        return sep.join(values) if values else "—"

    def link_text(value: Any) -> str:
        if value in (None, "", [], {}):
            return "—"
        if isinstance(value, dict):
            label = str(value.get("label") or value.get("name") or value.get("text") or "LINK")
            url = value.get("url") or value.get("href")
            return f"[{label}]({url})" if url else label
        if isinstance(value, (list, tuple)):
            rendered = [link_text(v) for v in value if v not in (None, "", [], {})]
            return "•".join(rendered) if rendered else "—"
        return str(value)

    def metric_line(emoji: str, label: str, value: Any) -> str:
        return f"{emoji} {label}: {fmt(value)}"

    title = pick("title", "alert_title", "name", "scan_name", default="wallet scan")
    symbol = pick("symbol", "token_symbol", default=None)
    ticker = str(symbol) if symbol else str(pick("token", default=""))
    if ticker and not ticker.startswith("$"):
        ticker = "$" + ticker

    fdv = fmt(pick("fdv", "market_cap", "mcap", default="—"))
    fdv_to = pick("fdv_to", "market_cap_to", default=None)
    fdv_window = pick("fdv_window", "window", default=None)
    fdv_value = fdv
    if fdv_to not in (None, "", [], {}):
        fdv_value += f" ⇨ {fmt(fdv_to)}"
    if fdv_window not in (None, "", [], {}):
        fdv_value += f" [{fmt(fdv_window)}]"

    liq = fmt(pick("liquidity", "liq", default="—"))
    liq_mult = pick("liquidity_mult", "liq_mult", default=None)
    liq_value = liq + (f" [{fmt(liq_mult)}]" if liq_mult not in (None, "", [], {}) else "")

    one_h = fmt(pick("price_change_1h", "change_1h", "one_hour_change", default="—"))
    buys = fmt(pick("buys", "buy_count", default="—"))
    sells = fmt(pick("sells", "sell_count", default="—"))

    top_holders = compact_list(pick("top_holders", "top_holder_percents", "th", default=[]))
    top_holder_total = pick("top_holder_total", "top_holder_pct", default=None)
    th_value = top_holders + (f" [{fmt(top_holder_total)}]" if top_holder_total not in (None, "", [], {}) else "")

    total_holders = fmt(pick("total_holders", "holders", default="—"))
    avg_holder_age = pick("avg_holder_age", "average_holder_age", default=None)
    total_value = total_holders + (f" • avg {fmt(avg_holder_age)} old" if avg_holder_age not in (None, "", [], {}) else "")

    fresh_1d = fmt(pick("fresh_1d", "fresh_1d_pct", default="—"))
    fresh_7d = fmt(pick("fresh_7d", "fresh_7d_pct", default="—"))

    token_address = pick("token_address", "mint", "address", default=None)
    code_links = pick("code_links", "quick_links", "links", default=[])
    chart_links = pick("chart_links", "charts", default=[])
    more_links = pick("more_links", "more", default=[])

    headline_metric = pick("headline_metric", default=None)
    headline_change = pick("headline_change", default=None)
    headline_suffix = ""
    if headline_metric not in (None, "", [], {}) or headline_change not in (None, "", [], {}):
        headline_suffix = f" [{fmt(headline_metric, '')}/{fmt(headline_change, '')}]"

    lines = [f"💊 {fmt(title)}{headline_suffix}"]
    if ticker:
        lines.append(ticker)
    lines.extend([
        f"🟪 {fmt(pick('chain', default='Solana'))} @ {fmt(pick('venue', 'platform', default='Pump'))}",
        metric_line("💰", "USD", pick("usd_price", "price_usd", "price", default="—")),
        metric_line("💎", "FDV", fdv_value),
        metric_line("💦", "Liq", liq_value),
        f"📊 Vol: {fmt(pick('volume', 'vol', default='—'))} • Age: {fmt(pick('age', default='—'))}",
        f"💩 1H: {one_h} Ⓑ {buys} Ⓢ {sells}",
        "",
        metric_line("👥", "TH", th_value),
        metric_line("🤝", "Total", total_value),
        f"🌱 Fresh 1D: {fresh_1d} • 7D: {fresh_7d}",
        f"💹 Chart: {link_text(chart_links)}",
        f"🧰 More: {link_text(more_links)}",
    ])

    if token_address not in (None, "", [], {}):
        lines.extend(["", str(token_address)])
    if code_links not in (None, "", [], {}):
        lines.append(link_text(code_links))

    lines.extend(["", f"🎯 Wallets: {len(normalized)} • {quality} • {verdict}"])
    if normalized:
        for record in normalized[:max_table_rows]:
            score = record.get("copyability_score")
            score_part = f" • {score}" if score not in (None, "", [], {}) else ""
            lines.append(f"{record.get('emoji', '☄️')} {_short_wallet(record['wallet'])} • {record.get('quality_label')} • {record.get('confidence', 'low')} • {record.get('verdict', 'investigate')}{score_part}")
        if len(normalized) > max_table_rows:
            lines.append(f"… {len(normalized) - max_table_rows} more in files")
        lines.extend(["", "```text", raw_wallet_block(normalized), "```"])
    else:
        lines.append("No wallets qualified. No wallet quality inferred.")

    winner = pick("winner", "caller", default=None)
    call_stat = pick("call_stat", "entry", default=None)
    mult = pick("multiplier", default=None)
    watchers = pick("watchers", "views", default=None)
    if winner not in (None, "", [], {}) or call_stat not in (None, "", [], {}):
        tail = f"🏆 {fmt(winner, '[anon]')}"
        if call_stat not in (None, "", [], {}):
            tail += f" @ {fmt(call_stat)}"
        if mult not in (None, "", [], {}):
            tail += f"•{fmt(mult)}"
        if watchers not in (None, "", [], {}):
            tail += f" 👀 {fmt(watchers)}"
        lines.extend(["", tail])

    if artifacts:
        file_bits: list[str] = []
        for key, label in (("txt", "TXT"), ("csv", "CSV"), ("json", "JSON"), ("import_json", "IMPORT"), ("report", "REPORT")):
            path = artifacts.get(key)
            if path:
                file_bits.append(label if include_media else f"{label}:{path}")
        if file_bits:
            lines.append("📎 Files: " + " • ".join(file_bits))
        if include_media:
            for key in ("csv", "json", "import_json"):
                path = artifacts.get(key)
                if path:
                    lines.append(f"MEDIA:{path}")

    caveat = summary.get("caveat") or "Top holder / early buyer ≠ good wallet until realized exits + transfer-adjusted PnL are known."
    lines.extend(["", f"⚠️ {caveat}"])
    return "\n".join(lines).rstrip()


def build_wallet_scan_render_descriptor(
    summary: dict[str, Any] | None,
    records: Iterable[dict[str, Any]],
    artifacts: dict[str, str] | None = None,
    *,
    max_table_rows: int = 8,
) -> dict[str, Any]:
    """Build a Telegram-only render descriptor for a read-only Chaos card.

    The returned shape is data-only and policy-validated. External URLs are
    restricted to transparent read-only chart/explorer hosts; callbacks use
    opaque ids and carry no raw action authority.
    """
    summary = dict(summary or {})
    normalized = normalize_wallet_records(records)
    text = render_wallet_scan_telegram(
        summary,
        normalized,
        artifacts,
        include_media=False,
        max_table_rows=max_table_rows,
    )
    token_address = summary.get("token_address") or summary.get("mint") or summary.get("address")
    rows: list[list[dict[str, Any]]] = []
    first_row: list[dict[str, Any]] = []
    if token_address:
        mint = str(token_address).strip()
        first_row.append({"text": "Copy CA", "copy_text": mint})
        first_row.append({
            "text": "Open DEX: dexscreener.com",
            "url": f"https://dexscreener.com/solana/{mint}",
        })
        rows.append(first_row)
        rows.append([{
            "text": "Open SOL: solscan.io",
            "url": f"https://solscan.io/token/{mint}",
        }])

    wallets = raw_wallet_block(normalized)
    if wallets and len(wallets) <= 256:
        rows.append([{"text": "Copy wallets", "copy_text": wallets}])

    risk_text = str(summary.get("risk") or summary.get("risk_flags") or summary.get("caveat") or "Risk notes unavailable.")
    cb_id = opaque_callback_id(token_address, risk_text, len(normalized), "show_risk")
    rows.append([{
        "text": "Show risk notes",
        "callback": {"action": "show_risk", "id": cb_id, "text": risk_text[:900]},
    }])

    allowed_rows, decisions = validate_button_rows(rows)
    descriptor_artifacts: list[dict[str, str]] = []
    for key, value in (artifacts or {}).items():
        if not value:
            continue
        suffix = Path(value).suffix.lower().lstrip(".")
        if suffix not in {"csv", "json", "txt"}:
            continue
        artifact_decision = validate_button_policy({"text": f"Files: {suffix.upper()}", "artifact_path": str(value)})
        if artifact_decision.decision == "allow" and artifact_decision.button:
            descriptor_artifacts.append({"type": suffix, "path": artifact_decision.button["artifact_path"], "label": key})

    return {
        "text": text,
        "format": "plain",
        "link_preview": {"disabled": True},
        "buttons": allowed_rows,
        "artifacts": descriptor_artifacts,
        "policy_decisions": [decision.__dict__ for decision in decisions],
    }


def _load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"]
    if isinstance(data, list):
        return data
    raise SystemExit(f"Expected list or {{records: [...]}} in {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render/write copy-first Chaos wallet output artifacts.")
    parser.add_argument("records_json", type=Path, help="JSON list of wallet records")
    parser.add_argument("--scan-name", default="wallet_scan")
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--telegram", action="store_true", help="Print Telegram message")
    parser.add_argument("--descriptor", action="store_true", help="Print Telegram render descriptor JSON")
    args = parser.parse_args()

    records = _load_records(args.records_json)
    summary = json.loads(args.summary_json.read_text(encoding="utf-8")) if args.summary_json else {"scan_name": args.scan_name}
    artifacts = write_wallet_artifacts(args.scan_name, records, summary)
    if args.descriptor:
        print(json.dumps(build_wallet_scan_render_descriptor(summary, records, artifacts.as_dict()), indent=2, ensure_ascii=False))
    elif args.telegram:
        print(render_wallet_scan_telegram(summary, records, artifacts.as_dict()))
    else:
        print(json.dumps({"ok": True, "artifacts": artifacts.as_dict()}, indent=2))


if __name__ == "__main__":
    main()
