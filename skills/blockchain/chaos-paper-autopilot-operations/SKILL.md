---
name: chaos-paper-autopilot-operations
description: Use when running, monitoring, tuning, stopping, or reporting the chaos-trader paper-only autopilot. Enforces bounded runs, provenance-rich P&L, config-to-code verification, candidate rotation checks, and proof before scheduling.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, solana, paper-trading, autopilot, pnl, operations]
    related_skills: [chaos-risk-engine, chaos-trade-journal, chaos-alpha-decoder]
---

# Chaos Paper Autopilot Operations

Operate the chaos-trader paper engine as a bounded simulation system. It is not a live executor and must never be presented as one.

## Prerequisites

- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

Every report must identify:

```text
mode: paper / simulated
source database
reporting window
freshness timestamp
realized P&L
unrealized P&L or unavailable reason
fees or unavailable reason
open and closed positions
```

## When to Use

Use for:

- `chaos_paper_autopilot.py` runs;
- burn-in monitoring;
- paper P&L questions;
- paper config changes;
- candidate-rotation problems;
- deciding whether a paper strategy is ready for scheduling.

Do not use for live signing, wallet funding, or live execution; this package has none (see chaos-execution-control).

## Paths

The script runs with `chaos run chaos_paper_autopilot`; state lives under `CHAOS_HOME`.

```text
chaos_paper_autopilot.py
trading/config/paper_autopilot.yaml
trading/db/paper_autopilot.sqlite
trading/reports/
```

`CHAOS_HOME` is the directory `chaos onboard` created (default `~/.chaos-trader`). On a Hermes install, `HERMES_HOME` is accepted as a fallback.

## Operating Sequence

### 1. Preflight

Verify config parses, DB integrity passes, no duplicate runner exists, and the requested run is bounded.

```bash
chaos run chaos_paper_autopilot --status --raw
```

Check the OS process table for an existing runner before starting another. Completion criterion: one or zero runners, valid config, `PRAGMA integrity_check = ok`.

### 2. Choose a bounded run

Single cycle:

```bash
chaos run chaos_paper_autopilot --once --raw
```

Burn-in:

```bash
chaos run chaos_paper_autopilot --max-cycles 40 --with-x --raw
```

Never start an unbounded loop from a chat or agent session. Use `--max-cycles` and track the process handle. Completion criterion: command includes an explicit cycle budget.

### 3. Monitor

Collect:

- discovery cycles;
- new candidates;
- repeated candidates;
- paper-enter, paper-avoid, paper-wait counts;
- open and closed positions;
- deep-analysis and X-search budget use;
- last event timestamp;
- runner PID and elapsed time.

If discovery repeatedly returns only terminal candidate states, classify the bottleneck as candidate rotation. Do not loosen entry gates merely to force simulated trades.

### 4. Report P&L

Use the SQLite DB read-only. Never infer P&L from candidate scores.

Rules:

- realized P&L requires a closed position and exit value;
- unrealized P&L requires an open position and a current mark;
- net P&L requires available fees;
- no mark means `unrealized_pnl: unavailable`, not zero;
- no trades means `realized_pnl: 0` only when the DB proves zero closed positions;
- always state paper provenance.

### 5. Tune one variable class

Change one class per burn-in:

- candidate sourcing/rotation;
- deep-analysis budget;
- X budget;
- entry gates;
- paper sizing;
- monitoring/exit cadence.

Do not change sourcing and entry strictness in the same cohort. Completion criterion: the next cohort has one attributable change and a before/after report.

## Config Enforcement Contract

Before exposing or changing a YAML field, prove the runner reads it and the value changes runtime behavior.

Classify each field:

```text
enforced     code reads and applies it
observed     code reads it for reporting only
decorative   present in YAML but unused
unknown      not yet traced
```

Only `enforced` fields may be presented to a user as tunable. Add a contract test whenever an enforced setting is exposed.

## Scheduling Gate

The package ships six locked, bounded, paper-only wrappers that a scheduler (system cron, Task Scheduler, or the agent harness's own scheduler) can call. None is scheduled by the package; the user creates any schedule.

```text
chaos run chaos_paper_autopilot_tick     one discover/decide/monitor pass of the paper autopilot
chaos run chaos_wallet_discovery_tick    enrich a bounded batch of never-scored edge-wallets, then re-rank (Helius key needed)
chaos run chaos_paper_learning_tick      daily paper learning report, compact stdout
chaos run chaos_alpha_elite_ingest       bounded elite-wallet ingestion (any RPC)
chaos run chaos_alpha_elite_paper_tick   one tick of the elite-wallet paper cohort
chaos run chaos_alpha_elite_paper_cycle  a bounded elite-wallet paper burn-in
```

Run each one by hand first, for example `chaos run chaos_paper_autopilot_tick`. If a user already schedules these, do not disable them to satisfy the checklist below; it governs NEW automation and strategy evaluation. Treat a new install as burn-in and do not draw strategy conclusions until full paper entry-to-exit lifecycles accumulate.

Before scheduling any NEW recurring job, all must be true:

- bounded burn-ins complete without duplicate processes;
- interruption and resume behavior are tested;
- P&L provenance and fee handling are correct;
- candidate rotation prevents terminal-state rediscovery loops;
- stop/status/report controls are available;
- daily tool/X budgets are enforced in code;
- The user has explicitly approved the new standing automation.

## Common Pitfalls

1. **Calling an avoid-only loop healthy.** It may be functioning but producing no strategy evidence.
2. **Reporting zero for unavailable P&L.** Unknown mark or fees must stay unknown.
3. **Trusting YAML labels.** A configured cap is not a cap until code and tests enforce it.
4. **Starting a second runner.** Check process state first.
5. **Loosening gates to manufacture activity.** Fix sourcing and calibration before risk discipline.
6. **Treating paper success as live readiness.** Live execution has separate security gates.
7. **Letting a source-specific hunter consume the global candidate backlog.** Wallet-seeded runs must analyze only mints emitted by that cycle, while still applying terminal-state, cooldown, and budget checks. Verify the reported `analysis_candidates` are members of the cycle's source candidates before trusting the cohort.

## Verification Checklist

- [ ] Mode is explicitly paper/simulated.
- [ ] Run is bounded and tracked.
- [ ] DB integrity is `ok`.
- [ ] No duplicate runner exists.
- [ ] Candidate rotation is measured.
- [ ] P&L fields distinguish realized, unrealized, fees, and unavailable data.
- [ ] Every editable config field is proven enforced.
- [ ] No live wallet or signer path was invoked.
