#!/usr/bin/env python3
"""Chaos paper-only autopilot runner.

Build runner for the operator's strategy-faithful paper loop. It is deliberately
read-only and paper-only:

- no wallet connection
- no signing
- no order construction/routing
- no webhooks or live alerts
- no execution verbs in persisted position actions

The runner can execute bounded dry runs now. A continuous loop still requires an
explicit start command; config default remains loop.enabled=false.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
CONFIG_PATH = PROFILE_HOME / "trading" / "config" / "paper_autopilot.yaml"
DEFAULT_DB = PROFILE_HOME / "trading" / "db" / "paper_autopilot.sqlite"
DEFAULT_SMART_WALLET_DB = PROFILE_HOME / "trading" / "db" / "smart_wallets.sqlite"
PY = os.environ.get("CHAOS_PYTHON", sys.executable)
BOUNDARY = "paper-only autopilot; no wallet, signing, orders, routing, webhooks, alerts, or live execution"
STRATEGY_VERSION = "strategy_paper_engine_p0_v1"
POLICY_VERSION = "paper_policy_p0_v4_multilane"
ANALYZER_VERSION = "token_event_analyzer_p0_v1"
SOURCE_ROSTER_VERSION = "source_roster_p0_v1"
ELITE_BRIDGE_POLICY_VERSION = "elite_provenance_bridge_v1"
MAX_ELITE_LINEAGE_EVENTS = 100
FEE_MODEL_VERSION = "paper_fee_model_p0_v1"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from alpha_tape import ELITE_ACTIONABLE_WINDOW_MINUTES, EXCLUDED_SWEEP_MINTS, connect_ro, parse_utc, stop_if_corrupt_tape, sweep_payload  # noqa: E402
from dexscreener_client import fetch_token  # noqa: E402
from gate_classifier import classify_gate  # noqa: E402
from paper_autopilot_config_check import load_config, validate  # noqa: E402
from position_context import normalize_entry_gate  # noqa: E402
from smart_wallet_tracker import ONCHAIN_SOURCE_SQL  # noqa: E402
from strategy_paper_engine import decide as strategy_decide  # noqa: E402
from trending_token_sweep import discovery_candidates as trending_discovery_candidates, enrich as enrich_trending_candidates  # noqa: E402
import x_provider  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    state TEXT NOT NULL,
    discovered_at_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    sweep_hits INTEGER NOT NULL DEFAULT 0,
    candidate_score REAL,
    gate_label TEXT,
    market_cap REAL,
    liquidity_usd REAL,
    source_json TEXT NOT NULL DEFAULT '{}',
    last_deep_analyze_utc TEXT,
    last_decision TEXT,
    x_checked INTEGER NOT NULL DEFAULT 0,
    market_sweep_hits INTEGER NOT NULL DEFAULT 0,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    state TEXT NOT NULL,
    opened_at_utc TEXT NOT NULL,
    closed_at_utc TEXT,
    entry_price REAL,
    entry_market_cap REAL,
    entry_liquidity_usd REAL,
    simulated_notional_usd REAL,
    stop_pct REAL,
    tp1_pct REAL,
    tp2_pct REAL,
    time_stop_minutes INTEGER,
    strategy_version TEXT,
    policy_version TEXT,
    analyzer_version TEXT,
    source_roster_version TEXT,
    fee_model_version TEXT,
    decision_json TEXT NOT NULL,
    last_market_json TEXT NOT NULL DEFAULT '{}',
    high_watermark_price REAL,
    high_watermark_market_cap REAL,
    remaining_pct REAL NOT NULL DEFAULT 100,
    tp1_done INTEGER NOT NULL DEFAULT 0,
    tp2_done INTEGER NOT NULL DEFAULT 0,
    realized_r REAL NOT NULL DEFAULT 0,
    exit_reason TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    event_type TEXT NOT NULL,
    mint TEXT,
    position_id TEXT,
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('entry','trim','exit')),
    reason TEXT,
    timestamp_utc TEXT NOT NULL,
    original_fraction REAL NOT NULL CHECK(original_fraction >= 0 AND original_fraction <= 1),
    quantity_units REAL,
    execution_price_usd REAL,
    mark_source TEXT,
    mark_timestamp_utc TEXT,
    gross_cost_basis_usd REAL,
    gross_proceeds_usd REAL,
    gross_realized_pnl_usd REAL,
    remaining_fraction REAL NOT NULL CHECK(remaining_fraction >= 0 AND remaining_fraction <= 1),
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS paper_fees (
    fee_id TEXT PRIMARY KEY,
    fill_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    fee_type TEXT NOT NULL,
    bps REAL,
    fee_usd REAL,
    estimated INTEGER NOT NULL DEFAULT 1,
    timestamp_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(fill_id) REFERENCES paper_fills(fill_id)
);

CREATE TRIGGER IF NOT EXISTS paper_fills_no_update
BEFORE UPDATE ON paper_fills
BEGIN
    SELECT RAISE(ABORT, 'paper_fills is immutable');
END;

CREATE TRIGGER IF NOT EXISTS paper_fills_no_delete
BEFORE DELETE ON paper_fills
BEGIN
    SELECT RAISE(ABORT, 'paper_fills is immutable');
END;

CREATE TRIGGER IF NOT EXISTS paper_fees_no_update
BEFORE UPDATE ON paper_fees
BEGIN
    SELECT RAISE(ABORT, 'paper_fees is immutable');
END;

CREATE TRIGGER IF NOT EXISTS paper_fees_no_delete
BEFORE DELETE ON paper_fees
BEGIN
    SELECT RAISE(ABORT, 'paper_fees is immutable');
END;

CREATE TABLE IF NOT EXISTS budget_counters (
    day_utc TEXT NOT NULL,
    counter TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(day_utc, counter)
);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", [], {}):
            return default
        return int(float(value))
    except Exception:
        return default


def valid_fee_bps(value: Any) -> float | None:
    bps = as_float(value, None)
    if bps is None or not math.isfinite(bps) or bps < 0 or bps > 5000:
        return None
    return bps


@dataclass
class RunnerConfig:
    raw: dict[str, Any]
    db_path: Path

    @classmethod
    def from_file(cls, path: Path) -> "RunnerConfig":
        config = load_config(path)
        checked = validate(config)
        if not checked.get("ok"):
            raise SystemExit("paper autopilot config invalid: " + jdump(checked))
        rel_db = ((config.get("paths") or {}).get("sqlite") or str(DEFAULT_DB))
        db_path = Path(rel_db)
        if not db_path.is_absolute():
            db_path = PROFILE_HOME / db_path
        return cls(raw=config, db_path=db_path)

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for key in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(key)
        return default if cur is None else cur


def coerce_flag(value: Any) -> bool:
    """Strict truthiness for safety flags.

    `bool("false")` is True, so a YAML-quoted "false" would silently re-enable a
    lane that a config author meant to keep off. Only real booleans and explicit
    affirmative strings/numbers enable; anything else — including a malformed dict
    or an unrecognised string — stays off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return False


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class ClosingConnection(sqlite3.Connection):
    """Connection whose `with` block also closes it; open handles keep the DB file locked on Windows."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class PaperAutopilotRunner:
    def __init__(self, config: RunnerConfig):
        self.config = config
        self.db_path = config.db_path

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=30.0, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA)
        self.ensure_schema(con)
        return con

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        """Add non-execution paper-state columns to older DBs."""
        pos_cols = {str(r[1]) for r in con.execute("PRAGMA table_info(paper_positions)").fetchall()}
        wanted_pos = {
            "high_watermark_price": "REAL",
            "high_watermark_market_cap": "REAL",
            "remaining_pct": "REAL NOT NULL DEFAULT 100",
            "tp1_done": "INTEGER NOT NULL DEFAULT 0",
            "tp2_done": "INTEGER NOT NULL DEFAULT 0",
            "strategy_version": "TEXT",
            "policy_version": "TEXT",
            "analyzer_version": "TEXT",
            "source_roster_version": "TEXT",
            "fee_model_version": "TEXT",
        }
        for name, decl in wanted_pos.items():
            if name not in pos_cols:
                con.execute(f"ALTER TABLE paper_positions ADD COLUMN {name} {decl}")
        cand_cols = {str(r[1]) for r in con.execute("PRAGMA table_info(candidates)").fetchall()}
        wanted_cand = {
            "last_deep_analyze_utc": "TEXT",
            "last_decision": "TEXT",
            "x_checked": "INTEGER NOT NULL DEFAULT 0",
            "market_sweep_hits": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, decl in wanted_cand.items():
            if name not in cand_cols:
                con.execute(f"ALTER TABLE candidates ADD COLUMN {name} {decl}")

    def log_event(self, con: sqlite3.Connection, event_type: str, *, mint: str | None = None, position_id: str | None = None, message: str = "", payload: Any = None) -> None:
        con.execute(
            "INSERT INTO events(timestamp_utc,event_type,mint,position_id,message,payload_json) VALUES(?,?,?,?,?,?)",
            (now_utc(), event_type, mint, position_id, message, jdump(payload or {})),
        )


    @staticmethod
    def runtime_contract_keys() -> set[str]:
        return {
            "entry.allowed_decisions", "entry.allowed_entry_gates", "entry.blocked_entry_gates", "entry.require_wallet_or_catalyst", "entry.wallet_signal_lane_enabled", "entry.min_independent_elite_wallets", "entry.min_elite_weighted_score", "entry.min_wallet_clean_closed_positions", "entry.min_wallet_realized_pnl_sol", "entry.min_wallet_win_rate", "entry.single_wallet_a_probe_enabled", "entry.single_wallet_a_probe_notional_usd", "entry.blocked_social_catalysts",
            "market_discovery.enabled", "market_discovery.limit", "market_discovery.min_liquidity_usd", "market_discovery.min_market_cap_usd", "market_discovery.max_market_cap_usd", "market_discovery.min_h1_price_change_pct", "market_discovery.max_h1_price_change_pct", "market_discovery.min_volume_liquidity_ratio", "market_discovery.max_volume_liquidity_ratio", "market_discovery.repeat_sweep_hits_required", "market_discovery.probe_notional_usd",
            "budgets.max_open_positions", "budgets.max_new_entries_per_hour", "budgets.max_new_entries_per_day", "budgets.max_same_source_positions", "budgets.max_same_narrative_positions",
            "risk.max_daily_loss_r", "risk.max_consecutive_losses", "risk.pause_after_consecutive_losses_min", "risk.pause_on_deep_analyze_errors_per_hour", "risk.pause_on_dex_errors_per_hour", "risk.disable_x_on_x_errors_per_hour",
            "deep_analyze.max_per_day", "deep_analyze.max_per_hour", "x_research.max_per_day", "x_research.max_per_hour",
            "exit.stop_pct_conviction", "exit.stop_pct_low_cap", "exit.low_cap_time_stop_min", "exit.conviction_time_stop_min", "exit.tp1_pct", "exit.tp2_pct",
            "policy.version",
        }

    def versioning(self) -> dict[str, str]:
        return {
            "strategy_version": STRATEGY_VERSION,
            "policy_version": str(self.config.get("policy", "version", default=POLICY_VERSION) or POLICY_VERSION),
            "analyzer_version": ANALYZER_VERSION,
            "source_roster_version": SOURCE_ROSTER_VERSION,
            "fee_model_version": FEE_MODEL_VERSION,
        }

    def _add_blocker(self, decision: dict[str, Any], blocker: str) -> None:
        blockers = decision.setdefault("blockers", [])
        if blocker not in blockers:
            blockers.append(blocker)
        plan = decision.setdefault("paper_plan", {})
        triggers = plan.setdefault("required_trigger", [])
        if blocker not in triggers:
            triggers.append(blocker)

    def enrich_versions(self, decision: dict[str, Any]) -> dict[str, Any]:
        versions = self.versioning()
        decision.setdefault("versioning", {}).update(versions)
        for key, value in versions.items():
            decision.setdefault(key, value)
        return decision

    def _event_count_since(self, con: sqlite3.Connection, event_type: str, since: datetime) -> int:
        return int(con.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=? AND timestamp_utc>=?",
            (event_type, since.isoformat(timespec="seconds")),
        ).fetchone()[0])

    def _entry_gate_allowed(self, decision: dict[str, Any]) -> bool:
        allowed = {str(x).lower() for x in (self.config.get("entry", "allowed_entry_gates", default=[]) or [])}
        return not allowed or str(decision.get("entry_action") or "").lower() in allowed

    def _has_wallet_or_catalyst(self, decision: dict[str, Any]) -> bool:
        if as_int(decision.get("watch_wallet_hits"), 0) > 0:
            return True
        catalyst = decision.get("social_catalyst") or {}
        if str(catalyst.get("type") or "none") != "none" and str(catalyst.get("quality") or "none").lower() in {"medium", "high", "very high"}:
            return True
        x = decision.get("x") or {}
        return bool(x.get("success") and as_int(x.get("citations"), 0) > 0)

    def _decision_key(self, decision: dict[str, Any], kind: str) -> str | None:
        direct = decision.get(f"{kind}_key")
        if direct:
            return str(direct).strip().lower()
        if kind == "source":
            source = (decision.get("source_identity") or {}).get("source_identity_tier") or decision.get("source_identity_tier")
        else:
            catalyst = decision.get("social_catalyst") or {}
            source = catalyst.get("type") or decision.get("play_type")
        value = str(source or "").strip().lower()
        return value or None

    def _open_position_key_count(self, con: sqlite3.Connection, kind: str, key: str) -> int:
        count = 0
        for row in con.execute("SELECT decision_json FROM paper_positions WHERE state IN ('PAPER_OPEN','PAPER_TRIMMED')"):
            try:
                payload = json.loads(row[0] or "{}")
            except Exception:
                payload = {}
            if self._decision_key(payload, kind) == key:
                count += 1
        return count

    def _daily_loss_r(self, con: sqlite3.Connection) -> float:
        today = today_utc()
        row = con.execute("SELECT COALESCE(SUM(realized_r),0) FROM paper_positions WHERE state='PAPER_CLOSED' AND closed_at_utc>=?", (today + "T00:00:00+00:00",)).fetchone()
        return float(row[0] or 0.0)

    def _minutes_since_last_loss(self, con: sqlite3.Connection) -> float:
        row = con.execute("SELECT MAX(closed_at_utc) FROM paper_positions WHERE state='PAPER_CLOSED' AND realized_r < 0").fetchone()
        dt = parse_utc(row[0] if row else None)
        if dt is None:
            return float("inf")
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0

    def _consecutive_losses(self, con: sqlite3.Connection) -> int:
        rows = con.execute("SELECT realized_r FROM paper_positions WHERE state='PAPER_CLOSED' ORDER BY closed_at_utc DESC, updated_at_utc DESC LIMIT 20").fetchall()
        losses = 0
        for row in rows:
            if as_float(row[0], 0.0) is not None and float(row[0]) < 0:
                losses += 1
            else:
                break
        return losses

    def apply_exit_policy(self, decision: dict[str, Any]) -> None:
        if decision.get("decision") != "paper_enter":
            return
        plan = decision.setdefault("paper_plan", {})
        mode = str(decision.get("mode_context") or "")
        if mode == "low-cap trench":
            plan["stop_pct"] = float(self.config.get("exit", "stop_pct_low_cap", default=plan.get("stop_pct", -30)))
            plan["time_stop_minutes"] = int(self.config.get("exit", "low_cap_time_stop_min", default=plan.get("time_stop_minutes", 15)))
        else:
            plan["stop_pct"] = float(self.config.get("exit", "stop_pct_conviction", default=plan.get("stop_pct", -25)))
            plan["time_stop_minutes"] = int(self.config.get("exit", "conviction_time_stop_min", default=plan.get("time_stop_minutes", 25)))
        plan["tp1_pct"] = float(self.config.get("exit", "tp1_pct", default=plan.get("tp1_pct", 75)))
        plan["tp2_pct"] = float(self.config.get("exit", "tp2_pct", default=plan.get("tp2_pct", 200)))

    def apply_runtime_policy(self, con: sqlite3.Connection, decision: dict[str, Any]) -> dict[str, Any]:
        self.enrich_versions(decision)
        if decision.get("decision") != "paper_enter":
            return decision
        allowed_decisions = {str(x) for x in (self.config.get("entry", "allowed_decisions", default=["paper_enter"]) or [])}
        if str(decision.get("decision")) not in allowed_decisions:
            self._add_blocker(decision, "entry.allowed_decisions blocks decision")
        blocked_gates = {str(x).lower() for x in (self.config.get("entry", "blocked_entry_gates", default=[]) or [])}
        entry_action = str(decision.get("entry_action") or "").lower()
        if entry_action in blocked_gates:
            self._add_blocker(decision, f"entry.blocked_entry_gates blocks {entry_action}")
        if not self._entry_gate_allowed(decision):
            self._add_blocker(decision, f"entry.allowed_entry_gates blocks {entry_action}")
        if bool(self.config.get("entry", "require_wallet_or_catalyst", default=False)) and decision.get("paper_lane") != "market_structure_probe" and not self._has_wallet_or_catalyst(decision):
            self._add_blocker(decision, "entry.require_wallet_or_catalyst missing wallet/catalyst proof")
        catalyst_type = str((decision.get("social_catalyst") or {}).get("type") or "none")
        blocked_catalysts = {str(x) for x in (self.config.get("entry", "blocked_social_catalysts", default=[]) or [])}
        if catalyst_type in blocked_catalysts:
            self._add_blocker(decision, f"entry.blocked_social_catalysts blocks {catalyst_type}")
        hour_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        day_cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        hourly_max = int(self.config.get("budgets", "max_new_entries_per_hour", default=999999))
        daily_max = int(self.config.get("budgets", "max_new_entries_per_day", default=999999))
        if self._event_count_since(con, "paper_position_open", hour_cutoff) >= hourly_max:
            self._add_blocker(decision, "budgets.max_new_entries_per_hour reached")
        if self._event_count_since(con, "paper_position_open", day_cutoff) >= daily_max:
            self._add_blocker(decision, "budgets.max_new_entries_per_day reached")
        source_key = self._decision_key(decision, "source")
        source_max = int(self.config.get("budgets", "max_same_source_positions", default=999999))
        if source_key and self._open_position_key_count(con, "source", source_key) >= source_max:
            self._add_blocker(decision, "budgets.max_same_source_positions reached")
        narrative_key = self._decision_key(decision, "narrative")
        narrative_max = int(self.config.get("budgets", "max_same_narrative_positions", default=999999))
        if narrative_key and self._open_position_key_count(con, "narrative", narrative_key) >= narrative_max:
            self._add_blocker(decision, "budgets.max_same_narrative_positions reached")
        max_daily_loss = float(self.config.get("risk", "max_daily_loss_r", default=-999999))
        if self._daily_loss_r(con) <= max_daily_loss:
            self._add_blocker(decision, "risk.max_daily_loss_r pause active")
        max_losses = int(self.config.get("risk", "max_consecutive_losses", default=999999))
        pause_min = int(self.config.get("risk", "pause_after_consecutive_losses_min", default=60))
        if self._consecutive_losses(con) >= max_losses and self._minutes_since_last_loss(con) < max(1, pause_min):
            self._add_blocker(decision, "risk.max_consecutive_losses pause active")
        if self._event_count_since(con, "deep_analyze_error", hour_cutoff) >= int(self.config.get("risk", "pause_on_deep_analyze_errors_per_hour", default=999999)):
            self._add_blocker(decision, "risk.pause_on_deep_analyze_errors_per_hour active")
        if self._event_count_since(con, "position_monitor_error", hour_cutoff) >= int(self.config.get("risk", "pause_on_dex_errors_per_hour", default=999999)):
            self._add_blocker(decision, "risk.pause_on_dex_errors_per_hour active")
        if decision.get("blockers"):
            decision["decision"] = "paper_wait"
            decision.setdefault("warnings", []).append("runtime policy converted paper_enter to paper_wait")
        else:
            self.apply_exit_policy(decision)
        decision["_runtime_policy_checked"] = True
        return decision


    def get_counter(self, con: sqlite3.Connection, counter: str) -> int:
        row = con.execute("SELECT value FROM budget_counters WHERE day_utc=? AND counter=?", (today_utc(), counter)).fetchone()
        return int(row[0]) if row else 0

    def inc_counter(self, con: sqlite3.Connection, counter: str, inc: int = 1) -> int:
        day = today_utc()
        con.execute(
            "INSERT INTO budget_counters(day_utc,counter,value) VALUES(?,?,?) ON CONFLICT(day_utc,counter) DO UPDATE SET value=value+excluded.value",
            (day, counter, inc),
        )
        return self.get_counter(con, counter)

    def discover(self, con: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
        max_candidates = int(limit or self.config.get("budgets", "max_candidates", default=25))
        payload = sweep_payload(limit=max_candidates, dex=True)
        if not payload.get("ok"):
            stop_if_corrupt_tape()  # a corrupt wallet database fails the tick instead of passing for an empty tape
        rows = list(payload.get("candidates") or [])
        market_meta: dict[str, Any] = {"enabled": False}
        if bool(self.config.get("market_discovery", "enabled", default=False)):
            market_limit = max(1, min(max_candidates, int(self.config.get("market_discovery", "limit", default=5))))
            try:
                raw_market, market_meta = trending_discovery_candidates()
                generated_at = now_utc()
                for ranked in enrich_trending_candidates(raw_market, market_limit):
                    summary = ranked.get("summary") or {}
                    rows.append({
                        "mint": ranked.get("mint"),
                        "source": "dexscreener_trending",
                        "sources": ranked.get("sources") or [],
                        "generated_at_utc": generated_at,
                        "candidate_score": ranked.get("candidate_score"),
                        "gate": {"verdict": "market-trending"},
                        "signal": {"signal_type": "dexscreener-trending", "captured_at_utc": generated_at},
                        "market": {
                            "symbol": summary.get("symbol"),
                            "price_usd": summary.get("priceUsd"),
                            "market_cap": summary.get("marketCap") or summary.get("fdv"),
                            "liquidity_usd": summary.get("liquidity_usd"),
                            "volume_h1": summary.get("volume_h1"),
                            "volume_h24": summary.get("volume_h24"),
                            "txns_h1": summary.get("txns_h1"),
                            "price_change_h1": summary.get("priceChange_h1"),
                            "price_change_h24": summary.get("priceChange_h24"),
                            "url": summary.get("url") or ranked.get("discovery_url"),
                        },
                    })
            except Exception as exc:
                market_meta = {"enabled": True, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        avoided_retry_min = int(self.config.get("entry", "avoided_retry_min", default=240))
        retry_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(0, avoided_retry_min))).isoformat(timespec="seconds")
        discovered: list[dict[str, Any]] = []
        for row in rows:
            mint = str(row.get("mint") or "").strip()
            if not mint or mint == "None":
                continue
            market = row.get("market") or {}
            sig = row.get("signal") or {}
            gate = row.get("gate") or {}
            symbol = market.get("symbol") or sig.get("token_symbol")
            now = now_utc()
            market_hit = 1 if row.get("source") == "dexscreener_trending" else 0
            con.execute(
                """
                INSERT INTO candidates(mint,symbol,state,discovered_at_utc,last_seen_utc,sweep_hits,market_sweep_hits,candidate_score,gate_label,market_cap,liquidity_usd,source_json,updated_at_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mint) DO UPDATE SET
                  state=CASE
                    WHEN candidates.state='AVOIDED' AND candidates.updated_at_utc<=? THEN 'DISCOVERED'
                    ELSE candidates.state
                  END,
                  symbol=COALESCE(excluded.symbol,candidates.symbol),
                  last_seen_utc=excluded.last_seen_utc,
                  sweep_hits=candidates.sweep_hits+1,
                  market_sweep_hits=candidates.market_sweep_hits+excluded.market_sweep_hits,
                  candidate_score=excluded.candidate_score,
                  gate_label=excluded.gate_label,
                  market_cap=excluded.market_cap,
                  liquidity_usd=excluded.liquidity_usd,
                  source_json=excluded.source_json,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (
                    mint,
                    symbol,
                    "DISCOVERED",
                    now,
                    now,
                    1,
                    market_hit,
                    as_float(row.get("candidate_score"), None),
                    gate.get("verdict") or gate.get("gate"),
                    as_float(market.get("market_cap"), None),
                    as_float(market.get("liquidity_usd"), None),
                    jdump(row),
                    now,
                    retry_cutoff,
                ),
            )
            discovered.append(row)
        self.log_event(con, "discovery", message=f"{len(discovered)} candidates", payload={"count": len(discovered), "freshness": payload.get("freshness"), "market_discovery": market_meta, "tape_errors": payload.get("errors") or []})
        return discovered

    def market_probe_confirmation(self, con: sqlite3.Connection, mint: str) -> dict[str, Any]:
        row = con.execute("SELECT market_sweep_hits,source_json FROM candidates WHERE mint=?", (mint,)).fetchone()
        if row is None:
            return {"eligible": False, "lane": "none", "reason": "candidate missing"}
        try:
            source = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            source = {}
        if source.get("source") != "dexscreener_trending":
            return {"eligible": False, "lane": "none", "reason": "not a live market-discovery candidate"}
        market = source.get("market") or {}
        liq = as_float(market.get("liquidity_usd"), 0.0) or 0.0
        mc = as_float(market.get("market_cap"), 0.0) or 0.0
        h1 = as_float(market.get("price_change_h1"), None)
        vol_h1 = as_float(market.get("volume_h1"), 0.0) or 0.0
        vl = vol_h1 / liq if liq > 0 else None
        hits = int(row[0] or 0)
        blockers: list[str] = []
        checks = (
            (liq >= float(self.config.get("market_discovery", "min_liquidity_usd", default=25_000)), f"liquidity ${liq:,.0f}"),
            (float(self.config.get("market_discovery", "min_market_cap_usd", default=25_000)) <= mc <= float(self.config.get("market_discovery", "max_market_cap_usd", default=750_000)), f"market cap ${mc:,.0f}"),
            (h1 is not None and float(self.config.get("market_discovery", "min_h1_price_change_pct", default=10)) <= h1 <= float(self.config.get("market_discovery", "max_h1_price_change_pct", default=180)), f"h1 change {h1}"),
            (vl is not None and float(self.config.get("market_discovery", "min_volume_liquidity_ratio", default=1.5)) <= vl <= float(self.config.get("market_discovery", "max_volume_liquidity_ratio", default=25)), f"h1 volume/liquidity {vl}"),
            (hits >= int(self.config.get("market_discovery", "repeat_sweep_hits_required", default=2)), f"market sweep hits {hits}"),
        )
        for passed, label in checks:
            if not passed:
                blockers.append(label)
        return {
            "eligible": not blockers,
            "mint": mint,
            "lane": "market_structure_probe",
            "verified_by": "paper_autopilot.market_probe_confirmation_v1",
            "market_sweep_hits": hits,
            "liquidity_usd": liq,
            "market_cap": mc,
            "price_change_h1": h1,
            "volume_liquidity_ratio_h1": None if vl is None else round(vl, 4),
            "sources": source.get("sources") or [],
            "blockers": blockers,
        }

    def elite_source_confirmation(self, mint: str, *, as_of_utc: datetime | None = None) -> dict[str, Any]:
        if mint in EXCLUDED_SWEEP_MINTS:
            return {"eligible": False, "lane": "none", "reason": "mint excluded from elite sweep"}
        configured = self.config.get("paths", "smart_wallet_sqlite", default=None)
        db_path = Path(str(configured)).expanduser() if configured else DEFAULT_SMART_WALLET_DB
        if not db_path.is_absolute():
            db_path = PROFILE_HOME / db_path
        if not db_path.is_file():
            return {"eligible": False, "lane": "none", "reason": "smart-wallet database missing"}
        now = as_of_utc or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=ELITE_ACTIONABLE_WINDOW_MINUTES)
        source: sqlite3.Connection | None = None
        try:
            source = connect_ro(db_path)
            if source is None:
                return {"eligible": False, "lane": "none", "reason": "elite source unavailable: cannot open read-only"}
            rows = source.execute(
                f"""
                SELECT e.id, e.wallet, e.signature, e.block_time_utc, e.run_id,
                       e.event_type, e.side, e.source_id, e.confidence,
                       r.source_commit, r.notes, r.completed_at
                FROM wallet_token_events AS e
                JOIN ingestion_runs AS r ON r.run_id=e.run_id
                WHERE e.mint=?
                  AND e.run_id GLOB 'elite-*'
                  AND e.event_type='buy'
                  AND e.source_id IN ({ONCHAIN_SOURCE_SQL})
                  AND e.confidence IN ('medium','high')
                  AND r.source_id IN ({ONCHAIN_SOURCE_SQL})
                  AND r.status='completed'
                  AND r.completed_at IS NOT NULL
                  AND COALESCE(r.source_commit,'')<>''
                """,
                (mint,),
            ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            return {"eligible": False, "lane": "none", "reason": f"elite source unavailable: {type(exc).__name__}"}
        finally:
            if source is not None:
                source.close()
        fresh_rows: list[dict[str, Any]] = []
        for row in rows:
            observed = parse_utc(row[3])
            completed = parse_utc(row[11])
            roster_sha256 = str(row[9] or "")
            try:
                notes = json.loads(row[10] or "{}")
            except (TypeError, ValueError):
                notes = {}
            roster_version = notes.get("roster_version") if isinstance(notes, dict) else None
            notes_sha256 = notes.get("roster_sha256") if isinstance(notes, dict) else None
            wallet_tier_raw = str(notes.get("wallet_tier") or "").upper() if isinstance(notes, dict) else ""
            wallet_tier = wallet_tier_raw if wallet_tier_raw in {"A", "B", "C"} else None
            valid_roster = (
                re.fullmatch(r"[0-9a-f]{64}", roster_sha256) is not None
                and notes_sha256 == roster_sha256
                and isinstance(roster_version, str)
                and bool(roster_version.strip())
            )
            valid_completion = completed is not None and observed is not None and observed <= completed <= now
            if row[1] and observed is not None and cutoff <= observed <= now and valid_roster and valid_completion:
                assert completed is not None
                fresh_rows.append({
                    "event_id": int(row[0]),
                    "wallet": str(row[1]),
                    "signature": row[2],
                    "block_time_utc": observed.isoformat(timespec="seconds"),
                    "run_id": str(row[4]),
                    "event_type": str(row[5]),
                    "side": row[6],
                    "source_id": str(row[7]),
                    "confidence": str(row[8]),
                    "roster_sha256": roster_sha256,
                    "roster_version": roster_version,
                    "wallet_tier": wallet_tier,
                    "run_completed_at": completed.isoformat(timespec="seconds"),
                })
        if len(fresh_rows) > MAX_ELITE_LINEAGE_EVENTS:
            return {"eligible": False, "lane": "none", "reason": "elite lineage exceeds audit bound"}
        wallets = {row["wallet"] for row in fresh_rows}
        if not fresh_rows or not wallets:
            return {"eligible": False, "lane": "none", "reason": "no fresh elite buys"}
        min_clean_closed = int(self.config.get("entry", "min_wallet_clean_closed_positions", default=3))
        min_realized_pnl = float(self.config.get("entry", "min_wallet_realized_pnl_sol", default=0.0))
        min_win_rate = float(self.config.get("entry", "min_wallet_win_rate", default=0.4))
        placeholders = ",".join("?" for _ in wallets)
        eligibility_rows: dict[str, dict[str, Any]] = {}
        eligibility_source: sqlite3.Connection | None = None
        try:
            eligibility_source = connect_ro(db_path)
            if eligibility_source is None:
                return {"eligible": False, "lane": "none", "reason": "wallet live eligibility unavailable: cannot open read-only"}
            metrics = eligibility_source.execute(
                f"""
                SELECT wallet,
                       COUNT(*) AS clean_closed,
                       SUM(realized_pnl_sol) AS realized_pnl_sol,
                       SUM(CASE WHEN realized_pnl_sol>0 THEN 1 ELSE 0 END) AS wins
                FROM positions
                WHERE wallet IN ({placeholders})
                  AND status='closed'
                  AND COALESCE(transfer_contaminated,0)=0
                  AND realized_pnl_sol IS NOT NULL
                GROUP BY wallet
                """,
                tuple(sorted(wallets)),
            ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            return {"eligible": False, "lane": "none", "reason": f"wallet live eligibility unavailable: {type(exc).__name__}"}
        finally:
            if eligibility_source is not None:
                eligibility_source.close()
        for wallet, closed_count, pnl, wins in metrics:
            clean_closed = int(closed_count or 0)
            realized_pnl = float(pnl or 0.0)
            win_rate = (int(wins or 0) / clean_closed) if clean_closed else None
            eligibility_rows[str(wallet)] = {
                "clean_closed_positions": clean_closed,
                "realized_pnl_sol": round(realized_pnl, 9),
                "win_rate": None if win_rate is None else round(win_rate, 4),
                "eligible": clean_closed >= min_clean_closed and realized_pnl > min_realized_pnl and win_rate is not None and win_rate >= min_win_rate,
            }
        for wallet in wallets:
            eligibility_rows.setdefault(wallet, {"clean_closed_positions": 0, "realized_pnl_sol": 0.0, "win_rate": None, "eligible": False})
        qualified_wallets = {wallet for wallet, row in eligibility_rows.items() if row["eligible"]}
        fresh_rows = [row for row in fresh_rows if row["wallet"] in qualified_wallets]
        wallets = qualified_wallets
        if not fresh_rows or not wallets:
            return {
                "eligible": False,
                "lane": "none",
                "reason": "fresh elite buys came only from wallets failing current realized-performance eligibility",
                "wallet_live_eligibility": eligibility_rows,
            }
        tier_weights = {"A": 3, "B": 2, "C": 1}
        wallet_tiers: dict[str, str | None] = {}
        for wallet in wallets:
            known = {str(row["wallet_tier"]) for row in fresh_rows if row["wallet"] == wallet and row.get("wallet_tier") in tier_weights}
            wallet_tiers[wallet] = min(known, key=lambda tier: tier_weights[tier]) if known else None
        tier_breakdown = {tier: sum(1 for value in wallet_tiers.values() if value == tier) for tier in ("A", "B", "C")}
        tier_breakdown["unknown"] = sum(1 for value in wallet_tiers.values() if value is None)
        weighted_score = sum(tier_weights.get(value or "", 0) for value in wallet_tiers.values())
        times = [datetime.fromisoformat(row["block_time_utc"]) for row in fresh_rows]
        run_lineage: dict[str, dict[str, Any]] = {}
        for row in fresh_rows:
            run_lineage[row["run_id"]] = {
                "run_id": row["run_id"],
                "status": "completed",
                "completed_at": row["run_completed_at"],
                "roster_sha256": row["roster_sha256"],
                "roster_version": row["roster_version"],
                "wallet_tier": row["wallet_tier"],
            }
        event_receipts = [
            {key: row[key] for key in (
                "event_id", "wallet", "signature", "block_time_utc", "run_id",
                "event_type", "side", "source_id", "confidence", "wallet_tier",
            )}
            for row in sorted(fresh_rows, key=lambda item: item["event_id"])
        ]
        return {
            "eligible": True,
            "mint": mint,
            "lane": "elite",
            "verified_by": "paper_autopilot.elite_source_confirmation_v1",
            "bridge_policy_version": ELITE_BRIDGE_POLICY_VERSION,
            "run_id_prefix": "elite-",
            "wallet_count": len(wallets),
            "weighted_score": weighted_score,
            "tier_breakdown": tier_breakdown,
            "wallet_tiers": {wallet: wallet_tiers[wallet] for wallet in sorted(wallet_tiers)},
            "wallet_live_eligibility": {wallet: eligibility_rows[wallet] for wallet in sorted(eligibility_rows)},
            "buy_events": len(fresh_rows),
            "first_buy_utc": min(times).isoformat(timespec="seconds"),
            "latest_buy_utc": max(times).isoformat(timespec="seconds"),
            "max_age_minutes": ELITE_ACTIONABLE_WINDOW_MINUTES,
            "lineage": {
                "event_ids": [row["event_id"] for row in event_receipts],
                "events": event_receipts,
                "runs": [run_lineage[key] for key in sorted(run_lineage)],
            },
        }

    @staticmethod
    def apply_elite_source_confirmation(payload: dict[str, Any], confirmation: dict[str, Any], *, expected_mint: str | None = None) -> dict[str, Any]:
        mode_name = str((payload.get("mode_context") or {}).get("mode") or "").strip().lower()
        payload_mint = str(payload.get("mint") or "")
        confirmation_mint = str(confirmation.get("mint") or "")
        expected = str(expected_mint or payload_mint)
        if (
            not confirmation.get("eligible")
            or mode_name != "conviction trench"
            or not payload_mint
            or payload_mint != expected
            or confirmation_mint != expected
        ):
            return payload
        # The runner is the provenance boundary. Translate verified DB facts into
        # existing analyzer fields; shared classifiers never trust payload markers.
        source_payload = copy.deepcopy(payload)
        elite_wallets = int(confirmation.get("wallet_count") or 0)
        wallet_timing = dict(source_payload.get("wallet_timing") or {})
        wallet_timing["watch_wallet_hit_count"] = max(int(wallet_timing.get("watch_wallet_hit_count") or 0), elite_wallets)
        wallet_timing["quality_wallet_hit_count"] = max(int(wallet_timing.get("quality_wallet_hit_count") or 0), elite_wallets)
        timing_rows = list(wallet_timing.get("wallet_timing") or [])
        timing_rows.append({
            "first_touch_utc": confirmation.get("first_buy_utc"),
            "first_touch_type": "elite_buy",
            "source": "verified_elite_wallet_events",
        })
        wallet_timing["wallet_timing"] = timing_rows
        source_payload["wallet_timing"] = wallet_timing
        classification = dict(source_payload.get("classification") or {})
        validation = dict(classification.get("validation") or {})
        validation["quality_wallet_hits"] = max(int(validation.get("quality_wallet_hits") or 0), elite_wallets)
        classification["validation"] = validation
        source_payload["classification"] = classification
        source_gate = classify_gate(source_payload)
        source_entry_gate = normalize_entry_gate(source_gate, source_payload.get("classification"))
        if source_entry_gate.get("action") not in {"watch", "manual-review"}:
            return payload
        source_payload["gate"] = source_gate
        source_payload["entry_gate"] = source_entry_gate
        return source_payload

    def budget_available(self, con: sqlite3.Connection, counter: str, max_value: int) -> bool:
        return self.get_counter(con, counter) < max_value

    def active_candidates(self, con: sqlite3.Connection, limit: int = 10, *, with_x: bool = False) -> list[dict[str, Any]]:
        cooldown_min = int(self.config.get("deep_analyze", "cooldown_per_mint_min", default=10))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(0, cooldown_min))).isoformat(timespec="seconds")
        # "Active" requires a recent sweep sighting; stale rows otherwise outrank
        # fresh finds forever via sweep_hits and drain the analyze budget.
        max_age_hours = int(self.config.get("deep_analyze", "candidate_max_age_hours", default=24))
        seen_cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))).isoformat(timespec="seconds")
        # Only wallet candidates still inside the confirmation window jump the queue;
        # a stale one cannot be source-confirmed anyway, so it earns no priority.
        actionable_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ELITE_ACTIONABLE_WINDOW_MINUTES)).isoformat(timespec="seconds")
        # Two lanes, queried separately then interleaved. A single ordered query cannot
        # guarantee both lanes are represented: the elite ingest seeds a whole batch at
        # once that stays fresh for the full window, so any over-fetch large enough to
        # matter is still all-wallet. Wallet candidates EXPIRE (elite confirmation only
        # accepts buys inside ELITE_ACTIONABLE_WINDOW_MINUTES) while trending candidates
        # never do and are re-seen by every sweep — so whichever lane is given absolute
        # priority starves the other. Trending starved wallets first (298 aged out
        # unanalysed); absolute wallet priority then reversed it. Interleaving fixes both.
        #
        # Freshness is measured from THIS CANDIDATE'S buy (signal.captured_at_utc, which the
        # seeder sets from last_buy_utc), not from discovery and not from the shared
        # freshness block. freshness.latest.wallet_events is global tape freshness written
        # once for the whole batch, so a 70-minute-old buy inherited a 1-minute-old
        # timestamp and held priority it had not earned.
        # json_valid guards malformed source_json, which would otherwise abort the selector
        # and kill the whole tick. COALESCE keeps the NOT(...) lane total when the JSON path
        # is missing, so a row can never fall into neither lane.
        # CASE, not AND: SQLite does not guarantee AND short-circuits in a WHERE clause,
        # so json_extract could still run on a malformed row and abort the query. CASE
        # only evaluates its THEN branch.
        wallet_lane = (
            "COALESCE(CASE WHEN json_valid(source_json) THEN"
            " json_extract(source_json,'$.source')='elite-wallet-buys'"
            " AND json_extract(source_json,'$.signal.captured_at_utc') >= ?"
            " ELSE 0 END, 0)"
        )
        base = """
            SELECT * FROM candidates
            WHERE state IN ('DISCOVERED','PRE_FILTERED','PAPER_WAIT')
              AND last_seen_utc >= ?
              AND (
                last_deep_analyze_utc IS NULL
                OR last_deep_analyze_utc <= ?
                OR (?=1 AND state='PAPER_WAIT' AND COALESCE(x_checked,0)=0)
              )
              AND {lane}
            ORDER BY
              CASE WHEN (?=1 AND state='PAPER_WAIT' AND COALESCE(x_checked,0)=0) THEN 0 ELSE 1 END,
              CASE WHEN json_valid(source_json)
                     AND (CASE WHEN json_valid(source_json)
                               THEN json_extract(source_json,'$.source')='dexscreener_trending'
                               ELSE 0 END) THEN 0 ELSE 1 END,
              last_seen_utc DESC,
              market_sweep_hits DESC,
              COALESCE(candidate_score,0) DESC,
              sweep_hits DESC
            LIMIT ?
        """
        xflag = 1 if with_x else 0
        params = (seen_cutoff, cutoff, xflag, actionable_cutoff, xflag, limit)
        wallet_rows = [dict(r) for r in con.execute(base.format(lane=wallet_lane), params).fetchall()]
        other_rows = [dict(r) for r in con.execute(base.format(lane=f"NOT ({wallet_lane})"), params).fetchall()]
        picked: list[dict[str, Any]] = []
        # Unfinished X work keeps GLOBAL priority across both lanes. It was already deep
        # analysed and is waiting on one more step, so finishing it beats starting
        # something new. The ORDER BY can only rank within a lane now that the lanes are
        # queried separately, so this has to be hoisted here or the merge below would let
        # an ordinary wallet row displace a pending-X row.
        if with_x:
            pending_mints = {
                str(row.get("mint"))
                for row in wallet_rows + other_rows
                if str(row.get("state")) == "PAPER_WAIT" and not (row.get("x_checked") or 0)
            }
            for row in wallet_rows + other_rows:
                if len(picked) >= limit:
                    break
                if str(row.get("mint")) in pending_mints:
                    picked.append(row)
            taken = {str(row.get("mint")) for row in picked}
            wallet_rows = [r for r in wallet_rows if str(r.get("mint")) not in taken]
            other_rows = [r for r in other_rows if str(r.get("mint")) not in taken]
        # Wallet first: it expires, trending does not. At limit=1 there is only one slot,
        # so that single slot always goes to the expiring lane — deliberate, because
        # yielding it means letting a wallet candidate die for a trending one that will
        # still be there next tick. The deployed tick analyses two, so both lanes progress.
        while len(picked) < limit and (wallet_rows or other_rows):
            if wallet_rows:
                picked.append(wallet_rows.pop(0))
            if len(picked) < limit and other_rows:
                picked.append(other_rows.pop(0))
        return picked[:limit]

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("CHAOS_HOME", str(PROFILE_HOME))
        env.setdefault("HERMES_HOME", str(PROFILE_HOME))
        env.setdefault("PYTHONPATH", os.pathsep.join(p for p in [str(SCRIPT_DIR), os.environ.get("HERMES_AGENT_SRC"), env.get("PYTHONPATH", "")] if p))
        env["PYTHONIOENCODING"] = "utf-8"  # children write utf-8; run_analyze decodes utf-8
        return env

    def run_analyze(self, mint: str, *, x_enabled: bool) -> dict[str, Any]:
        deep = self.config.get("deep_analyze", default={}) or {}
        xcfg = self.config.get("x_research", default={}) or {}
        cmd = [
            PY,
            str(SCRIPT_DIR / "token_event_analyzer.py"),
            mint,
            "--tx-limit",
            str(int(deep.get("tx_limit") or 40)),
            "--x-days",
            str(int(xcfg.get("lookback_days") or 2)),
            "--source-command",
            "paper_autopilot",
        ]
        if x_enabled:
            cmd.append("--x")
        cmd.append("--raw")
        proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), env=self.env(), text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=620, check=False)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "analyze failed")[:1200])
        return json.loads(proc.stdout)

    def identity_mismatch_decision(self, con: sqlite3.Connection, requested_mint: str, observed_mint: Any, stage: str, *, x_enabled: bool = False) -> dict[str, Any]:
        observed = str(observed_mint or "<missing>")
        decision = {
            "ok": False,
            "mint": requested_mint,
            "decision": "paper_wait",
            "entry_action": "study",
            "reasons": [],
            "blockers": [f"identity mismatch at {stage}: requested mint does not match observed mint"],
            "warnings": [f"observed mint={observed}"],
            "watch_wallet_hits": 0,
            "source_confirmation": None,
            "x": {"enabled": bool(x_enabled), "success": False, "citations": 0},
            "paper_plan": {"required_trigger": ["analyzer and provenance mint identity must match"]},
            "boundary": BOUNDARY,
        }
        self.log_event(
            con,
            "deep_analyze_error",
            mint=requested_mint,
            message="mint identity mismatch",
            payload={"stage": stage, "requested_mint": requested_mint, "observed_mint": observed},
        )
        self.persist_decision(con, decision, expected_mint=requested_mint)
        return decision

    def decide_candidate(self, con: sqlite3.Connection, mint: str, *, use_x: bool = False) -> dict[str, Any]:
        # Cheap, deterministic wallet evidence comes first. Candidates that
        # cannot satisfy the source contract must not consume scarce market/X
        # analysis budget or enter through the old generic catalyst fallback.
        # entry.wallet_signal_lane_enabled controls whether wallet evidence may be the
        # stated REASON for an entry. When the lane is off, this suppresses the
        # source_confirmation channel, and the wallet_timing channel is suppressed below.
        # It does NOT remove wallet influence entirely — gate/classification/mode labels
        # arrive precomputed from the analyzer and wallet data helped produce them; do not
        # describe the flag as removing wallet data from the decision.
        wallet_lane = coerce_flag(self.config.get("entry", "wallet_signal_lane_enabled", default=False))
        source_confirmation = self.elite_source_confirmation(mint) if wallet_lane else {
            "eligible": False,
            "lane": "none",
            "reason": "wallet signal lane disabled by entry.wallet_signal_lane_enabled",
        }
        market_confirmation = self.market_probe_confirmation(con, mint)
        if source_confirmation.get("eligible") and str(source_confirmation.get("mint") or "") != mint:
            return self.identity_mismatch_decision(con, mint, source_confirmation.get("mint"), "confirmation", x_enabled=False)
        min_elite_wallets = int(self.config.get("entry", "min_independent_elite_wallets", default=2))
        min_elite_weighted_score = int(self.config.get("entry", "min_elite_weighted_score", default=4))
        elite_wallets = int(source_confirmation.get("wallet_count") or 0) if source_confirmation.get("eligible") else 0
        elite_weighted_score = int(source_confirmation.get("weighted_score") or 0) if source_confirmation.get("eligible") else 0
        standard_source_gate = source_confirmation.get("eligible") and elite_wallets >= max(1, min_elite_wallets) and elite_weighted_score >= max(1, min_elite_weighted_score)
        single_a_probe = (
            coerce_flag(self.config.get("entry", "single_wallet_a_probe_enabled", default=False))
            and source_confirmation.get("eligible")
            and elite_wallets == 1
            and int((source_confirmation.get("tier_breakdown") or {}).get("A") or 0) == 1
            and elite_weighted_score >= 3
        )
        market_probe = bool(market_confirmation.get("eligible"))
        if not standard_source_gate and not single_a_probe and not market_probe:
            decision = {
                "ok": True,
                "mint": mint,
                "decision": "paper_wait",
                "entry_action": "study",
                "reasons": ["source-first paper lane"],
                "blockers": [
                    f"elite source consensus below minimum: {elite_wallets}/{max(1, min_elite_wallets)} independent wallets",
                    f"elite tier weight below minimum: {elite_weighted_score}/{max(1, min_elite_weighted_score)}",
                ] + (["market probe: " + "; ".join(market_confirmation.get("blockers") or [])] if market_confirmation.get("lane") == "market_structure_probe" else []),
                "warnings": [],
                "watch_wallet_hits": elite_wallets,
                "source_confirmation": source_confirmation if source_confirmation.get("eligible") else None,
                "x": {"enabled": False, "success": False, "citations": 0},
                "paper_plan": {"required_trigger": ["fresh independent elite-wallet consensus before deep analysis"]},
                "boundary": BOUNDARY,
            }
            self.persist_decision(con, decision, expected_mint=mint)
            return decision
        deep_max = int(self.config.get("deep_analyze", "max_per_day", default=100))
        if not self.budget_available(con, "deep_analyze", deep_max):
            return {"ok": False, "decision": "budget_exhausted", "mint": mint, "boundary": BOUNDARY}
        hour_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        deep_hour_max = int(self.config.get("deep_analyze", "max_per_hour", default=999999))
        if self._event_count_since(con, "deep_analyze", hour_cutoff) >= deep_hour_max:
            return {"ok": False, "decision": "budget_exhausted", "mint": mint, "boundary": BOUNDARY, "reason": "deep_analyze.max_per_hour reached"}
        x_max = int(self.config.get("x_research", "max_per_day", default=25))
        x_hour_max = int(self.config.get("x_research", "max_per_hour", default=999999))
        x_enabled = bool(use_x) and self.budget_available(con, "x_search", x_max) and self._event_count_since(con, "x_search", hour_cutoff) < x_hour_max
        payload = self.run_analyze(mint, x_enabled=x_enabled)
        self.inc_counter(con, "deep_analyze")
        self.log_event(con, "deep_analyze", mint=mint, message="deep analyze consumed", payload={"x_enabled": x_enabled})
        if payload.get("x_request_made"):  # D2: charged only when a request actually reached the provider
            self.inc_counter(con, "x_search")
            self.log_event(con, "x_search", mint=mint, message="x search consumed", payload={})
        if str(payload.get("mint") or "") != mint:
            return self.identity_mismatch_decision(con, mint, payload.get("mint"), "analyzer", x_enabled=x_enabled)
        payload.pop("source_confirmation", None)
        if not wallet_lane:
            # Second wallet-evidence channel, independent of source_confirmation: the
            # analyzer's wallet_timing feeds strategy_paper_engine, where watch_hits is a
            # SUFFICIENT condition for paper_enter (strategy_paper_engine.py:159). Leaving
            # it in place kept the lane open through the analyzer even with the gates shut.
            payload["wallet_timing"] = {
                "watch_wallet_hit_count": 0,
                "quality_wallet_hit_count": 0,
                "wallet_timing": [],
                "suppressed_by": "entry.wallet_signal_lane_enabled=false",
            }
        if source_confirmation.get("eligible") and str(source_confirmation.get("mint") or "") != mint:
            return self.identity_mismatch_decision(con, mint, source_confirmation.get("mint"), "confirmation", x_enabled=x_enabled)
        payload = self.apply_elite_source_confirmation(payload, source_confirmation, expected_mint=mint)
        decision = strategy_decide(
            payload,
            base_risk_usd=float(self.config.get("paper_size", "base_risk_usd", default=100)),
            max_notional_usd=float(self.config.get("paper_size", "max_notional_usd", default=250)),
            liquidity_bps=float(self.config.get("paper_size", "liquidity_bps", default=50)),
            min_liquidity_usd=float(self.config.get("entry", "min_liquidity_usd", default=25000)),
            max_holder_pct=float(self.config.get("entry", "max_adjusted_holder_concentration_pct", default=45)),
        )
        if str(decision.get("mint") or "") != mint:
            return self.identity_mismatch_decision(con, mint, decision.get("mint"), "decision", x_enabled=x_enabled)
        decision["source_confirmation"] = source_confirmation if source_confirmation.get("eligible") else None
        decision["market_confirmation"] = market_confirmation if market_confirmation.get("lane") == "market_structure_probe" else None
        elite_entry = decision.get("decision") == "paper_enter" and source_confirmation.get("eligible")
        if elite_entry and not single_a_probe and elite_wallets < max(1, min_elite_wallets):
            decision["decision"] = "paper_wait"
            decision.setdefault("blockers", []).append(
                f"elite source consensus below minimum: {elite_wallets}/{max(1, min_elite_wallets)} independent wallets"
            )
            decision.setdefault("paper_plan", {}).setdefault("required_trigger", []).append(
                f"require {max(1, min_elite_wallets)} independent fresh elite wallets"
            )
        if elite_entry and not single_a_probe and elite_weighted_score < max(1, min_elite_weighted_score):
            decision["decision"] = "paper_wait"
            decision.setdefault("blockers", []).append(
                f"elite tier weight below minimum: {elite_weighted_score}/{max(1, min_elite_weighted_score)}"
            )
            decision.setdefault("paper_plan", {}).setdefault("required_trigger", []).append(
                f"require elite tier weight {max(1, min_elite_weighted_score)} (A=3, B=2, C=1)"
            )
        if elite_entry and decision.get("decision") == "paper_enter":
            # Attribution + concentration. _decision_key() derives the source budget key
            # from source_identity, which only the market lane used to set, so elite
            # positions were invisible to budgets.max_same_source_positions and persisted
            # with a null source. Three could open against a configured limit of two.
            decision.setdefault("source_identity", {"source_identity_tier": "elite-wallet-buys"})
            # Size by evidence strength. min_independent_elite_wallets is now 1, so a single
            # wallet of ANY tier clears the standard gate — but the probe cap only applied to
            # tier A, so weaker B/C evidence was sized 4x larger than A. Cap every
            # single-wallet entry; consensus (2+) earns full size.
            if elite_wallets <= 1:
                probe_cap = float(self.config.get("entry", "single_wallet_a_probe_notional_usd", default=25))
                plan = decision.setdefault("paper_plan", {})
                plan["simulated_notional_usd"] = min(float(plan.get("simulated_notional_usd") or probe_cap), probe_cap)
                decision["paper_lane"] = "single_wallet_probe"
                tier = "A" if single_a_probe else "B/C"
                decision.setdefault("warnings", []).append(f"exploratory paper-only probe: one currently qualified tier-{tier} wallet")
            else:
                decision.setdefault("paper_lane", "elite_consensus")
        # An admitted market probe whose analyzer independently returned paper_enter used to
        # be stored with paper_lane=None, so a position could not be attributed to the
        # hypothesis that produced it. Label it before the paper_wait promotion below.
        if market_probe and decision.get("decision") == "paper_enter" and not decision.get("paper_lane") and not elite_entry:
            decision["paper_lane"] = "market_structure_probe"
        if market_probe and decision.get("decision") == "paper_wait" and not decision.get("blockers"):
            allowed_market_actions = {"micro-watch", "watch", "deep-check", "manual-review"}
            flow_status = str((decision.get("flow") or {}).get("status") or "")
            attention_phase = str(decision.get("attention_phase") or "")
            if str(decision.get("entry_action") or "") in allowed_market_actions and flow_status in {"attention-converting", "holder-converting", "social-reflexivity", "clean-flow"} and attention_phase not in {"late", "failed"}:
                decision["decision"] = "paper_enter"
                decision["paper_lane"] = "market_structure_probe"
                decision["source_identity"] = {"source_identity_tier": "dexscreener_trending"}
                probe_cap = float(self.config.get("market_discovery", "probe_notional_usd", default=25))
                plan = decision.setdefault("paper_plan", {})
                plan["simulated_notional_usd"] = min(float(plan.get("simulated_notional_usd") or probe_cap), probe_cap)
                plan["required_trigger"] = [x for x in (plan.get("required_trigger") or []) if "wallet timing or credible catalyst" not in x]
                decision.setdefault("reasons", []).append("live market structure passed repeated-sweep and deep-flow confirmation")
        min_size = float(self.config.get("paper_size", "min_simulated_size_usd", default=25))
        if decision.get("decision") == "paper_enter" and float((decision.get("paper_plan") or {}).get("simulated_notional_usd") or 0) < min_size:
            decision["decision"] = "paper_wait"
            decision.setdefault("warnings", []).append(f"simulated size below min ${min_size}")
            decision.setdefault("paper_plan", {}).setdefault("required_trigger", []).append("liquidity must support minimum simulated size")
        decision = self.apply_runtime_policy(con, decision)
        self.persist_decision(con, decision, expected_mint=mint)
        return decision

    def persist_decision(self, con: sqlite3.Connection, decision: dict[str, Any], *, expected_mint: str | None = None) -> None:
        mint = decision.get("mint")
        if not mint:
            return
        if expected_mint is not None and str(mint) != str(expected_mint):
            raise ValueError("refusing to persist decision for a different mint")
        self.enrich_versions(decision)
        if decision.get("decision") == "paper_enter" and not decision.get("_runtime_policy_checked"):
            decision = self.apply_runtime_policy(con, decision)
        dec = decision.get("decision")
        state_map = {"paper_enter": "PAPER_OPEN", "paper_wait": "PAPER_WAIT", "paper_avoid": "AVOIDED"}
        new_state = state_map.get(str(dec), "PRE_FILTERED")
        con.execute("UPDATE candidates SET state=?, last_deep_analyze_utc=?, last_decision=?, x_checked=CASE WHEN ? THEN 1 ELSE COALESCE(x_checked,0) END, updated_at_utc=? WHERE mint=?", (new_state, now_utc(), str(dec), 1 if ((decision.get("x") or {}).get("enabled")) else 0, now_utc(), mint))
        self.log_event(con, str(dec), mint=mint, message=str(dec), payload=decision)
        if dec == "paper_enter":
            open_count = con.execute("SELECT COUNT(*) FROM paper_positions WHERE state IN ('PAPER_OPEN','PAPER_TRIMMED')").fetchone()[0]
            max_open = int(self.config.get("budgets", "max_open_positions", default=3))
            if open_count >= max_open:
                self.log_event(con, "paper_wait", mint=mint, message="max open positions reached", payload=decision)
                con.execute("UPDATE candidates SET state='PAPER_WAIT', updated_at_utc=? WHERE mint=?", (now_utc(), mint))
                return
            pos_id = f"paper-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
            market = decision.get("market") or {}
            plan = decision.get("paper_plan") or {}
            opened_at = now_utc()
            entry_mark_price = as_float(market.get("price_usd"), None)
            fee_model = self._fee_model()
            entry_slip = float(fee_model["entry_slippage_bps"]) / 10000.0 if fee_model else 0.0
            entry_price = entry_mark_price * (1.0 + entry_slip) if entry_mark_price is not None else None
            entry_market_cap = as_float(market.get("market_cap"), None)
            entry_liquidity = as_float(market.get("liquidity_usd"), None)
            simulated_notional = as_float(plan.get("simulated_notional_usd"), None)
            stop_pct = as_float(plan.get("stop_pct"), None)
            tp1_pct = as_float(plan.get("tp1_pct"), None)
            tp2_pct = as_float(plan.get("tp2_pct"), None)
            time_stop_minutes = as_int(plan.get("time_stop_minutes"), 0)
            con.execute(
                """
                INSERT INTO paper_positions(id,mint,symbol,state,opened_at_utc,entry_price,entry_market_cap,entry_liquidity_usd,simulated_notional_usd,stop_pct,tp1_pct,tp2_pct,time_stop_minutes,strategy_version,policy_version,analyzer_version,source_roster_version,fee_model_version,decision_json,high_watermark_price,high_watermark_market_cap,updated_at_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pos_id,
                    mint,
                    decision.get("symbol"),
                    "PAPER_OPEN",
                    opened_at,
                    entry_price,
                    entry_market_cap,
                    entry_liquidity,
                    simulated_notional,
                    stop_pct,
                    tp1_pct,
                    tp2_pct,
                    time_stop_minutes,
                    decision.get("strategy_version"),
                    decision.get("policy_version"),
                    decision.get("analyzer_version"),
                    decision.get("source_roster_version"),
                    decision.get("fee_model_version"),
                    jdump(decision),
                    entry_price,
                    entry_market_cap,
                    now_utc(),
                ),
            )
            self._record_entry_fill(con, {
                "id": pos_id,
                "mint": mint,
                "opened_at_utc": opened_at,
                "entry_price": entry_price,
                "entry_mark_price": entry_mark_price,
                "simulated_notional_usd": simulated_notional,
            }, market={**market, "generated_at_utc": market.get("generated_at_utc") or opened_at})
            self.log_event(con, "paper_position_open", mint=mint, position_id=pos_id, message="paper position opened", payload=decision)

    def fetch_market(self, mint: str) -> dict[str, Any]:
        dex = fetch_token("solana", mint, cache=True, ttl_seconds=10)
        s = dex.get("summary") or {}
        return {
            "ok": bool(dex.get("ok")),
            "mint": mint,
            "price_usd": as_float(s.get("priceUsd"), None),
            "market_cap": as_float(s.get("marketCap") or s.get("fdv"), None),
            "liquidity_usd": as_float(s.get("liquidity_usd"), None),
            "price_change_m5": as_float(s.get("priceChange_m5"), None),
            "volume_h1": as_float(s.get("volume_h1"), None),
            "url": s.get("url"),
            "generated_at_utc": now_utc(),
            "boundary": "read-only Dex market refresh",
        }

    def _position_return_pct(self, row: dict[str, Any], market: dict[str, Any]) -> float | None:
        entry_price = as_float(row.get("entry_price"), None)
        price = as_float(market.get("price_usd"), None)
        entry_mc = as_float(row.get("entry_market_cap"), None)
        mc = as_float(market.get("market_cap"), None)
        if entry_price and price:
            return ((price / entry_price) - 1.0) * 100.0
        if entry_mc and mc:
            return ((mc / entry_mc) - 1.0) * 100.0
        return None

    def _r_from_pct(self, pct: float, stop_pct: float | None) -> float:
        risk = abs(stop_pct or -25.0) or 25.0
        return pct / risk

    def _realized_r_increment(self, row: dict[str, Any], *, net_usd: float | None, return_pct: float, fraction: float) -> float:
        """R contribution of one realization, net of recorded fees.

        Risk unit is notional * |stop_pct|; the gross-percentage fallback only
        applies when no fee model produced a net figure.
        """
        notional = as_float(row.get("simulated_notional_usd"), None)
        stop_ref = abs(as_float(row.get("stop_pct"), -25.0) or -25.0)
        risk_usd = notional * stop_ref / 100.0 if notional else None
        if net_usd is not None and risk_usd:
            return net_usd / risk_usd
        return self._r_from_pct(return_pct, as_float(row.get("stop_pct"), -25.0)) * fraction

    def _fee_model(self) -> dict[str, Any] | None:
        raw = self.config.get("paper_fee_model", default={}) or {}
        if not isinstance(raw, dict):
            return None
        entry_bps = valid_fee_bps(raw.get("entry_bps"))
        exit_bps = valid_fee_bps(raw.get("exit_bps"))
        if entry_bps is None or exit_bps is None:
            return None
        entry_slippage_bps = valid_fee_bps(raw.get("entry_slippage_bps"))
        exit_slippage_bps = valid_fee_bps(raw.get("exit_slippage_bps"))
        return {
            "entry_bps": entry_bps,
            "exit_bps": exit_bps,
            "entry_slippage_bps": 0.0 if entry_slippage_bps is None else entry_slippage_bps,
            "exit_slippage_bps": 0.0 if exit_slippage_bps is None else exit_slippage_bps,
            "estimated": bool(raw.get("estimated", True)),
            "provenance": str(raw.get("provenance") or "paper-only fixed-bps estimate"),
        }

    def _exit_slippage_fraction(self) -> float:
        fee_model = self._fee_model()
        return float(fee_model["exit_slippage_bps"]) / 10000.0 if fee_model else 0.0

    def _net_fill_return_pct(self, fill_return_pct: float) -> float:
        """Apply exit slippage to a rule-level fill return (stop/TP/trail level or observed mark)."""
        return ((1.0 + fill_return_pct / 100.0) * (1.0 - self._exit_slippage_fraction()) - 1.0) * 100.0

    def _market_source(self, market: dict[str, Any]) -> str | None:
        if as_float(market.get("price_usd"), None) is not None:
            return "price_usd"
        if as_float(market.get("market_cap"), None) is not None:
            return "market_cap"
        return None

    def _position_quantity(self, row: dict[str, Any], fraction: float) -> float | None:
        notional = as_float(row.get("simulated_notional_usd"), None)
        entry_price = as_float(row.get("entry_price"), None)
        if notional is None or not entry_price:
            return None
        return (notional / entry_price) * fraction

    def _insert_paper_fill(
        self,
        con: sqlite3.Connection,
        *,
        fill_id: str,
        position_id: str,
        mint: str,
        side: str,
        reason: str,
        timestamp_utc: str,
        original_fraction: float,
        quantity_units: float | None,
        execution_price_usd: float | None,
        mark_source: str | None,
        mark_timestamp_utc: str | None,
        gross_cost_basis_usd: float | None,
        gross_proceeds_usd: float | None,
        gross_realized_pnl_usd: float | None,
        remaining_fraction: float,
        payload: dict[str, Any],
    ) -> bool:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO paper_fills(fill_id,position_id,mint,side,reason,timestamp_utc,original_fraction,quantity_units,execution_price_usd,mark_source,mark_timestamp_utc,gross_cost_basis_usd,gross_proceeds_usd,gross_realized_pnl_usd,remaining_fraction,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                fill_id,
                position_id,
                mint,
                side,
                reason,
                timestamp_utc,
                round(original_fraction, 12),
                quantity_units,
                execution_price_usd,
                mark_source,
                mark_timestamp_utc,
                gross_cost_basis_usd,
                gross_proceeds_usd,
                gross_realized_pnl_usd,
                round(remaining_fraction, 12),
                jdump(payload),
            ),
        )
        return cur.rowcount > 0

    def _insert_paper_fee(
        self,
        con: sqlite3.Connection,
        *,
        fill_id: str,
        position_id: str,
        fee_type: str,
        bps: float | None,
        fee_usd: float | None,
        estimated: bool,
        timestamp_utc: str,
        payload: dict[str, Any],
    ) -> None:
        con.execute(
            """
            INSERT OR IGNORE INTO paper_fees(fee_id,fill_id,position_id,fee_type,bps,fee_usd,estimated,timestamp_utc,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f"{fill_id}:{fee_type}",
                fill_id,
                position_id,
                fee_type,
                bps,
                fee_usd,
                1 if estimated else 0,
                timestamp_utc,
                jdump(payload),
            ),
        )

    def _record_entry_fill(self, con: sqlite3.Connection, row: dict[str, Any], *, market: dict[str, Any]) -> None:
        ts = str(row.get("opened_at_utc") or now_utc())
        position_id = str(row.get("id"))
        notional = as_float(row.get("simulated_notional_usd"), None)
        entry_price = as_float(row.get("entry_price"), None)
        fill_id = f"{position_id}:entry"
        fee_model = self._fee_model()
        payload = {
            "paper_only": True,
            "boundary": BOUNDARY,
            "fee_model_available": fee_model is not None,
            "entry_mark_price_usd": as_float(row.get("entry_mark_price"), None),
            "entry_slippage_bps": float(fee_model["entry_slippage_bps"]) if fee_model is not None else None,
            "provenance": "paper position open; immutable source ledger",
        }
        created = self._insert_paper_fill(
            con,
            fill_id=fill_id,
            position_id=position_id,
            mint=str(row.get("mint")),
            side="entry",
            reason="paper_enter",
            timestamp_utc=ts,
            original_fraction=1.0,
            quantity_units=(notional / entry_price) if (notional is not None and entry_price) else None,
            execution_price_usd=entry_price,
            mark_source=self._market_source(market),
            mark_timestamp_utc=str(market.get("generated_at_utc") or ts),
            gross_cost_basis_usd=notional,
            gross_proceeds_usd=None,
            gross_realized_pnl_usd=None,
            remaining_fraction=1.0,
            payload=payload,
        )
        if not created:
            return
        if fee_model is None or notional is None:
            self._insert_paper_fee(con, fill_id=fill_id, position_id=position_id, fee_type="entry", bps=None, fee_usd=None, estimated=True, timestamp_utc=ts, payload={"paper_only": True, "provenance": "paper fee model unavailable"})
            return
        fee_usd = notional * float(fee_model["entry_bps"]) / 10000.0
        self._insert_paper_fee(con, fill_id=fill_id, position_id=position_id, fee_type="entry", bps=float(fee_model["entry_bps"]), fee_usd=fee_usd, estimated=bool(fee_model["estimated"]), timestamp_utc=ts, payload={"paper_only": True, "provenance": fee_model["provenance"]})

    def _entry_fee_usd(self, con: sqlite3.Connection, position_id: str) -> float | None:
        row = con.execute("SELECT fee_usd FROM paper_fees WHERE position_id=? AND fee_type='entry' ORDER BY timestamp_utc LIMIT 1", (position_id,)).fetchone()
        return as_float(row[0], None) if row else None

    def _record_realization_fill(self, con: sqlite3.Connection, row: dict[str, Any], *, side: str, reason: str, market: dict[str, Any], return_pct: float, original_fraction: float, remaining_fraction: float) -> tuple[bool, float | None]:
        """Insert the immutable fill+fee rows. Returns (created, net_realized_pnl_usd)."""
        ts = now_utc()
        position_id = str(row.get("id"))
        fill_id = f"{position_id}:{side}:{reason}"
        entry_price = as_float(row.get("entry_price"), None)
        execution_price = entry_price * (1.0 + return_pct / 100.0) if entry_price else as_float(market.get("price_usd"), None)
        notional = as_float(row.get("simulated_notional_usd"), None)
        cost_basis = (notional * original_fraction) if notional is not None else None
        proceeds = (cost_basis * (1.0 + return_pct / 100.0)) if cost_basis is not None else None
        gross = (proceeds - cost_basis) if (proceeds is not None and cost_basis is not None) else None
        fee_model = self._fee_model()
        entry_fee = self._entry_fee_usd(con, position_id)
        allocated_entry_fee = (entry_fee * original_fraction) if entry_fee is not None else None
        exit_bps = float(fee_model["exit_bps"]) if fee_model is not None else None
        exit_fee = (proceeds * exit_bps / 10000.0) if (proceeds is not None and exit_bps is not None) else None
        net = (gross - allocated_entry_fee - exit_fee) if (gross is not None and allocated_entry_fee is not None and exit_fee is not None) else None
        payload = {
            "paper_only": True,
            "boundary": BOUNDARY,
            "provenance": "paper realization; immutable source ledger",
            "return_pct": return_pct,
            "exit_slippage_bps": float(fee_model["exit_slippage_bps"]) if fee_model is not None else None,
            "allocated_entry_fee_usd": allocated_entry_fee,
            "exit_fee_usd": exit_fee,
            "net_realized_pnl_usd": net,
            "fee_model_available": fee_model is not None,
        }
        created = self._insert_paper_fill(
            con,
            fill_id=fill_id,
            position_id=position_id,
            mint=str(row.get("mint")),
            side=side,
            reason=reason,
            timestamp_utc=ts,
            original_fraction=original_fraction,
            quantity_units=self._position_quantity(row, original_fraction),
            execution_price_usd=execution_price,
            mark_source=self._market_source(market),
            mark_timestamp_utc=str(market.get("generated_at_utc") or ts),
            gross_cost_basis_usd=cost_basis,
            gross_proceeds_usd=proceeds,
            gross_realized_pnl_usd=gross,
            remaining_fraction=remaining_fraction,
            payload=payload,
        )
        if not created:
            return False, None
        self._insert_paper_fee(
            con,
            fill_id=fill_id,
            position_id=position_id,
            fee_type="exit",
            bps=exit_bps,
            fee_usd=exit_fee,
            estimated=bool(fee_model.get("estimated", True)) if fee_model is not None else True,
            timestamp_utc=ts,
            payload={"paper_only": True, "provenance": fee_model["provenance"] if fee_model is not None else "paper fee model unavailable"},
        )
        return True, net

    def trim_position(self, con: sqlite3.Connection, row: dict[str, Any], *, trim_pct: float, reason: str, market: dict[str, Any], return_pct: float) -> None:
        return_pct = self._net_fill_return_pct(return_pct)
        remaining_value = as_float(row.get("remaining_pct"), None)
        remaining = max(0.0, 100.0 if remaining_value is None else remaining_value)
        # Configured TP trim percentages are percentages of the current
        # remaining position. TP1=50 then TP2=25 trims 50% then 12.5% of
        # original when both hit in the same fast market tick.
        trim_pct_remaining = min(100.0, max(0.0, float(trim_pct)))
        actual_trim = round(remaining * trim_pct_remaining / 100.0, 6)
        if actual_trim <= 0:
            return
        new_remaining = round(remaining - actual_trim, 6)
        original_fraction = actual_trim / 100.0
        created, net_usd = self._record_realization_fill(con, row, side="trim", reason=reason, market=market, return_pct=return_pct, original_fraction=original_fraction, remaining_fraction=new_remaining / 100.0)
        if not created:
            return
        add_r = self._realized_r_increment(row, net_usd=net_usd, return_pct=return_pct, fraction=original_fraction)
        realized = round((as_float(row.get("realized_r"), 0.0) or 0.0) + add_r, 6)
        state = "PAPER_TRIMMED" if new_remaining > 0 else "PAPER_CLOSED"
        closed_at = now_utc() if state == "PAPER_CLOSED" else row.get("closed_at_utc")
        con.execute(
            "UPDATE paper_positions SET state=?, closed_at_utc=?, remaining_pct=?, realized_r=?, last_market_json=?, exit_reason=?, updated_at_utc=? WHERE id=?",
            (state, closed_at, new_remaining, realized, jdump(market), reason, now_utc(), row["id"]),
        )
        self.log_event(con, "paper_trim" if state != "PAPER_CLOSED" else "paper_exit", mint=row.get("mint"), position_id=row.get("id"), message=reason, payload={"trim_pct": trim_pct_remaining, "trim_pct_basis": "remaining_position", "actual_trim_pct_of_original": actual_trim, "remaining_pct": new_remaining, "return_pct": return_pct, "realized_r": realized, "market": market})

    def close_position(self, con: sqlite3.Connection, row: dict[str, Any], *, reason: str, market: dict[str, Any], return_pct: float | None) -> None:
        if return_pct is None:
            self.log_event(con, "position_monitor_check", mint=row.get("mint"), position_id=row.get("id"), message="close skipped; market refresh missing price/mcap", payload=market)
            return
        return_pct = self._net_fill_return_pct(return_pct)
        remaining = max(0.0, as_float(row.get("remaining_pct"), 100.0) or 100.0)
        original_fraction = remaining / 100.0
        if original_fraction <= 0:
            return
        created, net_usd = self._record_realization_fill(con, row, side="exit", reason=reason, market=market, return_pct=return_pct, original_fraction=original_fraction, remaining_fraction=0.0)
        if not created:
            return
        add_r = self._realized_r_increment(row, net_usd=net_usd, return_pct=return_pct, fraction=original_fraction)
        realized = round((as_float(row.get("realized_r"), 0.0) or 0.0) + add_r, 6)
        con.execute(
            "UPDATE paper_positions SET state='PAPER_CLOSED', closed_at_utc=?, remaining_pct=0, realized_r=?, last_market_json=?, exit_reason=?, updated_at_utc=? WHERE id=?",
            (now_utc(), realized, jdump(market), reason, now_utc(), row["id"]),
        )
        self.log_event(con, "paper_exit", mint=row.get("mint"), position_id=row.get("id"), message=reason, payload={"return_pct": return_pct, "realized_r": realized, "market": market})

    def evaluate_position(self, con: sqlite3.Connection, row: dict[str, Any], market: dict[str, Any]) -> str:
        return_pct = self._position_return_pct(row, market)
        entry_liq = as_float(row.get("entry_liquidity_usd"), None)
        liq = as_float(market.get("liquidity_usd"), None)
        price = as_float(market.get("price_usd"), None)
        mc = as_float(market.get("market_cap"), None)
        high_price = max([v for v in [as_float(row.get("high_watermark_price"), None), price] if v is not None], default=None)
        high_mc = max([v for v in [as_float(row.get("high_watermark_market_cap"), None), mc] if v is not None], default=None)
        con.execute("UPDATE paper_positions SET high_watermark_price=?, high_watermark_market_cap=?, last_market_json=?, updated_at_utc=? WHERE id=?", (high_price, high_mc, jdump(market), now_utc(), row["id"]))
        opened = parse_utc(row.get("opened_at_utc"))
        age_min = ((datetime.now(timezone.utc) - opened).total_seconds() / 60.0) if opened else 0.0
        if return_pct is None:
            self.log_event(con, "position_monitor_check", mint=row.get("mint"), position_id=row.get("id"), message="market refresh missing price/mcap", payload=market)
            return "checked"
        if entry_liq and liq is not None and liq <= entry_liq * (1.0 + float(self.config.get("exit", "liquidity_break_pct", default=-25)) / 100.0):
            self.close_position(con, row, reason="liquidity_break", market=market, return_pct=return_pct)
            return "closed"
        stop_pct = as_float(row.get("stop_pct"), -25.0)
        if return_pct <= stop_pct:
            # Polled marks, not resting orders: book the observed gap, overshoot
            # included. Filling at the stop level would understate losses on
            # thin pairs and falsify the daily-loss brake.
            self.close_position(con, row, reason="stop_loss", market=market, return_pct=return_pct)
            return "closed"
        time_stop = as_int(row.get("time_stop_minutes"), 0)
        if time_stop and age_min >= time_stop and return_pct < 30.0:
            self.close_position(con, row, reason="time_stop", market=market, return_pct=return_pct)
            return "closed"
        action = "checked"
        tp1_level = as_float(row.get("tp1_pct"), 75.0)
        if row.get("tp1_done") in (0, None) and return_pct >= tp1_level:
            # TPs are limit orders: fill at the target level, not the later observed spike.
            self.trim_position(con, row, trim_pct=float(self.config.get("exit", "tp1_trim_pct", default=50)), reason="tp1", market=market, return_pct=tp1_level)
            con.execute("UPDATE paper_positions SET tp1_done=1 WHERE id=?", (row["id"],))
            row = dict(con.execute("SELECT * FROM paper_positions WHERE id=?", (row["id"],)).fetchone())
            action = "trimmed"
        tp2_level = as_float(row.get("tp2_pct"), 200.0)
        if row.get("tp2_done") in (0, None) and return_pct >= tp2_level:
            refreshed = dict(con.execute("SELECT * FROM paper_positions WHERE id=?", (row["id"],)).fetchone())
            self.trim_position(con, refreshed, trim_pct=float(self.config.get("exit", "tp2_trim_pct", default=25)), reason="tp2", market=market, return_pct=tp2_level)
            con.execute("UPDATE paper_positions SET tp2_done=1 WHERE id=?", (row["id"],))
            action = "trimmed"
        if action == "trimmed":
            return "trimmed"
        trailing = float(self.config.get("exit", "trailing_stop_after_tp1_pct", default=-35))
        if row.get("tp1_done") and high_price and price and price <= high_price * (1.0 + trailing / 100.0):
            # Same polling reality as the stop: book the observed mark.
            self.close_position(con, row, reason="trailing_stop", market=market, return_pct=return_pct)
            return "closed"
        self.log_event(con, "position_monitor_check", mint=row.get("mint"), position_id=row.get("id"), message="hold", payload={"return_pct": return_pct, "market": market})
        return "checked"

    def monitor_positions_once(self, con: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = [dict(r) for r in con.execute("SELECT * FROM paper_positions WHERE state IN ('PAPER_OPEN','PAPER_TRIMMED') ORDER BY opened_at_utc").fetchall()]
        for row in rows:
            try:
                market = self.fetch_market(str(row.get("mint")))
                self.evaluate_position(con, row, market)
            except Exception as exc:
                self.log_event(con, "position_monitor_error", mint=row.get("mint"), position_id=row.get("id"), message=str(exc)[:500], payload={})
        return rows

    def run_once(self, *, limit: int | None = None, analyze_top: int = 0, with_x: bool = False) -> dict[str, Any]:
        with_x = bool(with_x) and x_provider.provider_name() != "none"  # D3: no provider is X off
        with self.connect() as con:
            discovered = self.discover(con, limit=limit)
            decisions: list[dict[str, Any]] = []
            if analyze_top > 0:
                for c in self.active_candidates(con, limit=analyze_top, with_x=with_x):
                    try:
                        decisions.append(self.decide_candidate(con, c["mint"], use_x=with_x))
                    except Exception as exc:
                        self.log_event(con, "deep_analyze_error", mint=c.get("mint"), message=str(exc)[:500], payload={})
                        decisions.append({"ok": False, "mint": c.get("mint"), "decision": "error", "error": str(exc)[:500]})
            open_positions = self.monitor_positions_once(con)
            con.commit()
            summary = self.status(con)
        return {
            "ok": True,
            "mode": "paper_autopilot_run_once",
            "generated_at_utc": now_utc(),
            "boundary": BOUNDARY,
            "discovered": len(discovered),
            "decisions": decisions,
            "open_positions_checked": len(open_positions),
            "status": summary,
        }

    def status(self, con: sqlite3.Connection | None = None) -> dict[str, Any]:
        close = False
        if con is None:
            con = self.connect()
            close = True
        try:
            states = {r["state"]: r["n"] for r in con.execute("SELECT state, COUNT(*) n FROM candidates GROUP BY state").fetchall()}
            pos_states = {r["state"]: r["n"] for r in con.execute("SELECT state, COUNT(*) n FROM paper_positions GROUP BY state").fetchall()}
            counters = {r["counter"]: r["value"] for r in con.execute("SELECT counter,value FROM budget_counters WHERE day_utc=?", (today_utc(),)).fetchall()}
            return {
                "ok": True,
                "mode": "paper_autopilot_status",
                "db": str(self.db_path),
                "boundary": BOUNDARY,
                "loop_enabled_config": bool(self.config.get("loop", "enabled", default=False)),
                "candidate_states": states,
                "position_states": pos_states,
                "today_counters": counters,
                "caps": {
                    "max_candidates": self.config.get("budgets", "max_candidates", default=25),
                    "max_open_positions": self.config.get("budgets", "max_open_positions", default=3),
                    "max_new_entries_per_hour": self.config.get("budgets", "max_new_entries_per_hour", default=None),
                    "max_new_entries_per_day": self.config.get("budgets", "max_new_entries_per_day", default=None),
                    "max_same_source_positions": self.config.get("budgets", "max_same_source_positions", default=None),
                    "max_same_narrative_positions": self.config.get("budgets", "max_same_narrative_positions", default=None),
                    "deep_analyze_per_day": self.config.get("deep_analyze", "max_per_day", default=100),
                    "deep_analyze_per_hour": self.config.get("deep_analyze", "max_per_hour", default=None),
                    "x_per_day": self.config.get("x_research", "max_per_day", default=25),
                    "x_per_hour": self.config.get("x_research", "max_per_hour", default=None),
                    "max_daily_loss_r": self.config.get("risk", "max_daily_loss_r", default=None),
                    "max_consecutive_losses": self.config.get("risk", "max_consecutive_losses", default=None),
                },
            }
        finally:
            if close:
                con.close()

    def run_loop(self, *, max_cycles: int | None = None, analyze_top: int = 0, with_x: bool = False) -> dict[str, Any]:
        if not bool(self.config.get("loop", "enabled", default=False)) and max_cycles is None:
            raise SystemExit("loop.enabled=false; use --once or --max-cycles for bounded paper dry run")
        interval = int(self.config.get("loop", "discovery_interval_sec", default=30))
        cycles = 0
        last: dict[str, Any] | None = None
        while True:
            cycles += 1
            last = self.run_once(analyze_top=analyze_top, with_x=with_x)
            print(json.dumps({"cycle": cycles, "discovered": last.get("discovered"), "open_positions_checked": last.get("open_positions_checked"), "boundary": BOUNDARY}, sort_keys=True), flush=True)
            if max_cycles is not None and cycles >= max_cycles:
                break
            time.sleep(max(1, interval))
        return {"ok": True, "cycles": cycles, "last": last, "boundary": BOUNDARY}


def compact(payload: dict[str, Any]) -> str:
    if payload.get("mode") == "paper_autopilot_status":
        return "\n".join([
            "☄️ PAPER AUTOPILOT STATUS",
            f"DB: {payload.get('db')}",
            f"Loop enabled in config: {payload.get('loop_enabled_config')}",
            f"Candidates: {payload.get('candidate_states')}",
            f"Positions: {payload.get('position_states')}",
            f"Counters: {payload.get('today_counters')}",
            BOUNDARY,
        ])
    return "\n".join([
        "☄️ PAPER AUTOPILOT RUN",
        f"Mode: {payload.get('mode')}",
        f"Discovered: {payload.get('discovered')}",
        f"Decisions: {len(payload.get('decisions') or [])}",
        f"Errors: {sum(1 for d in payload.get('decisions') or [] if d.get('decision') == 'error')}",
        f"Open positions checked: {payload.get('open_positions_checked')}",
        BOUNDARY,
    ])


def main() -> None:
    p = argparse.ArgumentParser(description="Chaos paper-only autopilot runner. No execution.")
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument("--status", action="store_true")
    p.add_argument("--once", action="store_true", help="Run one bounded discovery/monitor cycle")
    p.add_argument("--max-cycles", type=int, help="Run bounded loop for N cycles")
    p.add_argument("--limit", type=int, help="Discovery candidate limit override")
    p.add_argument("--analyze-top", type=int, default=0, help="Deep-analyze top N candidates during this run")
    p.add_argument("--with-x", action="store_true", help="Allow X in deep candidate decisions; still capped by config counters")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()
    if args.with_x and x_provider.provider_name() == "none":
        print(x_provider.no_provider_notice(), file=sys.stderr)

    cfg = RunnerConfig.from_file(Path(args.config).expanduser())
    runner = PaperAutopilotRunner(cfg)
    if args.status:
        out = runner.status()
    elif args.once:
        out = runner.run_once(limit=args.limit, analyze_top=max(0, args.analyze_top), with_x=bool(args.with_x))
    elif args.max_cycles:
        out = runner.run_loop(max_cycles=max(1, args.max_cycles), analyze_top=max(0, args.analyze_top), with_x=bool(args.with_x))
    else:
        raise SystemExit("Choose --status, --once, or --max-cycles. Continuous loop is not started by default.")
    print(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False, default=str) if args.raw else compact(out))
    raise SystemExit(1 if any(d.get("decision") == "error" for d in out.get("decisions") or []) else 0)


if __name__ == "__main__":
    main()
