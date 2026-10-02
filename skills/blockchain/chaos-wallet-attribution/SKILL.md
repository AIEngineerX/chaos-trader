---
name: chaos-wallet-attribution
description: Use when classifying Solana wallet ownership, funding, transfers, buys, sells, burns, sweepers, or mistaken-transfer claims. Produces evidence-ranked attribution without treating a transfer as a trade or exposing which wallets belong to the user.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, solana, wallet-attribution, transfers, provenance]
    related_skills: [chaos-alpha-decoder, chaos-position-aware-alpha, solana]
---

# Chaos Wallet Attribution

Classify what a wallet did and how strongly ownership or intent can be inferred. A token balance change is not automatically a buy, sell, endorsement, or position.

## Prerequisites

- The package is installed: `pip install chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Tools

Executable read-only tools for this work. Run them with the `chaos` verbs below; a script without its own verb runs with `chaos run <name>`.

- `chaos wallets` — smart-wallet discovery status plus the queue of never-scored wallets found via funding/transfer edges; `chaos wallets --discover N` enriches up to N of them (Helius key needed for the enrichment).
- `chaos run smart_wallet_tracker <wallet>` — **Helius-only** (needs `HELIUS_API_KEY`): transfer-aware enrichment through the Helius Wallet API; `--discovered N` enriches never-scored edge-wallets; `--top-db N` refreshes ranked wallets.
- `chaos run smart_wallet_promoter --raw --write` — multi-source promotion verdicts and the discovery queue, written to `trading/alpha/smart_wallet_promotions.json` under the home.
- `chaos run wallet_scan <wallet>` / `chaos run wallet_deep <wallet>` — **Helius-only** (they call `getTransactionsForAddress` and the Helius Wallet API, so a standard Solana RPC fails): focused raw reads for attribution evidence.

Without a Helius key, use `chaos token <mint>` for holder evidence and the `solana` skill for single wallet balances and transactions.

## Overview

Keep the user's main-wallet identity private. Use labels such as `user wallet`, `isolated risk wallet`, `funding wallet`, or `unknown` unless the user explicitly authorizes disclosure.

## When to Use

Use for:

- mistaken SOL or token transfer claims;
- wallet funding provenance;
- buy versus transfer classification;
- burner, sweeper, distributor, or deployer relationships;
- holder-quality analysis;
- deciding whether an observed wallet is relevant to the user's positions.

## Evidence Ladder

Rank each claim:

| Grade | Evidence |
|---|---|
| A | Signed statement or known ownership mapping plus matching on-chain transaction |
| B | Repeated direct funding/control pattern with timing and transaction evidence |
| C | Strong behavioral linkage but no proof of common control |
| D | Weak heuristic, shared counterparty, or one-off coincidence |
| Unknown | Insufficient or contradictory evidence |

Never upgrade a grade because a narrative is convenient.

## Transaction Classification

For each relevant transaction determine:

```text
signature
timestamp
source wallet
destination wallet
mint/native SOL
amount
fee payer
programs/instructions
token-account creation/closure
swap route if any
balance deltas
classification
confidence
```

Allowed classifications:

- `swap_buy`;
- `swap_sell`;
- `direct_transfer`;
- `funding_transfer`;
- `distribution`;
- `airdrop`;
- `burn`;
- `liquidity_action`;
- `account_close_or_rent_recovery`;
- `unknown`.

A direct transfer without swap instructions is not a buy or sell.

## Mistaken-Transfer Validation

1. Verify the exact signature and chain.
2. Confirm source, destination, mint/SOL amount, and timestamp.
3. Decode instructions, not only balance deltas.
4. Check whether destination is a token account, system account, program, exchange, or known owned wallet.
5. Check follow-on movement without assuming control.
6. Compare against the claimed intended destination only if the user supplies it.
7. Report recoverability separately from attribution.

Completion criterion: the report states what happened on-chain, what remains unproven, and whether any recovery action exists.

## Wallet Relationship Heuristics

Useful but non-conclusive signals:

- repeated funding from the same source;
- synchronized creation and transaction timing;
- identical transaction construction and fee behavior;
- shared withdrawal source;
- repeated sweep destination;
- unique token-account patterns;
- recurring counterparties.

Weak signals:

- same popular exchange;
- same router;
- same token;
- similar transaction time during high-volume events;
- one shared counterparty.

## Holder and Position Semantics

Distinguish:

```text
holds token
received token
bought token
sold token
funded position
controls wallet
beneficial owner
```

These are separate claims. If only `received token` is proven, do not describe the wallet as a buyer or holder conviction signal.

## Privacy Boundary

- Never print private keys, seed phrases, `.env` values, or keypair paths.
- Do not publish the user's wallet mapping.
- Do not send the user's wallet identifiers to third-party services unless necessary and approved.
- Prefer local/public-chain analysis and redact identity labels in reports.
- A wallet the user labels as a separate risk wallet must not be conflated with the user's main wallet.

## Output Contract

```text
Finding:
Classification:
Evidence grade:
On-chain facts:
Inference:
Contradictions:
Unknowns:
Privacy treatment:
Recommended next check:
```

Use exact signatures and public addresses only when needed for the user to verify. Public output should minimize identity linkage.

## Common Pitfalls

1. **Transfer equals purchase.** Decode swap instructions.
2. **Funding equals ownership.** It is evidence, not proof.
3. **Balance equals current position.** Account closures, burns, and distributions matter.
4. **One transaction proves a cluster.** Require repeated linkage.
5. **Attribution leaks identity.** Use role labels and bounded disclosure.
6. **Confidence omitted.** Every inference needs an evidence grade.

## Verification Checklist

- [ ] Signature and chain are verified.
- [ ] Instructions and balance deltas agree.
- [ ] Transfer, buy, sell, burn, and funding semantics are separated.
- [ ] Ownership/control claims have an evidence grade.
- [ ] Contradictory evidence is surfaced.
- [ ] Which wallets belong to the user remains private.
- [ ] Unknown states remain unknown.
