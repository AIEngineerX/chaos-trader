#!/usr/bin/env python3
"""Simple Chaos command router with plain-text cards.

This is the thin UX layer: one short command in chat or CLI,
one compact answer back. It calls the deeper read-only scripts and hides MD/JSON
artifact digging unless the output needs a saved evidence handle.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tg_card_policy import opaque_callback_id, validate_button_rows
from alpha_tape import render_sweep as render_alpha_sweep
from alpha_tape import render_token as render_alpha_token
from alpha_tape import sweep_payload as alpha_sweep_payload
from alpha_tape import token_payload as alpha_token_payload
from strategy_paper_engine import decide as decide_strategy_paper
from strategy_paper_engine import render as render_strategy_paper
from json_contract import print_envelope
from json_contract import status as json_status
import x_provider

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import REFILL_PAPER_BOOK, REFILL_WALLETS, chaos_home, db_failure, stop_if_corrupt, unreadable_db  # noqa: E402
PROFILE_HOME = chaos_home()
PY = os.environ.get("CHAOS_PYTHON", sys.executable)
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,88}$")
BOUNDARY = "Advisory + paper only. No wallet, signing, routing, or live execution."
NOTHING_YET_SWEEP_FAST = "The roster tape is empty until the ingest job has run. Run the ingest job, or use chaos sweep without --fast for the trending sweep."
PAPER_RETIRED = "chaos paper was retired; use chaos paper-report for the paper book and chaos strategy-paper <mint> for one mint."
NO_INGEST_REVIEW = "No ingest yet. Run chaos run chaos_alpha_elite_ingest first."
JSON_HELP = "Print one JSON envelope: schema_version, command, generated_at, data"
JSON_EXCLUSIVE = "--json cannot be combined with --raw or --render-json; pick one."
NOTHING_YET_WALLETS = 'Wallet discovery needs transfer edges from the Helius wallet API. With a Helius RPC and HELIUS_API_KEY set, run: chaos run smart_wallet_tracker <wallet address> — then try again.'


def env() -> dict[str, str]:
    e = dict(os.environ)
    e.setdefault("CHAOS_HOME", str(PROFILE_HOME))
    e.setdefault("HERMES_HOME", str(PROFILE_HOME))
    e.setdefault("PYTHONPATH", os.pathsep.join(p for p in [str(SCRIPT_DIR), os.environ.get("HERMES_AGENT_SRC"), e.get("PYTHONPATH", "")] if p))
    e["PYTHONIOENCODING"] = "utf-8"  # children write utf-8; run_raw decodes utf-8
    return e


def run_raw(args: list[str], timeout: int = 360) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [PY, *args, "--raw"],
            cwd=str(SCRIPT_DIR),
            env=env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"☄️ Chaos command timed out after {timeout}s. Raise --timeout, or check the RPC.")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "unknown error").strip()
        raise SystemExit(f"☄️ Chaos command failed\n{msg[:1200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"☄️ Chaos command returned non-JSON\n{exc}\n{proc.stdout[:1000]}")


def money(v: Any) -> str:
    try:
        f = float(v)
    except Exception:
        return "?"
    if f >= 1_000_000:
        return f"${f/1_000_000:.2f}M"
    if f >= 1_000:
        return f"${f/1_000:.1f}K"
    return f"${f:.0f}"


def num(v: Any) -> str:
    try:
        f = float(v)
    except Exception:
        return "?"
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    return f"{f:g}"


def short_ca(mint: str) -> str:
    return f"{mint[:6]}…{mint[-4:]}"


def verdict_mark(verdict: Any) -> str:
    text = str(verdict or "").lower()
    if text in {"watch", "study", "manual-review", "hold-core", "manage"}:
        return "🟡"
    if text in {"ignore", "avoid", "avoid-entry"}:
        return "🔴"
    if "exit" in text or "caution" in text or "trim" in text:
        return "🟠"
    return "⚪️"


def code_block(value: str, lang: str = "text") -> str:
    return f"```{lang}\n{value.strip()}\n```"


def solscan_token_url(mint: str) -> str:
    return f"https://solscan.io/token/{mint}"


def dexscreener_token_url(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"


def market_open_row(mint: str, market_url: str | None = None) -> str:
    # Plain Markdown/text rows are durable artifacts; do not trust vendor URLs.
    # Rich buttons still pass through tg_card_policy validation separately.
    dex = dexscreener_token_url(mint)
    return f"🧭 Open: [DEX]({dex}) · [SOL]({solscan_token_url(mint)})"


def _descriptor_text(markdown_text: str) -> str:
    """Convert the compact Markdown card into plain text for render descriptors.

    A chat adapter may reject hidden Markdown/HTML links at the descriptor
    boundary, so buttons carry navigation while the message remains plain and
    visibly copyable.
    """
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", markdown_text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text


def _open_button_rows(mint: str, market_url: str | None = None, *, copy_label: str = "Copy CA") -> list[list[dict[str, Any]]]:
    rows = [[
        {"text": copy_label, "copy_text": mint},
        {"text": "Open DEX: dexscreener.com", "url": market_url or dexscreener_token_url(mint)},
    ], [
        {"text": "Open SOL: solscan.io", "url": solscan_token_url(mint)},
    ]]
    allowed, _decisions = validate_button_rows(rows)
    return allowed


def _risk_callback_row(payload: dict[str, Any], *, mode: str) -> list[dict[str, Any]]:
    market = payload.get("market") or {}
    cls = payload.get("classification") or {}
    flow = cls.get("flow") or {}
    risk_parts = []
    for key in ("risk_flags", "reasons"):
        risk_parts.extend(str(x) for x in (cls.get(key) or []) if str(x).strip())
    risk_parts.extend(str(x) for x in (flow.get("flags") or []) if str(x).strip())
    why = cls.get("why_not_watch") or cls.get("why_watch")
    if why:
        risk_parts.append(str(why))
    text = "; ".join(risk_parts[:8]) or "No explicit risk notes returned."
    cb_id = opaque_callback_id(payload.get("mint"), market.get("symbol"), mode, text)
    allowed, _decisions = validate_button_rows([[{
        "text": "Show risk notes",
        "callback": {"action": "show_risk", "id": cb_id, "text": text[:900]},
    }]])
    return allowed[0] if allowed else []


def token_render_descriptor(payload: dict[str, Any], *, include_artifact: bool = False, mode_label: str = "TOKEN READ") -> dict[str, Any]:
    mint = str(payload.get("mint") or "").strip()
    market = payload.get("market") or {}
    rows = _open_button_rows(mint, market.get("url")) if mint else []
    risk_row = _risk_callback_row(payload, mode=mode_label)
    if risk_row:
        rows.append(risk_row)
    return {
        "text": _descriptor_text(compact_token(payload, include_artifact=include_artifact, mode_label=mode_label)),
        "format": "plain",
        "link_preview": {"disabled": True},
        "buttons": rows,
        "artifacts": [],
    }


def sweep_render_descriptor(payload: dict[str, Any], *, include_artifact: bool = False) -> dict[str, Any]:
    text = compact_sweep(payload, include_artifact=include_artifact)
    ranked = payload.get("ranked_candidates") or []
    reads = payload.get("deep_reads") or []
    mints: list[str] = []
    markets: dict[str, str] = {}
    for item in reads:
        mint = str(item.get("mint") or "").strip()
        if mint:
            mints.append(mint)
            market_url = ((item.get("market") or {}).get("url"))
            if market_url:
                markets[mint] = str(market_url)
    for item in ranked:
        mint = str(item.get("mint") or "").strip()
        if mint and mint not in mints:
            mints.append(mint)

    rows: list[list[dict[str, Any]]] = []
    if mints:
        copy_payload = "\n".join(mints[:5])
        if len(copy_payload) <= 256:
            allowed, _decisions = validate_button_rows([[{"text": "Copy CA list", "copy_text": copy_payload}]])
            rows.extend(allowed)
        rows.extend(_open_button_rows(mints[0], markets.get(mints[0])))
    descriptor_text = text
    if mints:
        if descriptor_text.rstrip().endswith(BOUNDARY):
            descriptor_text = descriptor_text.rstrip()[: -len(BOUNDARY)].rstrip()
        descriptor_text = f"{descriptor_text}\n\nButtons target top CA.\n{BOUNDARY}"
    return {
        "text": _descriptor_text(descriptor_text),
        "format": "plain",
        "link_preview": {"disabled": True},
        "buttons": rows,
        "artifacts": [],
    }


def cite_count(x: Any) -> int:
    if not isinstance(x, dict):
        return 0
    return len(x.get("citations") or []) + len(x.get("inline_citations") or [])


def compact_token(payload: dict[str, Any], *, include_artifact: bool = False, mode_label: str = "TOKEN READ") -> str:
    market = payload.get("market") or {}
    cls = payload.get("classification") or {}
    mode_ctx = payload.get("mode_context") or {}
    gate = payload.get("gate") or {}
    entry_gate = payload.get("entry_gate") or {}
    position = payload.get("position_context") or {}
    wallet = payload.get("wallet_timing") or {}
    owner = payload.get("owner_exposure") or {}
    catalyst = payload.get("social_catalyst") or {}
    flow_conv = payload.get("flow_conversion") or {}
    delta = payload.get("delta") or {}
    flow = cls.get("flow") or {}
    mint = payload.get("mint") or ""
    sym = market.get("symbol") or "UNKNOWN"
    entry_label = entry_gate.get("action") or gate.get("gate") or cls.get("verdict") or "study"
    position_action = position.get("position_action") or "no-position"
    header_label = "OWNER POSITION READ" if position.get("owner_exposed") and mode_label == "TOKEN READ" else mode_label
    mode = mode_ctx.get("mode") or "unknown"
    venue = mode_ctx.get("venue") or market.get("dex_id") or "unknown"
    ratio = flow_conv.get("volume_liquidity_ratio") if flow_conv.get("volume_liquidity_ratio") is not None else flow.get("volume_liquidity_ratio")
    catalyst_label = catalyst.get("catalyst_type") or "none"
    catalyst_sub = catalyst.get("catalyst_subtype")
    catalyst_text = f"{catalyst_label} / {catalyst_sub}" if catalyst_sub else catalyst_label
    flow_text = f"fake-flow {flow_conv.get('fake_flow_severity', 'unknown')} · conversion {flow_conv.get('attention_conversion', 'unknown')}"
    if flow_conv.get("conversion_status"):
        flow_text = f"{flow_conv.get('conversion_status')} · {flow_text}"
    owner_value = money(position.get("position_value_usd")) if position.get("position_value_usd") is not None else "?"
    owner_pct = position.get("position_pct_liquidity")
    owner_pct_text = f"{float(owner_pct):.2f}% liq" if owner_pct is not None else "? liq"
    # The line prints "timing <label>", and the unresolved label already starts with "timing ".
    timing_label = str(wallet.get("timing_label") or "unresolved").removeprefix("timing ")
    q_hits = wallet.get("quality_wallet_hit_count", wallet.get("watch_wallet_hit_count", 0))
    owner_hits = position.get("owner_wallet_hit_count", owner.get("owner_wallet_hit_count", 0))
    owner_count = position.get("owner_wallet_count", owner.get("owner_wallet_count", 0))
    why_items = [str(x) for x in (position.get("position_why") or []) if str(x).strip()] if position.get("owner_exposed") else []
    why_items.extend(str(x) for x in (gate.get("why") or []) if str(x).strip())
    risk_items = [str(x) for x in (position.get("position_risk") or []) if str(x).strip()] if position.get("owner_exposed") else []
    risk_items.extend(str(x) for x in (gate.get("risk") or []) if str(x).strip())
    if not why_items:
        why_items = [cls.get("why_not_watch") or cls.get("why_watch") or "no positive gate evidence"]
    if not risk_items:
        risk_items = [str(x) for x in (cls.get("risk_flags") or [])[:2] if str(x).strip()] or ["exit path unresolved"]

    lines = [
        f"☄️ {verdict_mark(entry_label)} {header_label}",
        f"${sym} · `{short_ca(mint)}`",
        f"MODE: **{mode}**",
        f"ENTRY GATE: **{entry_label}**",
        f"POSITION: **{position_action}**",
        f"CATALYST: {catalyst_text}",
        f"FLOW: {flow_text}",
        f"MC {money(market.get('market_cap'))} · Liq {money(market.get('liquidity_usd'))} · V/L {ratio if ratio is not None else '?'}x",
        f"WALLETS: Q {q_hits} · timing {timing_label} · owner {owner_hits}/{owner_count}",
    ]
    if payload.get("holder_data"):
        lines.append(f"HOLDERS: {payload['holder_data']}")
    elif payload.get("token_scan_error") or payload.get("pumpfun_error"):
        lines.append("CHAIN: unavailable (on-chain reads failed; see the saved JSON)")
    if position.get("owner_exposed"):
        lines.append(f"OWNER: {owner_hits}/{owner_count} · {owner_value} · {owner_pct_text}")
    if delta.get("useful"):
        change_bits = []
        if delta.get("catalyst_changed"):
            change_bits.append(f"catalyst {delta.get('catalyst_delta')}")
        if delta.get("mc_delta_pct") is not None:
            change_bits.append(f"MC {float(delta.get('mc_delta_pct')):+.1f}%")
        if delta.get("position_action_changed"):
            change_bits.append(f"position {delta.get('previous_position_action')} → {delta.get('current_position_action')}")
        if change_bits:
            lines.append("CHANGE: " + "; ".join(change_bits[:3]))
    lines.append("WHY:")
    lines.extend(f"- {item}" for item in why_items[:3])
    lines.append("RISK:")
    lines.extend(f"- {item}" for item in risk_items[:3])
    if position.get("owner_exposed"):
        lines.extend(["PRIVATE POSITION NOTE:", position_action])
    lines.extend([
        "\n📋 Copy CA",
        code_block(mint),
        market_open_row(mint, market.get("url")),
        "⚡ Next",
        code_block(gate.get("next") or f"analyze token {mint}"),
    ])
    if include_artifact and payload.get("markdown_path"):
        lines.append(f"Files: `{payload['markdown_path']}`")
    lines.append(BOUNDARY)
    return "\n".join(lines)


def compact_sweep(payload: dict[str, Any], *, include_artifact: bool = False) -> str:
    ranked = payload.get("ranked_candidates") or []
    reads = payload.get("deep_reads") or []
    filtered = payload.get("filtered_deep_reads") or []
    sweep_mode = payload.get("sweep_mode") or "alpha"
    summary = f"Mode {sweep_mode} · Scanned {payload.get('candidate_count')} · ranked {len(ranked)} · deep {len(reads)} · filtered {len(filtered)} · X {'on' if payload.get('x_enabled') else 'off'}"
    lines = ["☄️ TREND SWEEP", summary]
    copy_mints: list[str] = []
    next_commands: list[str] = []
    if reads:
        for i, r in enumerate(reads[:5], 1):
            mint = r.get("mint") or ""
            if mint:
                copy_mints.append(mint)
                next_commands.append(f"analyze token {mint}")
            if r.get("error"):
                lines.extend(["", f"## Candidate {i}", f"`{short_ca(mint)}` — error: {str(r['error'])[:120]}"])
                continue
            market = r.get("market") or {}
            cls = r.get("classification") or {}
            wallet = r.get("wallet_timing") or {}
            flow = cls.get("flow") or {}
            x_risk = cls.get("x_risk") or {}
            verdict = cls.get("verdict") or "study"
            phase = cls.get("attention_phase") or "unknown"
            why = cls.get("why_not_watch") or cls.get("why_watch") or "No gate note returned."
            symbol = market.get("symbol") or "UNKNOWN"
            score = cls.get("score")
            watch_hits = wallet.get("watch_wallet_hit_count", 0)
            x_hits = r.get("x_citation_count", 0)
            ratio = flow.get("volume_liquidity_ratio")
            ratio_text = f"{ratio}x" if ratio is not None else "?"
            avg = flow.get("avg_tx_usd")
            try:
                avg_text = f"${float(avg):.2f}"
            except Exception:
                avg_text = "?"
            mode_ctx = r.get("mode_context") or {}
            gate = r.get("gate") or {}
            entry_gate = r.get("entry_gate") or {}
            position = r.get("position_context") or {}
            catalyst = r.get("social_catalyst") or {}
            flow_conv = r.get("flow_conversion") or {}
            gate_label = entry_gate.get("action") or gate.get("gate") or verdict
            why = (gate.get("why") or [why])
            risk = gate.get("risk") or []
            verdict_lc = str(gate_label).lower()
            if any(term in verdict_lc for term in ("avoid", "ignore", "exit", "caution")):
                verdict_note = "not clean / avoid"
            elif "micro" in verdict_lc:
                verdict_note = "low-cap trench only"
            else:
                verdict_note = "manual review required"

            lines.extend([
                "",
                f"## Top Candidate" if i == 1 else f"## Candidate {i}",
                f"{verdict_mark(gate_label)} **${symbol}** — `{gate_label}`",
                f"MODE: **{mode_ctx.get('mode') or phase}** · ENTRY **{gate_label}** · POSITION **{position.get('position_action') or 'no-position'}**",
                f"CATALYST: **{catalyst.get('catalyst_type') or 'none'}** · FLOW: **{flow_conv.get('conversion_status') or 'unknown'}** · Fact **{r.get('fact_grade') or '?'}**",
                "",
                "## Why",
                str(why[0])[:180] if why else "No gate note returned.",
                "",
                "## Risk",
                f"- MC: **{money(market.get('market_cap'))}** · Liq: **{money(market.get('liquidity_usd'))}**",
                f"- V/L: **{ratio_text}** · Avg trade: **{avg_text}**",
                f"- X mentions: **{x_hits}** · Quality wallets: **{wallet.get('quality_wallet_hit_count', watch_hits)}** · Scout wallets: **{wallet.get('scout_wallet_hit_count', 0)}** · Secondary scouts: **{((gate.get('counts') or {}).get('early_hidden', 0) + (gate.get('counts') or {}).get('early_scout', 0))}**",
                *[f"- {str(x)[:120]}" for x in risk[:2]],
                "",
                "## Verdict",
                f"**{gate_label}** — {verdict_note}",
                "",
                market_open_row(mint, market.get("url")),
            ])
    elif ranked and not (sweep_mode == "alpha" and filtered and ((payload.get("caps") or {}).get("deep") or 0) > 0):
        lines.append("\n## Top Candidates")
        for i, c in enumerate(ranked[:5], 1):
            mint = c.get("mint") or ""
            if mint:
                copy_mints.append(mint)
                next_commands.append(f"analyze token {mint}")
            s = c.get("summary") or {}
            lines.append(
                f"{i}. `{short_ca(mint)}` — score **{num(c.get('candidate_score'))}** · Liquidity **{money(s.get('liquidity_usd'))}** · MC **{money(s.get('marketCap'))}**"
            )
            if mint:
                lines.append(f"   {market_open_row(mint)}")
    else:
        if sweep_mode == "alpha" and filtered:
            lines.extend([
                "\nNo alpha candidates passed the organic-flow gate.",
                f"Filtered {len(filtered)} trap/risk read(s) from the opportunity card.",
                "Run `sweep --mode trap` to inspect trap-radar output.",
            ])
            for item in filtered[:3]:
                mint = item.get("mint") or ""
                label = item.get("label") or item.get("verdict") or "filtered"
                why = item.get("why_not_watch") or "risk gate failed"
                lines.append(f"- `{short_ca(mint)}` — {label}: {str(why)[:120]}")
        else:
            lines.append("\nNo candidates surfaced.")

    if copy_mints:
        # Keep this no-bullets/no-prose block for long-press copy/import in a chat.
        lines.extend(["\n📋 Copy CA list", code_block("\n".join(copy_mints))])
    if next_commands:
        lines.extend(["⚡ Next", code_block("\n".join(next_commands[:5]))])
    if include_artifact and payload.get("markdown_path"):
        lines.append(f"Files: `{payload['markdown_path']}`")
    lines.append(BOUNDARY)
    return "\n".join(lines)


def x_requested(args: argparse.Namespace) -> bool:
    """Return whether X/social context should be included for this command.

    Chat-facing sweep/token reads default to X enabled per the Chaos command
    contract, but tests/older callers without ``default_x`` remain opt-in.
    ``--no-x`` is always the hard veto. X is on only when an X search provider is
    configured (x_provider.provider_name() is not "none"); ``--with-x`` forces it on then.
    ``--with-x`` with no provider is exactly X off, plus one notice line on stderr.
    """
    if bool(getattr(args, "no_x", False)):
        return False
    with_x = bool(getattr(args, "with_x", False))
    if x_provider.provider_name() == "none":
        if with_x:
            print(x_provider.no_provider_notice(), file=sys.stderr)
        return False
    return with_x or bool(getattr(args, "default_x", False))


def cmd_token(args: argparse.Namespace) -> None:
    mint = args.mint.strip()
    if not MINT_RE.match(mint):
        raise SystemExit("Invalid Solana mint/CA shape.")
    if getattr(args, "fast", False):
        payload = alpha_token_payload(mint, dex=getattr(args, "with_dex", False), dex_ttl=getattr(args, "dex_ttl", 60))
        if getattr(args, "json", False):
            print_envelope("token", payload)
            return
        if args.render_json:
            text = render_alpha_token(payload)
            print(json.dumps({
                "text": _descriptor_text(text),
                "format": "plain",
                "link_preview": {"disabled": True},
                "buttons": _open_button_rows(mint),
                "artifacts": [],
            }, ensure_ascii=False))
        else:
            print(render_alpha_token(payload))
        return
    script_args = ["token_event_analyzer.py", mint, "--tx-limit", str(args.tx_limit), "--x-days", str(args.x_days), "--source-command", "token"]
    if x_requested(args):
        script_args.append("--x")
    if getattr(args, "gmgn", False):
        script_args.append("--gmgn")
    payload = run_raw(script_args, timeout=args.timeout)
    if getattr(args, "json", False):
        print_envelope("token", payload)
    elif args.render_json:
        print(json.dumps(token_render_descriptor(payload, include_artifact=args.artifact, mode_label="TOKEN READ"), ensure_ascii=False))
    else:
        print(compact_token(payload, include_artifact=args.artifact, mode_label="TOKEN READ"))


def cmd_analyze(args: argparse.Namespace) -> None:
    mint = args.mint.strip()
    if not MINT_RE.match(mint):
        raise SystemExit("Invalid Solana mint/CA shape.")
    script_args = ["token_event_analyzer.py", mint, "--tx-limit", str(args.tx_limit), "--x-days", str(args.x_days), "--source-command", "analyze_token"]
    if x_requested(args):
        script_args.append("--x")
    if getattr(args, "gmgn", False):
        script_args.append("--gmgn")
    payload = run_raw(script_args, timeout=args.timeout)
    if getattr(args, "json", False):
        print_envelope("analyze", payload)
    elif args.render_json:
        print(json.dumps(token_render_descriptor(payload, include_artifact=True, mode_label="DEEP TOKEN ANALYSIS"), ensure_ascii=False))
    else:
        print(compact_token(payload, include_artifact=True, mode_label="DEEP TOKEN ANALYSIS"))


def cmd_sweep(args: argparse.Namespace) -> None:
    if getattr(args, "fast", False):
        payload = alpha_sweep_payload(limit=args.limit)
        if not payload.get("ok", True) and all(str(e).startswith(("missing table", "smart_wallets.sqlite missing")) for e in payload.get("errors") or []):
            if getattr(args, "json", False):
                print_envelope("sweep", json_status("no-ingest", NOTHING_YET_SWEEP_FAST))
                return
            print(NOTHING_YET_SWEEP_FAST)
            return
        if getattr(args, "json", False):
            print_envelope("sweep", payload)
            return
        if getattr(args, "raw", False):
            print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
            return
        if args.render_json:
            text = render_alpha_sweep(payload)
            candidates = payload.get("candidates") or []
            top_mint = (candidates[0] or {}).get("mint") if candidates else ""
            rows = _open_button_rows(top_mint) if top_mint else []
            print(json.dumps({
                "text": _descriptor_text(text),
                "format": "plain",
                "link_preview": {"disabled": True},
                "buttons": rows,
                "artifacts": [],
            }, ensure_ascii=False))
        else:
            print(render_alpha_sweep(payload))
        return
    script_args = [
        "trending_token_sweep.py",
        "--limit", str(args.limit),
        "--deep", str(args.deep),
        "--tx-limit", str(args.tx_limit),
        "--x-days", str(args.x_days),
        "--mode", args.mode,
    ]
    if x_requested(args):
        script_args.append("--x")
    payload = run_raw(script_args, timeout=args.timeout)
    if getattr(args, "json", False):
        print_envelope("sweep", payload)
    elif args.render_json:
        print(json.dumps(sweep_render_descriptor(payload, include_artifact=args.artifact), ensure_ascii=False))
    else:
        print(compact_sweep(payload, include_artifact=args.artifact))


def cmd_strategy_paper(args: argparse.Namespace) -> None:
    mint = args.mint.strip()
    if not MINT_RE.match(mint):
        raise SystemExit("Invalid Solana mint/CA shape.")
    script_args = ["token_event_analyzer.py", mint, "--tx-limit", str(args.tx_limit), "--x-days", str(args.x_days), "--source-command", "strategy_paper"]
    if x_requested(args):
        script_args.append("--x")
    payload = run_raw(script_args, timeout=args.timeout)
    decision = decide_strategy_paper(
        payload,
        base_risk_usd=args.base_risk_usd,
        max_notional_usd=args.max_notional_usd,
        liquidity_bps=args.liquidity_bps,
    )
    if getattr(args, "json", False):
        print_envelope("strategy-paper", decision)
    elif args.render_json:
        text = render_strategy_paper(decision)
        print(json.dumps({
            "text": _descriptor_text(text),
            "format": "plain",
            "link_preview": {"disabled": True},
            "buttons": _open_button_rows(mint),
            "artifacts": [],
        }, ensure_ascii=False))
    elif args.raw:
        print(json.dumps(decision, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    else:
        print(render_strategy_paper(decision))


def parse_loose(argv: list[str]) -> list[str]:
    """Allow chat-style forms: `sweep`, `trend`, `analyze token <mint>`."""
    if not argv:
        return ["help"]
    first = argv[0].lower()
    if first in {"trend", "trends", "trending", "sweep", "scan-trends", "scan_trends"}:
        return ["sweep", *argv[1:]]
    if first == "scan" and len(argv) > 1 and argv[1].lower() in {"trend", "trends", "trending"}:
        return ["sweep", *argv[2:]]
    if first == "paper" and len(argv) > 1 and argv[1].lower() in {"token", "ca", "mint"}:
        return ["strategy-paper", *argv[2:]]
    if first in {"strategy-paper", "strategy_paper", "paper-token", "paper_token"}:
        return ["strategy-paper", *argv[1:]]
    if first == "paper" and len(argv) > 1 and argv[1].lower() in {"report", "learning"}:
        return ["paper-report", *argv[2:]]
    if first in {"paper-report", "paper_report", "learning-report", "learning_report"}:
        return ["paper-report", *argv[1:]]
    if first == "paper":
        print(PAPER_RETIRED, file=sys.stderr)
        raise SystemExit(2)
    if first in {"wallets", "smart-wallets", "smart_wallets", "wallet-discovery", "discover-wallets"}:
        return ["wallets", *argv[1:]]
    if first in {"analyze", "analyse"}:
        rest = argv[1:]
        if rest and rest[0].lower() in {"token", "mint", "ca"}:
            rest = rest[1:]
        return ["analyze", *rest]
    if first in {"token", "ca", "mint"}:
        rest = argv[1:]
        if rest and rest[0].lower() in {"token", "mint", "ca"}:
            rest = rest[1:]
        return ["token", *rest]
    return argv


def cmd_smart_signals(args: argparse.Namespace) -> None:
    from smart_money_signal_client import fetch_live_signals, fetch_wallets, render_md
    if getattr(args, "wallets", False):
        payload = fetch_wallets(tier=getattr(args, "tier", None))
    else:
        payload = fetch_live_signals(limit=args.limit)
    if getattr(args, "json", False):
        print_envelope("smart-signals", payload)
    elif getattr(args, "raw", False):
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    elif args.render_json:
        print(json.dumps({
            "text": _descriptor_text(render_md(payload)),
            "format": "plain",
            "link_preview": {"disabled": True},
            "buttons": [],
            "artifacts": [],
        }, ensure_ascii=False))
    else:
        print(render_md(payload))


def cmd_wallets(args: argparse.Namespace) -> None:
    if getattr(args, "add", None) or getattr(args, "remove", None):
        cmd_wallets_edit(args)
        return
    if getattr(args, "review", False):
        cmd_wallets_review(args)
        return
    from smart_wallet_promoter import DEFAULT_DB as SMART_DB
    from smart_wallet_promoter import run as promoter_run
    db = Path(args.db).expanduser() if getattr(args, "db", None) else SMART_DB
    enriched: list[dict[str, Any]] = []
    if args.discover and not db.exists():
        if getattr(args, "json", False):
            print_envelope("wallets", json_status("no-wallet-db", NOTHING_YET_WALLETS))
            return
        print(NOTHING_YET_WALLETS)
        return
    try:
        if args.discover:
            from smart_wallet_tracker import discovered_wallets, enrich_wallet, ensure_db
            with closing(sqlite3.connect(db)) as con:
                con.execute("PRAGMA foreign_keys=ON")
                ensure_db(con)
                targets = discovered_wallets(con, max(1, min(args.discover, 10)))
                for w in targets:
                    try:
                        enriched.append(enrich_wallet(con, w, 50, 1, True))
                    except Exception as exc:
                        # Discard the failed wallet's pending deletes; without this the
                        # next successful wallet's commit() would seal them.
                        try:
                            con.rollback()
                        except sqlite3.Error:
                            pass
                        enriched.append({"wallet": w, "ok": False, "error": str(exc)[:300]})
        payload = promoter_run(db, args.limit, bool(args.discover))
    except sqlite3.DatabaseError as exc:
        raise SystemExit(unreadable_db(db, exc))
    payload["enriched_now"] = enriched
    enrich_failures = sum(1 for e in enriched if "error" in e)
    payload["ok"] = enrich_failures == 0
    if getattr(args, "json", False):
        print_envelope("wallets", payload)
        if enrich_failures:
            raise SystemExit(1)
        return
    if getattr(args, "raw", False):
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        if enrich_failures:
            raise SystemExit(1)
        return
    summary = payload.get("summary") or {}
    lines = [
        "☄️ SMART WALLET DISCOVERY",
        f"Scored universe: {payload.get('count')} · candidates {summary.get('smart_wallet_candidates')} · sensors {summary.get('cluster_sensors')} · study {summary.get('study')}",
        f"Discovery queue: {summary.get('discovery_queue')} unscored edge-wallets",
    ]
    if enriched:
        ok = [e for e in enriched if e.get("wallet") and "error" not in e]
        failed = [e for e in enriched if "error" in e]
        lines.append(f"Enriched now: {len(ok)} ok · {len(failed)} failed")
        for e in ok[:5]:
            lines.append(f"- {e['wallet'][:6]}…{e['wallet'][-4:]} score {e.get('score')} · {e.get('copyability')} · pnl {e.get('sample_realized_pnl_sol')} SOL")
        for e in failed[:3]:
            lines.append(f"- {e['wallet'][:6]}…{e['wallet'][-4:]} failed: {e.get('error')}")
    for r in (payload.get("rows") or [])[:5]:
        lines.append(f"- {r['verdict']} {r['score']:.0f} `{r['wallet'][:6]}…{r['wallet'][-4:]}` cluster {r.get('max_shared_mints')} pnl {r.get('sample_realized_pnl_sol')}")
    if args.discover and not enriched and not payload.get("discovery_queue"):
        lines.append(NOTHING_YET_WALLETS)
    lines.append(BOUNDARY)
    text = "\n".join(lines)
    if args.render_json:
        print(json.dumps({"text": _descriptor_text(text), "format": "plain", "link_preview": {"disabled": True}, "buttons": [], "artifacts": []}, ensure_ascii=False))
    else:
        print(text)
    if enrich_failures:
        # Card is printed either way; the exit code keeps cron/health truthful.
        raise SystemExit(1)


def cmd_wallets_edit(args: argparse.Namespace) -> None:
    """`chaos wallets --add/--remove`: edit the home roster file in place. Never reads the chain or the database."""
    from elite_wallet_pipeline import _valid_address
    path = PROFILE_HOME / "trading" / "config" / "roster.json"
    if not path.is_file():
        raise SystemExit(f"No roster at {path}. Run chaos onboard first.")
    roster = json.loads(path.read_text(encoding="utf-8"))
    wallets = roster["wallets"]
    present = [str(w.get("address") or "").strip() for w in wallets]
    if args.add:
        address = args.add.strip()
        if not _valid_address(address):
            raise SystemExit(f"{address} is not a Solana address (base58, 32 to 44 characters).")
        if address in present:
            raise SystemExit(f"{address} is already in the roster.")
        wallets.append({"address": address, "tier": args.tier})
        message = f"added {address} as tier {args.tier}; run chaos run chaos_alpha_elite_ingest to read it"
    else:
        address = args.remove.strip()
        if address not in present:
            raise SystemExit(f"{address} is not in the roster.")
        if len(wallets) == 1:
            raise SystemExit(f"{address} is the last wallet in the roster; the ingest needs at least one. Add another first.")
        roster["wallets"] = [w for w, a in zip(wallets, present) if a != address]
        message = f"removed {address} from the roster"
    roster["source"] = "user-edited"
    path.write_text(json.dumps(roster, indent=2) + "\n", encoding="utf-8")
    if getattr(args, "json", False):
        print_envelope("wallets --add" if args.add else "wallets --remove", json_status("added" if args.add else "removed", message))
        return
    print(message)


def has_completed_ingest(db: Path) -> bool:
    """True once a run has completed. `chaos wallets` creates the schema with no runs, so the file alone proves nothing."""
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
        return con.execute("SELECT 1 FROM ingestion_runs WHERE status='completed' LIMIT 1").fetchone() is not None


def roster_review(db: Path, records: list[dict[str, Any]], days: int, now: datetime) -> list[dict[str, Any]]:
    """One row per roster wallet from the local smart-wallet database: newest on-chain event, clean closed
    positions from the ledger the ingest rebuilds (the tracker's definition), and the newest on-chain
    tracker score. Reads the database only, never the chain."""
    from smart_wallet_tracker import ONCHAIN_SOURCE_SQL
    cutoff = now - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
        for record in records:
            wallet = record["address"]
            last = con.execute(
                f"SELECT MAX(block_time_utc) FROM wallet_token_events WHERE wallet=? AND source_id IN ({ONCHAIN_SOURCE_SQL})",
                (wallet,),
            ).fetchone()[0]
            clean_closed = con.execute(
                "SELECT COUNT(*) FROM positions WHERE wallet=? AND status='closed' AND transfer_contaminated=0 AND realized_pnl_sol IS NOT NULL",
                (wallet,),
            ).fetchone()[0]
            scored = con.execute(
                f"SELECT score,copyability FROM wallet_scores WHERE wallet=? AND source_id IN ({ONCHAIN_SOURCE_SQL}) ORDER BY scored_at DESC, id DESC LIMIT 1",
                (wallet,),
            ).fetchone()
            if last is None or datetime.fromisoformat(last.replace("Z", "+00:00")) < cutoff:
                label = "dormant"
            elif clean_closed < 3:
                label = "no track record yet"
            else:
                label = "scoring"
            rows.append({
                "tier": record["tier"],
                "address": wallet,
                "last_event_utc": last,
                "clean_closed": clean_closed,
                "score": None if scored is None or scored[0] is None else float(scored[0]),
                "copyability": scored[1] if scored else None,
                "label": label,
            })
    return rows


def cmd_wallets_review(args: argparse.Namespace) -> None:
    from elite_wallet_pipeline import load_roster
    from smart_wallet_promoter import DEFAULT_DB as SMART_DB
    if args.days < 1:
        raise SystemExit("--days must be 1 or more.")
    db = Path(args.db).expanduser() if getattr(args, "db", None) else SMART_DB
    try:
        if not db.exists() or not has_completed_ingest(db):
            if getattr(args, "json", False):
                print_envelope("wallets --review", json_status("no-ingest", NO_INGEST_REVIEW))
                return
            print(NO_INGEST_REVIEW)
            return
        roster = load_roster(PROFILE_HOME / "trading" / "config" / "roster.json", lenient_tiers=True)
        rows = roster_review(db, roster["records"], args.days, datetime.now(timezone.utc))
    except sqlite3.DatabaseError as exc:
        raise SystemExit(unreadable_db(db, exc))
    if getattr(args, "json", False):
        print_envelope("wallets --review", rows)
        return
    if getattr(args, "raw", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    lines = ["☄️ ROSTER REVIEW"]
    for r in rows:
        last = "none" if r["last_event_utc"] is None else datetime.fromisoformat(r["last_event_utc"].replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        score = "-" if r["score"] is None else f"{r['score']:.0f}"
        label = r["label"]
        if label == "scoring" and r["score"] is None:
            label = f"{label} (not scored yet: run chaos run smart_wallet_tracker {r['address']})"
        elif label == "scoring" and r["copyability"]:
            score = f"{score} {r['copyability']}"
        lines.append(f"{r['tier']} {r['address']} last {last} clean-closed {r['clean_closed']} score {score}  {label}")
    active = sum(1 for r in rows if r["label"] != "dormant")
    lines.append(f"{active} of {len(rows)} roster wallets active in the last {args.days} days")
    lines.append(BOUNDARY)
    text = "\n".join(lines)
    if args.render_json:
        print(json.dumps({"text": _descriptor_text(text), "format": "plain", "link_preview": {"disabled": True}, "buttons": [], "artifacts": []}, ensure_ascii=False))
    else:
        print(text)


def cmd_paper_report(args: argparse.Namespace) -> None:
    from paper_learning_report import PAPER_DB
    stop_if_corrupt(PAPER_DB, REFILL_PAPER_BOOK)  # one line here, not the report's failure block
    proc = subprocess.run([PY, str(SCRIPT_DIR / "paper_learning_report.py"), "--limit", str(args.limit)], env=env(), text=True, encoding="utf-8", capture_output=True, timeout=args.timeout)
    if proc.returncode != 0:
        print(f"☄️ PAPER LEARNING REPORT failed\n{(proc.stderr or proc.stdout).strip()[:800]}")
        raise SystemExit(1)
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        print(proc.stdout.strip()[:2000])
        return
    if getattr(args, "json", False):
        print_envelope("paper-report", payload)
        return
    if getattr(args, "raw", False):
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str))
        return
    if payload.get("note"):
        print(payload["note"])
        return
    pos = payload.get("positions") or {}
    lines = [
        "☄️ PAPER LEARNING REPORT",
        f"Positions: open {pos.get('open')} · closed {pos.get('closed')} · realized R {pos.get('realized_r')}",
        f"Fills: {payload.get('fills')} · events: {sum((payload.get('event_counts') or {}).values())}",
    ]
    for blocker, n in (payload.get("top_blockers") or [])[:5]:
        lines.append(f"- blocker ×{n}: {blocker}")
    for rec in (payload.get("recommendations") or [])[:3]:
        lines.append(f"- next: {rec}")
    lines.append(f"Report: {payload.get('report')}")
    lines.append(BOUNDARY)
    text = "\n".join(lines)
    if args.render_json:
        print(json.dumps({"text": _descriptor_text(text), "format": "plain", "link_preview": {"disabled": True}, "buttons": [], "artifacts": []}, ensure_ascii=False))
    else:
        print(text)


def main() -> None:
    argv = parse_loose(sys.argv[1:])
    p = argparse.ArgumentParser(description="Chaos simple command router (chat or CLI, plain-text cards)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sweep", help="Run live read-only trending token sweep")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--deep", type=int, default=1, help="Deep token reads to run; any Solana RPC works, and a Helius RPC adds the mint history")
    sp.add_argument("--fast", action="store_true", help="Use local alpha-tape cache only; no live Helius/deep reads")
    sp.add_argument("--slow", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--tx-limit", type=int, default=12)
    sp.add_argument("--x-days", type=int, default=2)
    sp.add_argument("--mode", choices=("alpha", "trap", "all"), default="alpha", help="alpha hides avoid/exit traps; trap shows trap-radar reads")
    sp.add_argument("--traps", dest="mode", action="store_const", const="trap", help="Shortcut for --mode trap")
    sp.add_argument("--default-x", action="store_true", default=True, help=argparse.SUPPRESS)
    sp.add_argument("--with-x", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--no-x", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--artifact", action="store_true", help="Include saved artifact path")
    sp.add_argument("--raw", action="store_true", help="Emit raw JSON payload")
    sp.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    sp.add_argument("--json", action="store_true", help=JSON_HELP)
    sp.add_argument("--timeout", type=int, default=520)
    sp.set_defaults(func=cmd_sweep)

    tp = sub.add_parser("token", help="Live read-only token event read")
    tp.add_argument("mint")
    tp.add_argument("--tx-limit", type=int, default=20)
    tp.add_argument("--x-days", type=int, default=2)
    tp.add_argument("--default-x", action="store_true", default=True, help=argparse.SUPPRESS)
    tp.add_argument("--with-x", action="store_true", help=argparse.SUPPRESS)
    tp.add_argument("--no-x", action="store_true", help=argparse.SUPPRESS)
    tp.add_argument("--gmgn", action="store_true", help="Attach secondary read-only GMGN evidence on slow token reads")
    tp.add_argument("--with-dex", action="store_true", help="Opt in to fast-lane Dex TTL snapshot")
    tp.add_argument("--dex-ttl", type=int, default=60, help="Dex cache freshness seconds for fast lane")
    tp.add_argument("--fast", action="store_true", help="Use local alpha-tape cache only; no live Helius/deep read")
    tp.add_argument("--slow", action="store_true", help=argparse.SUPPRESS)
    tp.add_argument("--artifact", action="store_true", help="Include saved artifact path")
    tp.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    tp.add_argument("--json", action="store_true", help=JSON_HELP)
    tp.add_argument("--timeout", type=int, default=480)
    tp.set_defaults(func=cmd_token)

    ap = sub.add_parser("analyze", help="Deeper one-token read: larger samples, longer X window, artifact path shown")
    ap.add_argument("mint")
    ap.add_argument("--tx-limit", type=int, default=60)
    ap.add_argument("--x-days", type=int, default=3)
    ap.add_argument("--default-x", action="store_true", default=True, help=argparse.SUPPRESS)
    ap.add_argument("--with-x", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-x", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--gmgn", action="store_true", help="Attach secondary read-only GMGN evidence after primary classification")
    ap.add_argument("--artifact", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    ap.add_argument("--json", action="store_true", help=JSON_HELP)
    ap.add_argument("--timeout", type=int, default=620)
    ap.set_defaults(func=cmd_analyze)

    stp = sub.add_parser("strategy-paper", help="Run one strategy-faithful paper decision from live analyze-token + X")
    stp.add_argument("mint")
    stp.add_argument("--tx-limit", type=int, default=40)
    stp.add_argument("--x-days", type=int, default=2)
    stp.add_argument("--default-x", action="store_true", default=True, help=argparse.SUPPRESS)
    stp.add_argument("--with-x", action="store_true", help=argparse.SUPPRESS)
    stp.add_argument("--no-x", action="store_true", help=argparse.SUPPRESS)
    stp.add_argument("--base-risk-usd", type=float, default=100.0)
    stp.add_argument("--max-notional-usd", type=float, default=250.0)
    stp.add_argument("--liquidity-bps", type=float, default=50.0)
    stp.add_argument("--raw", action="store_true")
    stp.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    stp.add_argument("--json", action="store_true", help=JSON_HELP)
    stp.add_argument("--timeout", type=int, default=620)
    stp.set_defaults(func=cmd_strategy_paper)

    ssp = sub.add_parser("smart-signals", help="Read smart-money cluster evidence from the external signal API")
    ssp.add_argument("--limit", type=int, default=20)
    ssp.add_argument("--wallets", action="store_true", help="Show the tracked wallet universe instead of signals")
    ssp.add_argument("--tier", choices=("A", "B", "C"), default=None, help="Filter the wallet universe by tier")
    ssp.add_argument("--raw", action="store_true", help="Emit raw JSON payload")
    ssp.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    ssp.add_argument("--json", action="store_true", help=JSON_HELP)
    ssp.set_defaults(func=cmd_smart_signals)

    prp = sub.add_parser("paper-report", help="Paper learning report: outcomes, blockers, rule recommendations")
    prp.add_argument("--limit", type=int, default=250)
    prp.add_argument("--raw", action="store_true", help="Emit raw JSON payload")
    prp.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    prp.add_argument("--json", action="store_true", help=JSON_HELP)
    prp.add_argument("--timeout", type=int, default=120)
    prp.set_defaults(func=cmd_paper_report)

    wp = sub.add_parser("wallets", help="Smart-wallet discovery status; --discover N enriches never-scored edge-wallets; --review checks the roster; --add/--remove edit it")
    wmode = wp.add_mutually_exclusive_group()
    wmode.add_argument("--discover", type=int, default=0, help="Enrich up to N never-scored wallets found via funding/transfer edges (Helius reads)")
    wmode.add_argument("--review", action="store_true", help="Each roster wallet's last event and track record from the local database")
    wmode.add_argument("--add", metavar="ADDRESS", default=None, help="Add a wallet to the home roster; needs --tier")
    wmode.add_argument("--remove", metavar="ADDRESS", default=None, help="Drop a wallet from the home roster")
    wp.add_argument("--tier", choices=("A", "B", "C"), default=None, help="With --add: the wallet's tier")
    wp.add_argument("--days", type=int, default=14, help="With --review: a wallet with no event in this many days is dormant")
    wp.add_argument("--limit", type=int, default=20)
    wp.add_argument("--db", default=None, help=argparse.SUPPRESS)
    wp.add_argument("--raw", action="store_true", help="Emit raw JSON payload")
    wp.add_argument("--render-json", action="store_true", help="Emit render descriptor JSON")
    wp.add_argument("--json", action="store_true", help=JSON_HELP)
    wp.set_defaults(func=cmd_wallets)

    hp = sub.add_parser("help", help="Show simple commands")
    hp.set_defaults(func=lambda _args: print("""☄️ Chaos simple commands

Commands, run as `chaos <command>`:
- onboard                       (create the home: .env, seed roster, paper config, data folders)
- update                        (refresh the paper defaults file; code updates come from pip)
- sweep                         (live trend sweep: limit 5, deep 1, X only with an X provider; `trend` is an alias)
- token <mint>                  (live token read, X only with an X provider)
- analyze token <mint>          (deeper token analysis + artifact path)
- strategy-paper <mint>         (strategy-faithful simulated decision: analyze-token + X; also `paper token <mint>`)
- paper-report                  (paper learning report: outcomes, blockers, rule recommendations)
- smart-signals                 (smart-money cluster evidence; --wallets lists the tracked wallets)
- wallets                       (smart-wallet discovery status + queue)
- wallets --discover 5          (enrich up to 5 never-scored edge-wallets, then re-rank)
- wallets --review              (each roster wallet's last activity and track record; --days 14 sets the dormant window)
- wallets --add <address> --tier A|B|C  (add a wallet to the home roster; no chain call)
- wallets --remove <address>    (drop a wallet from the home roster)
- skills list | install --for claude|codex|hermes|all  (shipped agent skills; `chaos skills install --dry-run` writes nothing)
- run <script> [args]           (run a pipeline script, job, or skill helper by name; `chaos run` alone lists them)
- --version                     (print the package version)
- help                          (this list)

Optional CLI flags:
- sweep --limit 5 --deep 1
- sweep --mode trap              (trap-radar: inspect avoid/exit-liquidity reads)
- sweep --fast                   (local alpha tape only; can be stale)
- token <mint> --fast --with-dex
- token <mint> --no-x
- X research is on when an X provider is configured (XAI_API_KEY or HERMES_AGENT_SRC; X_SEARCH_PROVIDER picks one); --no-x turns it off
- analyze token <mint> --gmgn    (secondary GMGN evidence; primary Helius/Dex gates unchanged)

Output is a compact card: stats, copy blocks, Open DEX/SOL links, and next-command blocks. Artifacts are saved silently unless --artifact/slow analyze is used.
sweep, token, analyze, strategy-paper, paper-report, smart-signals, and wallets take --json, which prints one envelope (schema_version, command, generated_at, data) in place of the card; --raw keeps the older unwrapped payload.
Advisory + paper only. No wallet, signing, routing, or live execution.""".strip()))

    args = p.parse_args(argv)
    if args.cmd == "wallets" and bool(args.add) != bool(args.tier):
        wp.error("--add needs --tier A, B, or C" if args.add else "--tier goes with --add")
    if getattr(args, "json", False) and (getattr(args, "raw", False) or getattr(args, "render_json", False)):
        print(JSON_EXCLUSIVE, file=sys.stderr)
        raise SystemExit(2)
    try:
        args.func(args)
    except sqlite3.DatabaseError as exc:  # wherever this platform's SQLite first notices the damage
        from paper_learning_report import PAPER_DB
        from smart_wallet_promoter import DEFAULT_DB as SMART_DB
        smart_db = Path(args.db).expanduser() if getattr(args, "db", None) else SMART_DB
        raise SystemExit(db_failure(exc, [(smart_db, REFILL_WALLETS), (PAPER_DB, REFILL_PAPER_BOOK)]))


if __name__ == "__main__":
    main()
