---
name: chaos-risk-engine
description: "Use when the agent must calculate or critique crypto trade risk: position size, max loss, stop distance, R:R, scaling, liquidity, drawdown, correlation, or kill switches. Converts conviction into constrained exposure before any action label."
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, crypto, risk, sizing, trading]
    related_skills: [chaos-crypto-trader, chaos-alpha-decoder]
---

# Chaos Risk Engine

Risk is the first language. This skill turns a trade idea into bounded exposure by defining max loss, invalidation, sizing, liquidity constraints, and kill switches before upside is considered.

## Prerequisites

- The package is installed: `pip install chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

No setup is actionable until the cost is known.

## When to Use

Use when the user asks for:

- position sizing;
- stop-loss or invalidation placement;
- R:R evaluation;
- scale-in / scale-out logic;
- max loss or portfolio risk;
- liquidity/exit risk;
- drawdown control;
- trade-plan critique;
- paper-trade rules;
- kill switch design.

Use also whenever a trade plan reaches `plan` or `act`.

## Required Inputs

If missing, state assumptions rather than fabricating certainty:

| Input | Required for |
|---|---|
| Account or sleeve size | position sizing |
| Max risk per trade | max loss |
| Entry price/zone | sizing and R:R |
| Invalidation/stop | sizing and kill switch |
| Targets | R:R |
| Liquidity/volume | exit feasibility |
| Timeframe | stop tolerance and size |
| Correlated exposure | portfolio risk |

If account size is unknown, express size as `R` units or percentage risk, not dollars.

## Sizing Formula

Base formula:

```text
position_size = max_loss / abs(entry - invalidation)
```

For spot tokens:

```text
token_qty = max_loss_usd / stop_distance_usd
notional = token_qty * entry_price
```

For percent distance:

```text
notional = max_loss_usd / stop_distance_pct
```

For no account size:

```text
1R = chosen max loss unit
notional = 1R / stop_distance_pct
```

Always include slippage and liquidity notes for small caps.

## Risk Classes

| Class | Use | Default max exposure |
|---|---|---|
| Study | thesis incomplete | 0R |
| Watch | evidence forming | 0R until trigger |
| Plan | valid setup, no trigger yet | define 0.25R-1R plan |
| Act | trigger hit and risk known | use defined R only |
| Hedge | reduce or offset exposure | size from existing risk |
| Avoid | edge poor or risk unbounded | 0R |

Never recommend full sizing for illiquid launches, fresh wallets, or late-consensus social moves.

## Liquidity and Exit Checks

Before action, answer:

- What is realistic entry slippage?
- What is realistic exit slippage under stress?
- Is liquidity concentrated or removable?
- Are early holders/dev wallets able to dump into the trade?
- Is the target notional small enough versus volume/liquidity?
- Can the thesis be exited before social consensus unwinds?

If exit is uncertain, downsize or avoid.

## Kill Switches

A kill switch is not a vibe. It is an exact condition.

Examples:

- price closes below invalidation level on the chosen timeframe;
- dev/early wallet sends material supply to exchange/pool;
- liquidity drops below required exit threshold;
- funding/OI flips against thesis while spot fails to confirm;
- catalyst window passes with no flow;
- source claim is contradicted by onchain data.

## Control Status

Every risk statement must be labeled by implementation status:

| Status | Meaning |
|---|---|
| analytical | calculated or recommended only |
| paper-enforced | applied by the paper runner and covered by tests |
| live-declared | present in policy/config but not technically enforced |
| live-enforced | persisted and enforced in code by a live executor with tests; chaos-trader has no executor, so nothing in it is live-enforced |

Never describe `live-declared` policy prose as a guardrail. For live execution, verify code paths for daily loss, trade count, open positions, cooldown, slippage, fees, balance changes, and kill switches before calling them controls.

## Output Contract

```markdown
## Risk
| Item | Value / Rule |
|---|---|
| Risk unit | |
| Entry assumption | |
| Invalidation | |
| Stop distance | |
| Max loss | |
| Position size | |
| Target R:R | |
| Liquidity cap | |
| Scale rule | |
| Kill switch | |

## Risk Verdict
0R / watch / reduced size / normal size / hedge / avoid.
```

## Common Pitfalls

1. **Conviction sizing.** Size follows stop distance, liquidity, confidence, and portfolio context, not excitement.
2. **Moving invalidation.** If invalidation moves after entry to protect ego, the plan failed.
3. **Ignoring slippage.** Thin tokens make theoretical R:R fake.
4. **Correlation blindness.** SOL beta, memecoin beta, and narrative clusters can make several trades one trade.
5. **No timeframe.** A scalp stop and swing invalidation are different creatures.
6. **Undefined max loss.** Without max loss, there is no plan.

## Verification Checklist

- [ ] Max loss is explicit or represented as R.
- [ ] Entry and invalidation are stated.
- [ ] Position sizing formula is shown when sizing is requested.
- [ ] Liquidity and slippage are considered.
- [ ] Correlated exposure is noted when relevant.
- [ ] Kill switch is exact.
- [ ] Risk verdict constrains the action label.
