---
name: chaos-smart-money-signals
description: Use when the agent should pull live smart-money wallet-cluster signals (N tracked wallets accumulating the same token in a short window) from an external signal API, or inspect the tracked wallet universe. A cluster hit is evidence to investigate, never a verdict or an instruction to act.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, crypto, alpha, solana, smart-money, signals, wallet-cluster]
    related_skills: [chaos-alpha-decoder, chaos-wallet-attribution, chaos-position-aware-alpha, chaos-crypto-trader]
---

# Chaos Smart-Money Signals

Note: this skill needs an external signal API (`SMART_MONEY_API_BASE`); no public provider exists today, so it is inactive without one.

## Prerequisites

- The package is installed: `pip install chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

The external signal API is a read-only service that watches a curated universe of proven
Solana wallets and emits a **cluster signal** when several of them accumulate the same
token inside a short window. Each signal is tier-weighted: a cluster of tier-A wallets
carries far more weight than the same count of tier-C wallets. This skill pulls those
signals and the wallet universe into chaos-trader over an authenticated GET-only feed.

It is one rail among several. A cluster hit is the *start* of a read, not the end of one.

## When to Use

- The user asks "what is smart money accumulating right now?" or "any live clusters?".
- You want to see which tracked wallets (and tiers) are behind a token before a deeper read.
- You need a candidate list to push into the deeper Helius/DexScreener pipeline.

Do **not** use it to justify an action on its own. A cluster may support a read label
(study, watch, manual-review, avoid-entry) or a paper decision (enter, wait, avoid) only
after independent confirmation and policy gates.

## Signal API keys

Set in the home's `.env` (never committed):

- `SMART_MONEY_API_BASE` — the external signal API base URL (https only).
- `SMART_MONEY_API_TOKEN` — the read bearer token.

If either is missing the client refuses to run and tells you which key to add.

## Quick Reference

Run the client with `chaos run smart_money_signal_client`. On Windows, `python -m chaos_trader.cli` works in place of `chaos`.

| Command | Purpose |
|---|---|
| `chaos run smart_money_signal_client signals --limit 20` | Live cluster signals (markdown card) |
| `chaos run smart_money_signal_client signals --raw` | Same, raw JSON for downstream parsing |
| `chaos run smart_money_signal_client wallets --tier A` | The tracked universe, filtered to a tier |
| `chaos smart-signals` / `chaos smart-signals --wallets` | Compact card via the `chaos` CLI |

## Procedure

1. Pull the feed (`signals`). Note each hit's `distinct_wallets`, `weighted_score`, and
   `tier_breakdown` (A/B/C). A high weighted score driven by tier-A wallets is a stronger
   lead than a larger count of tier-C wallets.
2. **Treat the hit as a claim, not a conclusion.** Hand every candidate to
   `chaos-alpha-decoder` and run its Claim → Mechanism → Evidence → Timing → Edge → Risk →
   Action contract. The cluster is the *Evidence* input for one rail only.
3. Grade the wallets with `chaos-wallet-attribution`: are these independent smart-money
   entries, or one funder/bundle wearing many addresses? A cluster of sybil-linked wallets
   is not real consensus.
4. Confirm on a second rail before it can move any label: Helius holder/flow read
   (`chaos token <mint>` / `chaos analyze token <mint>`), DexScreener liquidity. Per `chaos-position-aware-alpha`, a raw
   cluster may only inform an `entry_gate`/`position_action` label *after* this
   confirmation — never before.
5. If the user wants outcome tracking, log the signal through the calibration path (see
   `chaos-trade-journal`) so its realized win/loss is measured, not assumed.

## The honesty rule

Check how the provider computes its headline "hit rate". A **peak-touch** metric counts a token as a hit
if it ever touched a multiple of its call price, even if it later round-tripped to zero.
Entering after a cluster means entering *after* smart money already has — your realized
outcome is worse than the call price. Always frame a cluster as "smart money already
accumulated," never as "this will go up."

## Common Pitfalls

- Presenting a cluster as a recommendation. It is evidence for a read, nothing more.
- Ranking by raw wallet count instead of `weighted_score` / tier mix.
- Trusting a cluster of wallets that share a funder (run wallet-attribution first).
- Confusing peak-touch hit rate with realized return.

## Verification Checklist

- [ ] Pulled signals and read `weighted_score` + `tier_breakdown`, not just the count.
- [ ] Ran the candidate through `chaos-alpha-decoder` (cluster = one Evidence rail).
- [ ] Graded wallet independence with `chaos-wallet-attribution`.
- [ ] Confirmed on a second rail (Helius/DexScreener) before any label moved.
- [ ] Framed the read as "already accumulated," with the peak-touch caveat stated.
- [ ] Never treated the cluster itself as a trade instruction or live-execution trigger.
- [ ] Any read label was independently confirmed; any paper action came from deterministic policy.
