# Persisted operational readiness

`chaos readiness [--max-age-seconds 3600]` is a JSON-only mode of the existing `chaos_status.py`
reporting surface, wrapped in the existing version-1 JSON envelope (`command: readiness`). It
bypasses onboarding and runs the diagnostic child with bytecode writing disabled. It does not
fetch, initialize/migrate a database, write a report, or interpret recent trading as successful
collection. The selected home is `CHAOS_HOME` (otherwise the usual application default).

## Evidence mapping

| JSON component | Persisted source | What can actually be established |
| --- | --- | --- |
| `producer.elite_wallet_ingest` | Latest filename-sorted `trading/reports/elite_wallets/elite_ingest_*.json`, schema `chaos.alpha_elite_ingest.v1` | Attempted/succeeded/failed wallet counts, inserted events, and the receipt's `completed_at_utc` establish completion and activity. No wallet attempted is `unsupported`, not healthy. `latest_source_event_timestamp_utc` is separately labelled source-event time and does not drive refresh health. |
| `consumers.autopilot_book` | Validated `trading/config/paper_autopilot.yaml`, its selected SQLite book, `events` and `paper_positions` | Configuration and stored linkage counts, latest allowlisted error. There is **no durable complete-tick receipt**: ordinary position events, absence of errors, and `last_tick_at` cannot establish successful complete ticks or healthy idle. A configured existing book therefore remains `unsupported` with `no_persisted_complete_tick_receipt`. An error is exposed as separate `latest_error.state: failed_refresh` with its own age; recovery is not inferred. |
| `consumers.elite_paper_book` | `trading/db/alpha_elite_paper.sqlite`, `cycle_receipts` and `episodes` | Latest inserted `command='cycle'` receipt, matching embedded command/schema/completion timestamp, boolean `payload.ok`, and opened/mark counts. Observe-only and mark-only receipts are not complete cycles. Episode linkage is counted separately. |

Ingest filenames are producer-generated UTC cycle IDs. Elite cycles use insertion order, so a later
malformed receipt cannot silently fall back to an earlier successful timestamp. This diagnostic
reports the **latest persisted evidence**, not an in-progress process, crash before receipt, or
unrecorded wrapper/provider error. A fresh receipt is not a guarantee of current upstream availability.

All health timestamps are normalized UTC, with an explicit `timestamp_basis`, `age_seconds`, and
`fresh` boolean where available. Missing, malformed, timezone-naive, and future health timestamps
are invalid evidence. Exactly the threshold age is still fresh. A recorded failure retains
`failed_refresh` even when old; `fresh` then becomes false. Producer and consumer states are
independent: a healthy idle consumer does not make a stale producer healthy.

## States

- `unconfigured`: missing or invalid autopilot configuration; no runnable configuration asserted.
- `unsupported`: missing required schema, unsupported receipt schema, evidence that cannot prove
  complete refresh, no ingest attempts, unsafe-to-read active SQLite journal, or an autopilot DB
  configured outside the selected home. The fixed `reason` explains which.
- `never_produced`: no receipt/book or no complete elite cycle receipt exists.
- `stale`: successful completion evidence older than the requested threshold.
- `healthy_idle`: successful recent completion with zero inserted events (producer) or zero opened
  episodes and marks (elite consumer). This means no counted activity, not no positions or candidates.
- `healthy`: successful recent completion with counted activity.
- `failed_refresh`: receipt records failure (or an explicitly labelled autopilot error observation).
- `invalid_data`: corrupt database, malformed JSON/YAML/counts/timestamps/linkage payload.
- `unreadable`: filesystem or read-access failure.

These states describe evidence, not permission to trade or a single deployment-ready boolean.
A successful CLI exit means the diagnostic ran; component problems are represented in JSON.

## Existing linkage, without schema changes

Autopilot reports position totals and counts of nonempty decision JSON snapshots, policy-version
values, and `paper_position_open` events linked by `position_id`. It does not claim a normalized
`decision_id` relation: the existing decision is a JSON snapshot. A policy version is **not** a
configuration version; `config_version_link` is explicitly `not_persisted`.

Elite reports episode totals and counts with source-event-ID arrays and both policy/roster versions.
It does not invent autopilot-style decision linkage for this distinct book. These are structural
presence counts, not validation of referenced events in another database or version reproducibility.

## Read-only and public-output boundary

SQLite is opened with `mode=ro&immutable=1`, `query_only`, explicit closure, and integrity/schema
checks. Any WAL or rollback journal is refused (`unsupported`) before and after reading,
because immutable reads would otherwise ignore committed WAL data or skip recovery. This avoids
creating SHM/WAL files and never checkpoints someone else's book. Such a book requires a separately
prepared, consistent checkpointed snapshot; this command does not create one. There is no atomic
cross-file snapshot: producers should be quiescent or the selected home should be a consistent
snapshot. File-race safety is conservative, not a locking protocol.

Output is deliberately allowlisted: aggregate counts, normalized timestamps, fixed labels/reasons.
No raw exception messages, config values, DB paths, receipt paths, wallets, mints, raw error strings,
opaque version values, or arbitrary metadata are emitted. Existing JSON-envelope redaction remains
in place as a second layer. The diagnostic does not assert all producer families are covered:
market-price, X/social, general smart-wallet tracker, signal-ledger outcome jobs and their provider
configuration are outside this narrow evidence mapping.

Regression coverage uses temporary homes, real SQLite schemas/receipts, the actual CLI subprocess,
filesystem snapshots, a write/network-denying Python audit hook, malformed/missing/corrupt input,
staleness and failure precedence, absent schemas, and real uncheckpointed WAL state.
