---
name: chaos-crypto-trader
description: Use when someone asks what this package's read says about a Solana token or what its paper loop would do with it, such as "what does the read say about this token" or "what would the paper loop do with this token". It returns the verdict card from `chaos token` (gate state, evidence, risk flags, invalidation) and, when asked, the paper plan that `chaos strategy-paper` computes (decision, simulated size, stop, two targets, time stop, required trigger, invalidation), labelled as the paper loop's plan. It never gives a live order or a plan for a real trade.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, crypto, trading, solana, pumpfun]
    related_skills: [chaos-alpha-decoder, chaos-risk-engine, chaos-trade-journal]
---

# Chaos Crypto Trader

This skill answers two questions about a token: what the package's read says, and what its paper loop would do with it. It governs the source hierarchy, the research labels, the paper loop's authority, and the hard live-money boundary. It is not a live execution bot, and it does not plan real trades.

## Prerequisites

- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

Correct architecture:

```text
Sources → Normalizers → Signal modules → Risk engine → Verdict card → Paper plan → Journal/feedback
```

Incorrect architecture:

```text
X hype → ticker mention → trade
```

## When to Use

Use when the user asks:

- what the read says about a token mint, launch, pump.fun token, or its deployer and early buyers;
- what the paper loop would do with a token, and why;
- how X/Twitter narratives, ticker velocity, or KOL claims bear on a token's read;
- about a watchlist candidate, a risk read, or a postmortem of a paper position.

Do not use this skill for live execution or to plan a real trade. The agent may label tokens and run policy-authorized simulated positions. A real position is the user's own decision, and the paper plan is not a plan for it.

## Authority Boundary

This package labels tokens study, watch, manual-review, or avoid-entry; its paper loop decides enter, wait, or avoid; and for tokens your own wallets hold it reports a position state (hold-core, manage, trim-risk, or exit-watch) that describes risk, not an instruction. It never says buy or sell, and it cannot sign, route, or send.

Allowed:

- read-only research;
- research labels: `study`, `watch`, `manual-review`, and `avoid-entry`, plus the caution gate states `study-caution` and `exit-liquidity-watch`;
- the paper plan as the paper engine computes it, labelled as the paper loop's plan;
- deterministic paper entries, management, trims, and exits when configured policy authorizes them;
- wallet/token reads from approved read-only sources;
- market-structure analysis;
- journaling and postmortems;
- proposals for radars or integrations.

Blocked without explicit approval:

- live orders;
- wallet signing;
- fund movement;
- broker/exchange operation;
- wallet connectors, deeplinks, or wallet adapters;
- exchange API secret keys or order endpoints;
- pump.fun create/buy/sell;
- Jupiter swaps;
- unattended trading bots;
- public posting or signal distribution;
- scheduled jobs that act on funds.

If a request implies live execution or a plan for a real trade, say that this package cannot place orders and that its plans are paper plans, then offer the read and the paper loop's plan.

## Source Hierarchy

Prefer structured, native, read-only sources:

1. Helius RPC/API for Solana wallet/token/transaction history, when a Helius key is configured.
2. Solana RPC and verified program/account data.
3. pump.fun read-only program/event/bonding-curve data.
4. X/Twitter search for attention-flow signals.
5. Market data APIs, DEX aggregators, and price/liquidity sources.
6. Web/news/docs as secondary context.

When sources conflict, state the conflict. Prefer onchain/exchange data over social claims.

For a Solana mint, start the evidence with `chaos token <mint>`, or `chaos analyze token <mint>` for a deeper read. For the paper loop's plan, run `chaos strategy-paper <mint>`; add `--raw` to see the stop, targets, and time stop. All three are read-only and resolve `CHAOS_HOME` themselves.

## The Paper Loop's Plan

`chaos strategy-paper <mint>` runs the same read and hands it to the paper engine, `strategy_paper_engine.py`. Every field below is a simulation setting for the paper book, not a level for a real position.

| Field | What the engine computes |
|---|---|
| `decision` | `paper_enter`, `paper_wait`, or `paper_avoid`. Any blocker gives `paper_avoid`. `paper_enter` needs a passing gate (`watch` or `manual-review`) plus a watch-wallet hit, a medium-or-better catalyst, or sourced X evidence |
| `entry_action` | The gate state the decision started from |
| `paper_plan.simulated_notional_usd` | The paper size: the smallest of `--base-risk-usd` (100), `--max-notional-usd` (250), and `--liquidity-bps` (50, which is 0.5%) of pool liquidity |
| `paper_plan.stop_pct` | -25, or -30 in low-cap trench mode |
| `paper_plan.tp1_pct` and `paper_plan.tp2_pct` | 75 and 200 |
| `paper_plan.time_stop_minutes` | 25, or 15 in low-cap trench mode |
| `paper_plan.required_trigger` | What must change before a wait can become an entry |
| `paper_plan.invalidation` | The conditions that end the paper idea |
| `blockers`, `warnings`, `reasons` | Why the decision came out as it did |

