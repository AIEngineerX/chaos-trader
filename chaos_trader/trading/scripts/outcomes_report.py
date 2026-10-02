#!/usr/bin/env python3
"""`chaos outcomes`: how past reads did at one horizon, per read label, with a hard minimum sample.

Reads `signals JOIN outcomes` from the signal ledger. A mark counts only when the outcome tick took it within
`PRIMARY_TOLERANCE_SECONDS` of its horizon (`primary_eligible`); a pair gone from DexScreener inside that window
is a -100% mark. One read counts per (mint, label) per UTC day: the first one, and when its mark was late or
missing, that mint and label are not scored that day, since a later read must not stand in for it. Reads of
tokens the user's own wallets hold (`owner_position_read`) and unread sweep candidates are never on the card.
A label with fewer than `MIN_N` reads gets no rate at all.
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from signal_calibration_report import median, pct  # noqa: E402
from signal_outcome_tracker import PRIMARY_TOLERANCE_SECONDS, WINDOWS, parse_ts  # noqa: E402

MIN_N = 20
WINDOW_LABELS = tuple(WINDOWS)
# The read labels a token read gives (`position_context.ALLOWED_ENTRY_ACTIONS`); always on the card, so an
# empty ledger still shows how far each one is from a score.
ENTRY_LABELS = ("study", "watch", "manual-review", "study-caution", "exit-liquidity-watch", "avoid-entry")
# Labels that warn about a token: their hit is a mark of -70% or worse, a pool under $1k, or a pair gone.
CAUGHT_LABELS = frozenset({"avoid-entry", "exit-liquidity-watch", "exit-watch"})
OWNER_KIND = "owner_position_read"
UNREAD = "sweep-candidate-unread"
TICK_STAMP = Path("trading") / "state" / "outcome_tick_last_run"
BOUNDARY = "Advisory + paper only. No wallet, signing, routing, or live execution."
CLOSING = "Small samples prove nothing."

ROWS_SQL = """
SELECT s.id, s.timestamp_utc, s.mint, s.verdict, s.signal_kind,
       o.checked_at_utc, o.return_pct, o.outcome_status, COALESCE(o.primary_eligible, 0) AS primary_eligible
