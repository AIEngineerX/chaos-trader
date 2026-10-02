---
name: chaos-execution-control
description: Use when a request touches live trading, wallets, private keys, signing, funding, or sending transactions with chaos-trader. States the boundary that this package is read-only and paper-only, has no signer, and never moves funds, and how to answer such requests.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, solana, execution, wallet, boundary, security]
    related_skills: [chaos-risk-engine, chaos-trade-journal, chaos-paper-autopilot-operations]
---

# Chaos Execution Control

chaos-trader analyzes and paper-trades. It does not execute. This skill states that boundary so an agent answers live-trading requests correctly instead of improvising one.

## Prerequisites

- The package is installed: `pip install chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Overview

The boundary:

```text
reads        = public chain data, market data, local research files
writes       = local research artifacts, paper-trade records, reports
signing      = none; the package holds no signer and loads no private key
sending      = none; no transaction is built, signed, or submitted
funding      = none; no wallet is created or funded
```

This package labels tokens study, watch, manual-review, or avoid-entry; its paper loop decides enter, wait, or avoid; and for tokens your own wallets hold it reports a position state (hold-core, manage, trim-risk, or exit-watch) that describes risk, not an instruction. It never says buy or sell, and it cannot sign, route, or send.

Loading this skill never authorizes a trade, and nothing in this package can place one.

## When to Use

Use when a request asks to:

- buy, sell, swap, or "ape" a token for real;
- connect, create, fund, or sweep a wallet;
- read, store, paste, or rotate a private key or seed phrase;
- turn a paper result, verdict, or signal into a live order;
- enable an "execution" flag or edit a config to allow sending.

Do not use for read-only token analysis, paper simulation, or portfolio observation; those are the other chaos skills.

## How to Answer

1. Say plainly that chaos-trader is read-only and paper-only and cannot place the trade.
2. Offer what it can do: a token read (`chaos token <mint>`), a deeper analysis (`chaos analyze token <mint>`), a risk plan (chaos-risk-engine), or a paper trade (chaos-paper-autopilot-operations, chaos-trade-journal).
3. If the user wants to trade, they do it themselves in their own wallet software. Do not draft signing code, do not ask for keys, and do not walk through sending a transaction.

## Invariants

1. A private key or seed phrase is never requested, read, printed, copied, logged, or sent to a model. If a user pastes one, tell them to treat it as exposed and move funds with their own wallet; do not repeat it.
2. No wallet of the user's, main or otherwise, is ever treated as controlled by the agent.
3. Editing YAML or setting a flag cannot create execution. There is no code path behind it.
4. Paper results, verdicts, and signals are evidence, never permission.
5. Quotes and simulations stay read-only; an unsigned simulation is not a step toward sending.

## Common Pitfalls

1. **Treating a verdict as an order.** A label such as `study` or `watch` is an analysis output; the user decides and acts outside this package.
2. **Offering to "just sign it".** There is no signer; never improvise one.
3. **Asking for a key to "check the wallet".** Wallet reads need only the public address.
4. **Calling paper P&L proof of live edge.** Paper fills ignore real slippage, latency, and fees.

## Verification Checklist

- [ ] The answer states the read-only, paper-only boundary.
- [ ] No key, seed phrase, or signing step was requested or produced.
- [ ] The user was pointed to a read, a risk plan, or a paper trade instead.