The plan carries no entry level of its own. `market.price_usd` is the price the read saw, and the stop and targets are percentages from the paper entry.

## Default Analysis Frame

For token, launch, wallet, narrative, or paper-loop questions, answer through this frame:

1. **Gate** — the card's `ENTRY GATE:` state, and what must be true before the token deserves attention.
2. **Regime** — BTC/SOL/macro/liquidity/volatility context.
3. **Onchain** — wallet/token/holder/funding/deployer evidence when relevant.
4. **Market Structure** — trend, levels, liquidity when relevant.
5. **Narrative** — social/news/catalyst quality when relevant.
6. **Read** — what the evidence supports: improving, deteriorating, or unclear structure.
7. **Paper Plan** — when asked, the decision, simulated size, stop, targets, time stop, and required trigger from `chaos strategy-paper`, labelled as the paper loop's plan.
8. **Cost / Curse** — what can go wrong first.
9. **Boon** — what would confirm the read.
10. **Kill Switch** — the exact condition that invalidates the idea.
11. **Answer** — one research label: `study`, `watch`, `manual-review`, or `avoid-entry`. If relevant, add a separate simulated paper action such as `paper_enter`, `paper_wait`, `paper_avoid`, `paper_trim`, `paper_exit`, or `paper_expire`.

For fast questions, compress the frame but keep invalidation and risk.

## Read Contract

```markdown
## Read
- Gate:
- Confidence:
- Timeframe:
- Regime:

## Evidence
- Onchain:
- Market structure:
- Narrative:

## Paper loop's plan (simulated)
| Field | Value |
|---|---|
| Decision | |
| Simulated size | |
| Stop | |
| Target 1 | |
| Target 2 | |
| Time stop | |
| Required trigger | |
| Invalidation | |

## Curse
What must be endured or what can go wrong first.

## Boon
What would confirm the read.

## Kill Switch
One sentence.

## Answer
Study / watch / manual-review / avoid-entry. Label any paper action separately as simulated.
```

## Domain Postures

### Solana / Helius

Helius is the primary Solana data rail once a Helius key is configured. With a key, prefer `getTransactionsForAddress` (Helius-only) and `getTransaction`; use deprecated Enhanced Transactions only as a disclosed fallback. On a standard Solana RPC, use `getSignaturesForAddress` plus `getTransaction` and say the history is sampled. Never print API keys.

### pump.fun

Treat every launch as adversarial until structure improves. Check dev wallet history, early buyers, concentration, funding path, bonding curve progress, sell pressure, repeated deployers, liquidity, and exit path. Never expose buy/sell/create flows during research stages.

### X / Twitter

Use X as attention-flow data. Separate fresh primary-source catalysts from recycled ticker spam and paid engagement.

## Common Pitfalls

1. **Ticker-only answers.** A ticker is not a thesis.
2. **Social conviction.** Narrative velocity is not evidence until paired with mechanism and confirming data.
3. **No exit path.** Illiquid upside without exit rules is a trap.
4. **No invalidation.** If wrongness cannot be named, say so and keep the label at `study`.
5. **Authority confusion.** Read-only source access must not suppress labels or paper actions, and neither may become live execution wiring.
6. **Paper plan as a real plan.** The size, stop, and targets are the paper engine's settings for the paper book. Never present them as levels for a real position.
7. **Raw dumps.** Summarize findings into claims about the read; do not paste API noise.

## Verification Checklist

- [ ] Source access is read-only and live execution remains unavailable.
- [ ] Research label is stated; paper action is labeled simulated.
- [ ] Source hierarchy followed or limitations stated.
- [ ] Claim, mechanism, and evidence are separated.
- [ ] Invalidation and kill switch are explicit.
- [ ] Any size, stop, or target shown comes from the paper engine and is labelled as the paper loop's plan.
- [ ] Output ends with exactly one research label: study/watch/manual-review/avoid-entry.
- [ ] Any paper action is clearly labeled simulated and separate from live execution.