FROM outcomes o JOIN signals s ON s.id = o.signal_id
WHERE o.window_label = ?
ORDER BY s.timestamp_utc, s.id
"""


def _rows(db_path: Path, window: str) -> list[dict[str, Any]]:
    """Outcome rows for one window; none when the ledger or its outcomes table does not exist yet."""
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path, timeout=30)) as con:
        con.row_factory = sqlite3.Row
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'").fetchone() is None:
            return []
        return [dict(row) for row in con.execute(ROWS_SQL, (window,))]


def unscored(n: int) -> dict[str, Any]:
    """A label under the floor: its read count and no rate."""
    return {"n": n, "median": None, "up_pct": None, "runner_pct": None, "severe_pct": None,
            "caught_pct": None, "status": "below_n"}


def _score(label: str, rows: list[dict[str, Any]], min_n: int) -> dict[str, Any]:
    n = len(rows)
    if n < min_n:
        return unscored(n)
    rets = [r["return_pct"] for r in rows if r["return_pct"] is not None]
    severe = [r["return_pct"] is not None and r["return_pct"] <= -70 for r in rows]
    caught = [hit or r["outcome_status"] in {"non_exitable", "delisted"} for r, hit in zip(rows, severe)]
    return {
        "n": n,
        "median": median(rets),
        "up_pct": pct(sum(1 for v in rets if v > 0), n),
        "runner_pct": pct(sum(1 for v in rets if v >= 100), n),
        "severe_pct": pct(sum(severe), n),
        "caught_pct": pct(sum(caught), n) if label in CAUGHT_LABELS else None,
        "status": "scored",
    }


def aggregate(db_path: Path, *, window: str, min_n: int = MIN_N, now: datetime | None = None) -> dict[str, Any]:
    """Per-label hit rates at `window` for reads marked on time, as of `now` (default: the current time)."""
    cutoff = now or datetime.now(timezone.utc)
    not_counted = {"late": 0, "owner": 0, "unread": 0}
    first: dict[tuple[str, str, Any], dict[str, Any]] = {}
    for row in _rows(Path(db_path), window):
        read_at = parse_ts(row["timestamp_utc"])
        if read_at > cutoff or parse_ts(row["checked_at_utc"]) > cutoff:
            continue
        if row["signal_kind"] == OWNER_KIND:
            not_counted["owner"] += 1
        elif row["verdict"] == UNREAD:
            not_counted["unread"] += 1
        else:
            first.setdefault((row["mint"], row["verdict"] or "unknown", read_at.date()), row)
    # The first read of the day is the one that counts, whatever its mark; a later read never stands in for it.
    groups: dict[str, list[dict[str, Any]]] = {label: [] for label in ENTRY_LABELS}
    reads = []
    for (_mint, label, _day), row in first.items():
        if row["outcome_status"] == "missing_late":
            not_counted["late"] += 1
        elif row["primary_eligible"]:
            groups.setdefault(label, []).append(row)
            reads.append(row)
    return {
        "window": window,
        "min_n": min_n,
        "scored_reads": len(reads),
        "scored_mints": len({r["mint"] for r in reads}),
        "since": min(parse_ts(r["timestamp_utc"]) for r in reads).date().isoformat() if reads else None,
        "labels": {label: _score(label, rows, min_n) for label, rows in groups.items()},
        "not_counted": not_counted,
    }


def read_last_run(home: Path) -> datetime | None:
    """When the outcome tick last finished cleanly, from the stamp it writes; None if it never has."""
    stamp = home / TICK_STAMP
    return parse_ts(stamp.read_text(encoding="utf-8").strip()) if stamp.exists() else None


def _ago(when: datetime) -> str:
    minutes = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} d ago"


def render_card(result: dict[str, Any], last_run: datetime | None) -> str:
    """Plain-text card: one block per label, a rate only at `min_n` reads or more, ending with the boundary line.

    The six read labels get a block each. Any other labels share one line, so the card stays inside a Telegram
    message whatever labels the ledger holds; a card asked for one label shows that label in full."""
    window, min_n, labels = result["window"], result["min_n"], result["labels"]
    extra = sorted(set(labels) - set(ENTRY_LABELS))
    shown = [label for label in ENTRY_LABELS if label in labels] + (extra if len(labels) == 1 else [])
    folded = [] if len(labels) == 1 else extra
    scored = [label for label in shown if labels[label]["status"] == "scored"]
    lines = [f"☄️ OUTCOMES · {window} after the read"]
    if not scored:
        lines.append("NO SCORES YET")
    marked = f"Marked on time: {result['scored_reads']} reads · {result['scored_mints']} mints"
    lines.append(f"{marked} · since {result['since']}" if result["since"] else marked)
    lines.append(f"One read per mint and label per UTC day; only marks taken within "
                 f"{PRIMARY_TOLERANCE_SECONDS // 60} minutes of the {window} horizon count.")
    lines.append(f"A label needs {min_n} reads before any rate is shown.")
    lines.append("")
    for label in shown:
        item = labels[label]
        if item["status"] != "scored":
            lines.append(f"{label.upper()} · {item['n']} of {min_n} reads, not scored yet")
            continue
        mid = "n/a" if item["median"] is None else f"{item['median']:+.1f}%"
        rates = (f"  median {mid} · up {item['up_pct']:.0f}% · 2x+ {item['runner_pct']:.0f}%"
                 f" · -70% or worse {item['severe_pct']:.0f}%")
        if item["caught_pct"] is not None:
            rates += f" · caught {item['caught_pct']:.0f}%"
        lines += [f"{label.upper()} · n {item['n']}", rates]
    if folded:
        reads = sum(labels[label]["n"] for label in folded)
        lines.append(f"OTHER · {len(folded)} more label{'s' if len(folded) != 1 else ''}, {reads} reads; "
                     "chaos outcomes <label> shows one")
    lines.append("")
    if any(labels[label]["caught_pct"] is not None for label in scored):
        lines.append("Caught: a mark of -70% or worse, a pool under $1k, or a pair gone from DexScreener.")
    lines.append(f"Not counted: {result['not_counted']['late']} late marks")
    if last_run is None:
        lines.append(f"Outcome tick has not run. A mark is taken only within {PRIMARY_TOLERANCE_SECONDS // 60} "
                     "minutes of its horizon, so a read it misses is never scored.")
    else:
        lines.append(f"Outcome tick last ran: {_ago(last_run)}")
    lines += [CLOSING, BOUNDARY]
    return "\n".join(lines)
