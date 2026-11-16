#!/usr/bin/env python3
"""Validate Chaos paper autopilot config.

Read-only validation only. Does not start loops, create cron jobs, connect wallets,
or perform network/API calls.
"""
from __future__ import annotations
import os

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
CONFIG_PATH = PROFILE_HOME / "trading" / "config" / "paper_autopilot.yaml"

REQUIRED_TRUE = [
    ("boundary", "no_execution"),
    ("boundary", "no_wallet"),
    ("boundary", "no_signing"),
    ("boundary", "no_order_routing"),
    ("boundary", "no_swap_links"),
    ("boundary", "no_webhooks"),
    ("boundary", "no_live_alerts"),
]
ALLOWED_PAPER_OUTPUTS = {
    "paper_enter",
    "paper_wait",
    "paper_avoid",
    "paper_trim",
    "paper_exit",
    "paper_expire",
}
FORBIDDEN_LIVE_CAPABILITIES = {"swap", "snipe", "execute", "route", "sign", "submit"}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if yaml is None:
        raise SystemExit("PyYAML unavailable; cannot parse paper_autopilot.yaml")
    return yaml.safe_load(path.read_text())


def validate(config: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(config, dict):
        errors.append("config must be a mapping")
        config = {}
    else:
        config = dict(config)  # never repair the caller's config in place
    sections = ("policy", "boundary", "paths", "loop", "budgets", "pre_filter",
                "deep_analyze", "market_discovery", "x_research", "paper_size",
                "entry", "exit", "paper_fee_model", "risk", "llm", "logging")
    for section in sections:
        if section in config and not isinstance(config[section], dict):
            errors.append(section + " must be a mapping, not " + type(config[section]).__name__)
            config[section] = {}
    for section, key in (("loop", "position_refresh_sec"), ("exit", "exit_on_gate_flip"), ("exit", "exit_on_flow_conversion")):
        if key in config.get(section, {}):
            errors.append(section + "." + key + " is unsupported; remove this setting (it never controlled runtime behavior)")

    def numeric(section: str, key: str, *, low: float, high: float = math.inf, integer: bool = False, strict_low: bool = False, strict_high: bool = False) -> None:
        values = config.get(section, {})
        if key not in values:
            return  # supported runtime defaults remain usable
        value = values[key]
        valid = type(value) is int if integer else type(value) in (int, float)
        if valid:
            try:
                valid = math.isfinite(value) and (value > low if strict_low else value >= low) and (value < high if strict_high else value <= high)
            except (OverflowError, TypeError):
                valid = False
        if not valid:
            kind = "integer" if integer else "number"
            errors.append(f"{section}.{key} must be a finite {kind} in {'(' if strict_low else '['}{low}, {high}{')' if strict_high else ']'}")

    for key in ("max_candidates", "max_open_positions", "max_new_entries_per_hour", "max_new_entries_per_day", "max_same_source_positions", "max_same_narrative_positions"):
        numeric("budgets", key, low=0, integer=True)
    for section in ("deep_analyze", "x_research"):
        for key in ("max_per_day", "max_per_hour"):
            numeric(section, key, low=0, integer=True)
    numeric("loop", "discovery_interval_sec", low=0, integer=True, strict_low=True)
    for key in ("stop_pct_conviction", "stop_pct_low_cap", "trailing_stop_after_tp1_pct", "liquidity_break_pct"):
        numeric("exit", key, low=-100, high=0, strict_high=True)
    for key in ("tp1_trim_pct", "tp2_trim_pct"):
        numeric("exit", key, low=0, high=100, strict_low=True)
    for key in ("tp1_pct", "tp2_pct", "low_cap_time_stop_min", "conviction_time_stop_min"):
        numeric("exit", key, low=0, strict_low=True)
    if config.get("mode") != "paper_only":
        errors.append("mode must be paper_only")
    if (config.get("loop") or {}).get("enabled") is not False:
        warnings.append("loop.enabled is not false; ensure explicit operator approval before running")
    for path in REQUIRED_TRUE:
        cur: Any = config
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if cur is not True:
            errors.append(".".join(path) + " must be true")
    boundary = config.get("boundary") or {}
    outputs = boundary.get("allowed_outputs") or []
    forbidden = boundary.get("forbidden_words") or []
    if not isinstance(outputs, list) or not all(isinstance(value, str) and value.strip() for value in outputs):
        errors.append("boundary.allowed_outputs must be a list of non-empty strings")
        outputs = []
    if not isinstance(forbidden, list) or not all(isinstance(value, str) and value.strip() for value in forbidden):
        errors.append("boundary.forbidden_words must be a list of non-empty strings")
        forbidden = []
    bad_outputs = sorted(set(outputs) - ALLOWED_PAPER_OUTPUTS)
    forbidden_terms = FORBIDDEN_LIVE_CAPABILITIES | {
        "_".join(re.findall(r"[a-z0-9]+", value.lower())) for value in forbidden
    }
    for output in outputs:
        normalized = "_".join(re.findall(r"[a-z0-9]+", output.lower()))
        tokens = set(normalized.split("_"))
        if normalized in forbidden_terms or tokens & forbidden_terms:
            bad_outputs.append(output)
    bad_outputs = sorted(set(bad_outputs))
    if bad_outputs:
        errors.append("forbidden live-execution capabilities in allowed_outputs: " + ", ".join(bad_outputs))
    llm = config.get("llm") or {}
    if llm.get("fast_loop") is not False:
        errors.append("llm.fast_loop must be false")
    entry = config.get("entry")
    if entry is not None and not isinstance(entry, dict):
        errors.append("entry must be a mapping, not " + type(entry).__name__)
        entry = {}
    entry = entry or {}
    # entry.wallet_signal_lane_enabled must be present and a real boolean. A missing key
    # would rely on the code default, and a quoted "false" or a typo'd variant would read
    # as a choice that was never actually made.
    if "wallet_signal_lane_enabled" not in entry:
        errors.append("entry.wallet_signal_lane_enabled must be set explicitly (true/false)")
    elif not isinstance(entry["wallet_signal_lane_enabled"], bool):
        errors.append("entry.wallet_signal_lane_enabled must be a boolean, not " + type(entry["wallet_signal_lane_enabled"]).__name__)
    elif entry["wallet_signal_lane_enabled"]:
        warnings.append("entry.wallet_signal_lane_enabled is true; paper entries may cite wallet evidence as their reason")
    x = config.get("x_research") or {}
    if type(x.get("max_per_day")) in (int, float) and x["max_per_day"] > 100:
        warnings.append("x_research.max_per_day > 100; likely too expensive/noisy")
    deep = config.get("deep_analyze") or {}
    if type(deep.get("max_per_day")) in (int, float) and deep["max_per_day"] > 250:
        warnings.append("deep_analyze.max_per_day > 250; likely too much API pressure")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "mode": config.get("mode"),
        "loop_enabled": (config.get("loop") or {}).get("enabled"),
        "deep_analyze_max_per_day": deep.get("max_per_day"),
        "x_max_per_day": x.get("max_per_day"),
        "max_open_positions": (config.get("budgets") or {}).get("max_open_positions"),
        "boundary": "paper-only config validation; no wallet, signing, routing, or live execution",
    }


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else CONFIG_PATH
    try:
        payload = validate(load_config(path))
    except (OSError, ValueError) as exc:
        payload = {"ok": False, "errors": [f"cannot load config: {exc}"], "warnings": []}
    except yaml.YAMLError as exc:
        payload = {"ok": False, "errors": [f"cannot parse config: {exc}"], "warnings": []}
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not payload["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
