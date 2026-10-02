---
name: chaos-solana-intelligence
description: Use when the agent needs a Solana token, wallet, transaction, launch, or market read. Routes evidence through existing Helius-first pipeline contracts and optional GMGN cross-checks.
version: 1.0.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, solana, helius, token, wallet, transaction, pump, intelligence]
    related_skills: [solana, gmgn-alpha-intelligence, chaos-wallet-attribution, chaos-smart-money-signals, chaos-alpha-decoder]
---

# Chaos Solana Intelligence

This is the index for chaos-trader's existing Solana intelligence. It does not
create a second wallet, token, scoring, or execution stack.

## Prerequisites

- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Purpose

Use the narrowest existing rail that answers the question, preserve provenance, and
hand conclusions to chaos-trader's current classification/risk contracts.

## Authority Order

1. **Helius / Solana RPC:** transaction, signer, account deltas, balances, holder
   ownership, mint/freeze authority.
2. **Direct protocol decode:** Pump/PumpSwap lifecycle and program state.
3. **DexScreener / market resolver:** executable pair, price, liquidity, market cap.
4. **External signal API:** wallet-cluster claim requiring onchain confirmation.
5. **GMGN:** secondary labels, trends, flow, and wallet-performance estimates.
6. **X, chat groups, and other social:** attention and narrative evidence, never onchain proof.

Conflicts resolve upward. Preserve the disagreement instead of silently choosing the
more bullish value.

## Fast Routing

| Request | Primary route |
|---|---|
| token/CA read | `chaos token <MINT>` |
| deep token read | `chaos analyze token <MINT>` |
| live trend sweep | `chaos sweep` |
| wallet attribution | `chaos-wallet-attribution` + its wallet scripts (most need a Helius key) |
| transaction/signature | `chaos run transaction_lookup <SIGNATURE>` / `solana` skill transaction read |
| Pump launch state | `chaos run pumpfun_launch_read <MINT>` (launch history needs a Helius key) / direct protocol decode |
| smart-wallet clusters | `chaos-smart-money-signals`, then Helius confirmation |
| generic network stats/basic portfolio | existing `solana` skill helper |
| GMGN cross-check | `gmgn-alpha-intelligence` adapter only |

## Canonical Token Procedure

1. Validate the Solana mint shape.
2. Run the current Helius/Dex/Pump analyzer:

   ```bash
   chaos analyze token <MINT> --no-x
   ```

3. Read classification, gate, position context, concentration, flow, and failure
   states. Missing primary evidence stays missing.
4. If GMGN is explicitly useful and configured, run:

   ```bash
   chaos analyze token <MINT> --gmgn --no-x
   ```

5. Use GMGN as a namespaced cross-check only. It cannot change the already-computed
   classification, gate, position context, delta, or fact grade in this release.
6. Add X only when attention/catalyst evidence is required. Resolve the mint first.

## Wallet Procedure

1. Identify the exact wallet and why it matters.
2. Use transaction history (any Solana RPC; Helius adds the mint history and wallet API), funding edges, token events, and realized behavior.
3. Check independence: shared funder, bundle, transfer chain, CEX/onramp, or sybil
   relationships can make many wallets one actor.
4. Apply `chaos-wallet-attribution` and existing wallet-quality scoring.
5. GMGN KOL/smart-money/PnL labels are supporting features only. Never replace the
   chaos-trader verdict with a provider label.
6. Do not follow/copy a wallet or mutate a GMGN follow list.

## Transaction and Launch Procedure

- Use Helius/direct RPC for signatures and account deltas.
- Use direct Pump/PumpSwap protocol state for lifecycle/graduation claims.
- Treat explorer pages and provider descriptions as secondary display surfaces.
- Preserve failed decodes and unknown programs; do not infer a benign transaction.

## Basic Solana Helper

For generic network/RPC questions outside the trading pipeline:

```bash
chaos run solana_client stats
chaos run solana_client tx <SIGNATURE>
chaos run solana_client token <MINT>
chaos run solana_client wallet <WALLET> --no-prices
```

The generic helper is not the source of chaos-trader candidate, wallet-score, risk, or
position state.

## Pipeline Boundaries

Read-only intelligence may produce artifacts and ledger evidence. It may feed advisory decisions and the deterministic paper policy, but the intelligence skill itself may not:

- bypass the paper engine to write position state;
- create a live execution intent;
- sign or send transactions;
- overwrite `wallet_token_events`, concentration snapshots, or Helius transaction
  records with provider estimates;
- insert GMGN discovery directly into `token_signals` or autopilot candidates;
- count two presentations of the same onchain event as independent confirmation.

Execution remains governed separately by `chaos-execution-control` and explicit
user approval.

## Evidence Receipt

For every material conclusion, retain:

```text
chain: sol
mint / wallet / signature
source and source_id
observed_at_utc
raw or artifact reference
fact vs provider label vs inference
freshness / completeness
failure state
confidence
contradicting evidence
```

## Verification Checklist

- [ ] Exact Solana identity was validated.
- [ ] Helius/direct RPC was used for raw onchain truth.
- [ ] Dex market data came from the existing resolver.
- [ ] Pump state came from direct protocol evidence.
- [ ] External labels remained attributed and timestamped.
- [ ] Correlated observations were not double-counted.
- [ ] Missing/failing sources stayed visible.
- [ ] Existing classification, gate, risk, position, and execution boundaries were preserved.
- [ ] No signing, send, follow-wallet, direct candidate mutation, paper-policy bypass, or live position mutation occurred.
