---
name: chaos-alpha-decoder
description: Use when the agent must turn noisy crypto fragments from wallets, X, chat groups, market data, launch data, or news into a structured alpha read. Forces raw signal through claim, mechanism, evidence, timing, edge, risk, and action before any trade-plan output.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, crypto, alpha, solana, trading, risk]
    related_skills: [chaos-crypto-trader, chaos-risk-engine]
---

# Chaos Alpha Decoder

Raw crypto signal is not alpha. This skill forces the agent to translate each fragment into a falsifiable market claim before it can influence a trade plan.

## Prerequisites

- The package is installed: `pip install chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

Core transform:

```text
Raw signal → Claim → Mechanism → Evidence → Timing → Edge → Risk → Action
```

Alpha is the gap between what is happening and what the market has not priced yet. If that gap cannot be named, the correct action is study, watch, avoid, or archive.

## When to Use

Use when the user provides or asks about:

- a wallet buy/sell/transfer;
- a token mint, launch, pump.fun link, deployer, or holder change;
- an X/Twitter post, ticker trend, chat-group rumor, or KOL call;
- a market-data anomaly such as volume, funding, OI, or liquidity movement;
- a candidate narrative, sector rotation, or early rumor;
- any "is this alpha?" question.

Do not use this skill to place trades, sign transactions, copy wallets blindly, or convert one signal into conviction.

## Alpha Read Contract

For every candidate signal, produce this structure:

| Layer | Required answer |
|---|---|
| Raw signal | What exactly happened? |
| Actor | Who acted first, and why might they matter? |
| Claim | What is the signal implying? |
| Mechanism | How could it create price, liquidity, positioning, or attention flow? |
| Evidence | What confirms it? What contradicts it? |
| Timing | Pre-social, early-social, consensus, late, or dead? |
| Edge | What is not priced yet? |
| Liquidity | Can the user enter and exit without becoming the mark? |
| Risk | What kills the thesis? |
| Action | study / watch / plan / act / avoid / archive |

If any of claim, mechanism, evidence, timing, or risk is missing, do not output `act`.

## Source Class Discipline

| Source | Use | Failure mode |
|---|---|---|
| Wallets | Flow, accumulation, rotations, funder/deployer links | False smart-money labels; late copytrading |
| Launch/token data | Holders, liquidity, authority flags, bundles/snipers | Spam volume mistaken for demand |
| X/Twitter | Attention velocity and narrative emergence | Engagement bait, paid calls, recycled theses |
| Chat groups | Rumor velocity and coordination hints | Insider bait, unsourced screenshots, scams |
| Market data | Confirmation, liquidity, funding, OI, relative strength | Lagging confirmation without causal edge |
| Docs/repos/news | Catalyst basis | Slow to price unless paired with attention/flow |

Use social signals as prompts for investigation, not proof.

## Wallet Classification

Never write "smart money bought" without classification.

Classify actor first:

- early discoverer;
- repeat profitable trencher;
- KOL wallet;
- dev/deployer wallet;
- bundler/sniper;
- CEX/onramp funder;
- liquidity/market-maker actor;
- unknown.

Then classify the signal:

- first buy;
- scale-in;
- rotation;
- exit;
- funding;
- deploy;
- rebuy;
- wash/noise.

Useful compressed output:

```markdown
## Alpha Decode
- Wallet class:
- Historical reliability:
- Signal type:
- Market state:
- Claim:
- Mechanism:
- Evidence for:
- Evidence against:
- Edge:
- Risk:
- Action:
```

## Timing Labels

| Label | Meaning | Default posture |
|---|---|---|
| Pre-social | Onchain/deployer/funder movement before broad chatter | Study/watch; act only with liquidity and invalidation |
| Early-social | First credible social mentions with thin consensus | Plan or small risk unit if evidence aligns |
| Consensus | Broad ticker spam, obvious trend | Wait for pullback/structure; avoid chasing |
| Late | Euphoric, recycled posts, exit-liquidity risk | Avoid or hedge |
| Dead | Thesis failed, liquidity gone, narrative stale | Archive |

## X Evidence Receipt

When X is used as catalyst evidence, record a compact receipt:

```text
contract/mint searched
symbol ambiguity resolved
primary post URLs
claim
mechanism
timestamp and lookback window
direct proof vs inference
confidence
contradictions
```

A ticker match alone is not catalyst proof. Resolve the mint/contract first. For tokenized-agent or AI-agent narratives, distinguish:

- the token funds or governs a real agent;
- the token merely uses agent branding;
- the agent produces verifiable activity;
- the claimed activity has a plausible value-accrual mechanism for the token.

If direct primary evidence is absent, label the catalyst `unverified` and do not upgrade the action label.

## Common Pitfalls

1. **Raw signal as conclusion.** A wallet buy, KOL post, or trending ticker is only input.
2. **Unclassified actors.** Unknown wallets stay unknown until history, funding, and behavior prove otherwise.
3. **Social truth laundering.** Screenshots and chat-group claims need independent confirmation.
4. **Ignoring liquidity.** An accurate thesis is useless if entry or exit makes the user the mark.
5. **Late conviction.** If everyone can see it, the edge may already be gone.
6. **No invalidation.** No kill condition means no trade plan.

## Verification Checklist

- [ ] Raw signal is stated without embellishment.
- [ ] Actor/source quality is classified.
- [ ] Claim and mechanism are explicit.
- [ ] Evidence for and against are separated.
- [ ] Timing label is assigned.
- [ ] Edge is a real mispricing or information delta.
- [ ] Liquidity and exit path are addressed.
- [ ] Risk and invalidation are defined.
- [ ] Action is one of study/watch/plan/act/avoid/archive.
