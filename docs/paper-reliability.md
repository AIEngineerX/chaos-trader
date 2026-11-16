# Paper tick reliability

The autopilot remains paper-only. These changes do not enable live execution,
add exit policies, or change scheduling.

## Atomic accounting

Each candidate transition and each open-position evaluation uses a SQLite
savepoint, acquiring SQLite's writer lock before policy/accounting reads. A failed
accounting transition rolls back its position, fill, fee, candidate, entry budget,
and accounting-event writes. The caller records its error separately. Earlier
completed transitions remain committed. Discovery writes are also isolated.
Existing books are not repaired or migrated.

## Request budgets, retries, and overlap

Deep-analysis attempts and eligible X requests are reserved atomically against
UTC-day counters and rolling-hour events, then committed **before** the analyzer
boundary. They are not part of the paper-accounting rollback. An analyzer error,
timeout, missing/malformed receipt, or subsequent SQLite failure retains the
reservation. A crash between reservation and dispatch can overcount; this is
intentional fail-closed accounting, not proof of an actual provider charge.

Only `x_request_made` exactly equal to JSON `false` releases an X reservation.
It decrements the original reservation day's X counter and changes that specific
hourly event to `x_search_not_requested`; it never refunds deep analysis. A crash
before this release commits retains the reservation. Exhausted deep-analysis
budgets defer the candidate; exhausted X budgets disable X for that analysis.
The existing numeric caps and entry policies are unchanged.

Independent runners on the same SQLite file serialize reservation checks and
accounting writes. After analysis, the writer-locked path checks whether the mint
already has a position, preventing a stale concurrent result from booking a
second entry. Monitoring rereads each position after acquiring the writer lock,
so another runner's completed trim/close is not applied twice. Concurrent
analyses may still consume separate reservations within the caps; requests are
not globally deduplicated. Process coordination uses SQLite, not an in-memory
lock or new service. A lock timeout fails closed rather than bypassing limits.
Discovery and market-read boundaries currently run inside their transition lock;
slow reads can increase contention. This is not distributed exactly-once delivery.

## Health contract

`chaos_paper_autopilot.py --once --raw` retains the existing result fields and adds:

- `stages.discovery`, `stages.candidates`, `stages.monitor`: `attempted`,
  `succeeded`, `failed`, `deferred`, and `status` (`ok`, `degraded`, or `deferred`). Discovery counters describe the stage, not individual providers.
- `error_count`: total failed work units across stages; `decisions` retains
  candidate result/error details for compatibility.
- `health`: `degraded` when a stage failed, otherwise `deferred` when the
  deadline postponed candidates, otherwise `ok`.
- `open_positions_attempted`: attempted refreshes; `open_positions_checked`
  now counts successful evaluations, not swallowed exceptions or missing marks.

`ok` is false for failed discovery, analysis, accounting, or monitoring. Both
text and JSON one-shot modes exit 1 for such a tick. `--max-cycles` continues after a
degraded tick, and its JSON-line/text output exposes that tick's health. A budget
postponement is not an error. Empty discovery is not an error. Failed discovery
can leave useful candidates to process, but the tick remains degraded.

An unavailable market mark is reported as a failed refresh, without closing the
position. Existing pair-gone and non-exitable policies are unchanged. SQLite
errors during discovery still propagate to the existing database diagnostic
handler rather than being presented as successful discovery.

## Configuration migration

Remove these unsupported keys from existing paper configuration (even a false
value is rejected):

- `loop.position_refresh_sec`: monitoring runs once per tick; use the existing
  `loop.discovery_interval_sec` for the tick loop, or your external scheduler.
- `exit.exit_on_gate_flip`
- `exit.exit_on_flow_conversion`

Those keys previously did nothing; removing them does not turn off an implemented
protection. The shipped config omits them and retains supported defaults.
`budgets.max_open_positions` must be a nonnegative integer, and
`exit.stop_pct_conviction` must be finite, at least -100, and strictly negative.
The root and recognized sections must be mappings. Invalid input is rejected
before the runner creates its database. Validation reports errors rather than
raising conversion/attribute errors; it does not mutate the caller's input.

## Offline regression coverage

From `chaos_trader/trading/scripts`, run:

```sh
python -W error::ResourceWarning -m unittest test_paper_reliability test_paper_retry -v
```

The tests use isolated temporary SQLite books and trigger real insert failures
at entry, exit, trim, fee, and event boundaries; they do not mock the transition
under test. Network-facing discovery/analysis/market reads are replaced with
fixtures. Use a scratch `TMPDIR` and a scratch `CHAOS_HOME`, never a live profile.
