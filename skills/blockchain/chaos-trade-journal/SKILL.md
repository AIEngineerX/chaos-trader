---
name: chaos-trade-journal
description: "Use when the agent needs to create, update, or review local crypto trade theses, watchlists, paper trades, or postmortems. Keeps trade artifacts in files under the chaos home and prevents stale levels, one-off wallet claims, or temporary trades from entering durable memory."
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, crypto, journal, paper-trading, postmortem]
    related_skills: [chaos-crypto-trader, chaos-risk-engine]
---

# Chaos Trade Journal

Trading memory belongs in artifacts, not durable agent memory. This skill governs how the agent records theses, watchlist ideas, paper trades, and postmortems under files in the chaos home so they can be reviewed without polluting long-term memory with stale levels.

## Prerequisites

- The package is installed: `pip install chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

Default root, under `CHAOS_HOME`:

```text
trading/
├── watchlists/
├── journals/
├── postmortems/
└── reports/
```

## When to Use

Use when the user asks to:

- save a trade idea;
- create a watchlist;
- start a paper trade;
- record an entry/exit thesis;
- postmortem a win/loss/miss;
- review recurring mistakes;
- build a local trading report.

Do not save one-off token levels, wallet observations, current positions, or temporary signals to durable memory. Use files.

## Artifact Rules

1. Keep files under the chaos home unless the user specifies another workspace.
2. Use dated filenames: `YYYY-MM-DD_slug.md`.
3. Include source links or commands when available.
4. Separate plan from result.
5. Mark whether execution was real, paper, or hypothetical.
6. Record invalidation before outcome.
7. Never include private keys, seed phrases, exchange secrets, or wallet credentials.

## Thesis Template

```markdown
# Trade Thesis — <asset/ticker>

Date: YYYY-MM-DD
Mode: real / paper / hypothetical
Timeframe:
Source prompt:

## Read
- Bias:
- Confidence:
- Regime:
- Source quality:

## Evidence
- Onchain:
- Market structure:
- Narrative:
- Contradictions:

## Plan
| Item | Level / Rule |
|---|---|
| Entry trigger | |
| Invalidation | |
| Target 1 | |
| Target 2 | |
| Max risk | |
| Size logic | |
| Liquidity cap | |

## Curse

## Boon

## Kill Switch

## Decision
act / wait / avoid / hedge / study
```

## Postmortem Template

```markdown
# Postmortem — <asset/ticker>

Date opened:
Date closed/reviewed:
Mode: real / paper / hypothetical
Result: win / loss / scratch / missed / avoided
R multiple:

## Original Thesis

## What Happened

## What Was Right

## What Was Wrong

## Error Type
- thesis wrong
- timing wrong
- size wrong
- invalidation ignored
- liquidity/exit wrong
- social signal late
- onchain read wrong
- execution mistake
- good avoid

## Rule Change
One concrete rule to test next time.
```

## Review Loop

For weekly/monthly reviews, classify mistakes by error type rather than storytelling:

| Error Type | Question |
|---|---|
| Thesis wrong | Was the claim/mechanism false? |
| Timing wrong | Was the read early, late, or invalidated by regime? |
| Size wrong | Was risk too large for source quality/liquidity? |
| Invalidation ignored | Was the kill switch moved or violated? |
| Liquidity wrong | Was entry/exit unrealistic? |
| Social late | Was the signal consensus rather than edge? |
| Onchain wrong | Was wallet/deployer/flow classification wrong? |
| Good avoid | Did risk discipline protect capital? |

## Cohort Calibration

Paper/autopilot reviews must track decisions that were not entered, not only completed trades.

For each fixed-rule cohort report:

```text
entered winners
entered losers
avoided winners
avoided losers
wait-to-winner misses
wait-to-loser saves
unknown/unmarked outcomes
source precision
catalyst precision
```

Do not change strategy rules until the cohort has enough marked outcomes to identify a specific failure mode. Change one rule class at a time and record the before/after cohort IDs.

## Durable Memory Boundary

Allowed memory:

- stable trading preferences the user states, such as default timeframe or max risk convention;
- durable process rules that remain useful in 30+ days.

Forbidden memory:

- open positions;
- token levels;
- wallet one-offs;
- current watchlist items;
- trade outcomes;
- PnL snapshots;
- source claims that may stale.

## Common Pitfalls

1. **Saving stale data to memory.** Trade data rots. Put it in files.
2. **Outcome-only reviews.** A profitable bad process is still a fault.
3. **No source trail.** Future reviews need evidence, not vibes.
4. **Mixing real and paper trades.** Label mode clearly.
5. **Rule sprawl.** Every postmortem gets at most one rule change unless the user asks for deeper review.

## Verification Checklist

- [ ] Artifact path is under `trading/` in `CHAOS_HOME` unless specified.
- [ ] File has date, mode, timeframe, and source prompt.
- [ ] Original plan includes invalidation and max risk.
- [ ] Result/postmortem is separated from thesis.
- [ ] No secrets or credentials are present.
- [ ] Temporary trade data was not saved to durable memory.
