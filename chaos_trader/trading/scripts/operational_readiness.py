"""Persisted readiness evidence for chaos_status; never invoke a producer or migrate a book."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from paper_autopilot_config_check import load_config, validate


class EvidenceError(Exception):
    def __init__(self, state: str, reason: str):
        self.state, self.reason = state, reason


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(timezone.utc)


def outcome(at, *, now, max_age, failed=False, idle=False):
    dt = timestamp(at)
    age = (now - dt).total_seconds()
    if age < 0:
        raise ValueError("future timestamp")
    return {"state": "failed_refresh" if failed else "stale" if age > max_age else "healthy_idle" if idle else "healthy",
            "source_timestamp_utc": dt.isoformat(), "age_seconds": round(age, 3),
            "fresh": age <= max_age}


@contextmanager
def database(path):
    # immutable prevents even SQLite sidecar creation. Do not silently ignore a WAL.
    # Only quiescent, checkpointed books are supported by this diagnostic.
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        # pathlib reports symlink loops as RuntimeError on older Python versions.
        raise EvidenceError("unreadable", "database_path_unresolvable") from None
    def check():
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise EvidenceError("unsupported", "active_journal_requires_quiescent_checkpoint")
        return path.stat()

    if not path.exists():
        raise EvidenceError("never_produced", "database_missing")
    before = check()
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise EvidenceError("invalid_data", "database_corrupt")
        yield con
        after = check()
        if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
            raise EvidenceError("unsupported", "database_changed_during_read")
    finally:
        con.close()


def require(con, table, columns):
    present = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if not set(columns) <= present:
        raise EvidenceError("unsupported", "required_schema_absent")


def guarded(reader):
    try:
        return reader()
    except EvidenceError as exc:
        return {"state": exc.state, "reason": exc.reason}
    except sqlite3.DatabaseError:
        return {"state": "invalid_data", "reason": "database_unreadable"}
    except OSError:
        return {"state": "unreadable", "reason": "evidence_unreadable"}
    except (ValueError, TypeError, KeyError, OverflowError, yaml.YAMLError):
        return {"state": "invalid_data", "reason": "evidence_malformed"}


def nonnegative(data, key):
    value = data[key]
    if type(value) is not int or value < 0:
        raise ValueError("invalid count")
    return value


def elite_ingest(home, now, max_age):
    paths = sorted((home / "trading/reports/elite_wallets").glob("elite_ingest_*.json"))
    if not paths:
        return {"state": "never_produced", "reason": "no_ingest_receipt"}
    # Same filename ordering as elite_wallet_pipeline.latest_receipt; unlike that
    # helper, a malformed latest receipt is not treated as absent.
    receipt = json.loads(paths[-1].read_text(encoding="utf-8"))
    if receipt["schema"] != "chaos.alpha_elite_ingest.v1":
        raise EvidenceError("unsupported", "receipt_schema_unknown")
    counts = {key: nonnegative(receipt, key) for key in ("attempted", "succeeded", "failed", "events_inserted")}
    if counts["attempted"] != counts["succeeded"] + counts["failed"]:
        raise ValueError("inconsistent counts")
    result = outcome(receipt["completed_at_utc"], now=now, max_age=max_age,
                     failed=counts["failed"] > 0, idle=counts["events_inserted"] == 0)
    if counts["attempted"] == 0:
        result.update(state="unsupported", reason="no_wallet_attempted")
    result.update(counts)
    result["timestamp_basis"] = "ingest_receipt.completed_at_utc"
    metrics = receipt.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("invalid metrics")
    latest_event = metrics.get("latest_event_utc")
    result["latest_source_event_timestamp_utc"] = timestamp(latest_event).isoformat() if latest_event else None
    return result


def autopilot(home, now, max_age):
    config_path = home / "trading/config/paper_autopilot.yaml"
    if not config_path.exists():
        return {"state": "unconfigured", "reason": "paper_config_missing"}
    config = load_config(config_path)
    validation = validate(config)
    if not validation["ok"]:
        return {"state": "unconfigured", "reason": "paper_config_invalid", "validation_error_count": len(validation["errors"])}
    loop_enabled = config.get("loop", {}).get("enabled")
    if loop_enabled is not None and type(loop_enabled) is not bool:
        raise ValueError("invalid loop enabled flag")
    configured = (config.get("paths") or {}).get("sqlite") or "trading/db/paper_autopilot.sqlite"
    if not isinstance(configured, str):
        raise ValueError("invalid database path")
    try:
        path = Path(configured).expanduser()
    except RuntimeError:
        raise EvidenceError("invalid_data", "database_path_unexpandable") from None
    if not path.is_absolute():
        path = home / path
    # Do not follow configuration to unrelated books outside the selected home.
    try:
        resolved_path, resolved_home = path.resolve(), home.resolve()
    except (OSError, RuntimeError):
        raise EvidenceError("unreadable", "database_path_unresolvable") from None
    if not resolved_path.is_relative_to(resolved_home):
        return {"state": "unsupported", "reason": "database_outside_selected_home"}
    with database(path) as con:
        require(con, "paper_positions", ("id", "decision_json", "policy_version"))
        require(con, "events", ("id", "timestamp_utc", "event_type", "position_id"))
        total = con.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0]
        linked = con.execute("SELECT COUNT(*) FROM paper_positions p WHERE EXISTS (SELECT 1 FROM events e WHERE e.position_id=p.id AND e.event_type='paper_position_open')").fetchone()[0]
        decisions = 0
        for row in con.execute("SELECT decision_json FROM paper_positions"):
            decision = json.loads(row[0])
            if not isinstance(decision, dict):
                raise ValueError("invalid decision")
            decisions += bool(decision)
        versioned = con.execute("SELECT COUNT(*) FROM paper_positions WHERE policy_version IS NOT NULL AND policy_version != ''").fetchone()[0]
        result = {"state": "unsupported", "reason": "no_persisted_complete_tick_receipt",
                  "loop_enabled_config": loop_enabled,
                  "positions": total,
                  "linkage": {"positions_with_decision_snapshot": decisions,
                              "positions_with_open_event_link": linked,
                              "positions_with_policy_version": versioned,
                              "config_version_link": "not_persisted",
                              "decision_id_link": "not_persisted"}}
        error = con.execute("SELECT timestamp_utc FROM events WHERE event_type IN ('discovery_error','position_monitor_error','deep_analyze_error') ORDER BY id DESC LIMIT 1").fetchone()
        if error:
            result["latest_error"] = outcome(error[0], now=now, max_age=max_age, failed=True)
            result["latest_error"]["timestamp_basis"] = "events.timestamp_utc"
            # There is no durable full-cycle success to establish recovery.
            result["latest_error"]["recovery"] = "unknown_no_complete_tick_receipt"
        return result


def elite_paper(home, now, max_age):
    with database(home / "trading/db/alpha_elite_paper.sqlite") as con:
        require(con, "cycle_receipts", ("completed_at_utc", "command", "payload_json"))
        require(con, "episodes", ("episode_id", "source_event_ids_json", "policy_version", "roster_version"))
        row = con.execute("SELECT completed_at_utc,payload_json FROM cycle_receipts WHERE command='cycle' ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            return {"state": "never_produced", "reason": "no_complete_cycle_receipt"}
        receipt = json.loads(row[1])
        if receipt["schema"] != "chaos.alpha_elite_paper.v1" or receipt["command"] != "cycle":
            raise EvidenceError("unsupported", "receipt_schema_unknown")
        if timestamp(receipt["completed_at_utc"]) != timestamp(row[0]):
            raise ValueError("inconsistent timestamp")
        payload = receipt["payload"]
        if type(payload["ok"]) is not bool:
            raise ValueError("invalid outcome")
        observations, marking = payload["observations"], payload["marking"]
        opened = nonnegative(observations, "opened")
        marks = nonnegative(marking, "marks_written")
        result = outcome(row[0], now=now, max_age=max_age, failed=not payload["ok"], idle=opened == 0 and marks == 0)
        result.update(timestamp_basis="cycle_receipts.completed_at_utc", opened=opened, marks_written=marks)
        total = linked = versioned = 0
        for episode in con.execute("SELECT source_event_ids_json,policy_version,roster_version FROM episodes"):
            total += 1
            ids = json.loads(episode[0])
            if not isinstance(ids, list) or any(type(i) is not int for i in ids):
                raise ValueError("invalid source event ids")
            linked += bool(ids)
            versioned += bool(episode[1]) and bool(episode[2])
        result["episodes"] = total
        result["linkage"] = {"episodes_with_source_event_ids": linked,
                             "episodes_with_policy_and_roster_version": versioned,
                             "config_version_link": "not_persisted",
                             "decision_id_link": "not_persisted"}
        return result


def report(home: Path, *, max_age_seconds=3600, now=None):
    now = now or datetime.now(timezone.utc)
    return {"mode": "operational_readiness", "generated_at_utc": now.isoformat(),
            "max_age_seconds": max_age_seconds,
            "producer": {"elite_wallet_ingest": guarded(lambda: elite_ingest(home, now, max_age_seconds))},
            "consumers": {"autopilot_book": guarded(lambda: autopilot(home, now, max_age_seconds)),
                          "elite_paper_book": guarded(lambda: elite_paper(home, now, max_age_seconds))},
            "boundary": "read-only persisted evidence; no network, scheduling, or execution",
            "limitations": ["No deployment, credentials, or scheduler health is asserted.",
                            "Receipts describe completed attempts, not full roster coverage or upstream availability.",
                            "Trade time and file modification time never prove collection success.",
                            "SQLite reads require quiescent checkpointed books; active journals are unsupported.",
                            "Linkage counts show stored references, not verified cross-book referential integrity."]}
