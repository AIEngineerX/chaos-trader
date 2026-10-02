---
name: chaos-evm-intelligence
description: Guidance for reading EVM wallets, tokens, and contracts by hand with Blockscout and JSON-RPC. The pipeline itself reads Solana only.
version: 0.1.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [chaos, evm, wallet, token, contract, attribution, risk]
    related_skills: [chaos-wallet-attribution, chaos-crypto-trader, chaos-risk-engine]
---

# Chaos EVM Intelligence

This skill is guidance for reading EVM wallets, tokens, and contracts by hand with
Blockscout and JSON-RPC. No `chaos` command reads an EVM chain: the pipeline reads Solana
only, so every EVM fact here comes from the explorer and RPC calls the agent makes itself.

## Prerequisites

- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Use This Skill For

Use for EVM-format wallet reviews, token contracts, deployers, curves, pools, factories, launchpads, holder concentration, and contract-risk checks on any EVM network.

## Procedure

1. **Resolve the chain before history.** Obtain the chain from the user's words, explorer, transaction, venue, or contract context. Record mainnet/testnet, decimal and hex chain ID, canonical explorer, and canonical RPC. If absent, ask for the chain rather than searching unrelated networks.
2. **Validate the RPC.** Call `eth_chainId`; do not trust balances, bytecode, or transactions from an endpoint until its result matches the target chain. If the canonical public endpoint is rate-limited, use an independently listed public endpoint and repeat this check.
3. **Classify the address.** Call `eth_getCode` before applying wallet semantics. `0x` means EOA candidate; non-empty bytecode means contract. For contracts, identify token, proxy, curve, pool, router, safe, factory, or application role before continuing.
4. **Establish a positive chain anchor.** Require a matching balance, verified contract, recent timestamp, expected token, counterparty, or transaction. Treat the same hexadecimal address on other EVM networks as unrelated unless an explicit bridge path links it.
5. **Read primary state directly.** For EOAs, inspect native/token balances, nonce, funding, transactions, and approvals. For ERC-20s, read name, symbol, decimals, total supply, creator/deployer, curve/pool balances, creator balance, and zero/dead balances through RPC.
6. **Recover and inspect verified source.** Check exact-match verification and proxy implementation. Review minting, blacklist, pause, transfer restrictions, taxes, ownership/admin powers, upgradeability, rescue paths, and liquidity/graduation contracts. A clean token does not clear a risky curve, factory, hook, locker, or admin path.
7. **Prove sellability.** Prefer successful live sells and decoded state changes over provider “normal” labels or simulations. Keep token-transferability, market sellability, and exit depth as separate findings.
8. **Separate protocol inventory from holders.** Exclude curve, pool, locker, burn, bridge, and protocol inventory before describing concentration. Report creator holdings as a percentage of both total supply and economically circulating/sold supply when possible.
9. **Verify issuer authorization and commitment.** Constructor metadata and embedded social links are claims. Search the linked official account for the exact contract or launch URL. On very new launches, repeat the search near delivery because confirmation can appear within hours. Distinguish authenticity from commitment: an official experimental token may still be `avoid-entry`.
10. **Reconcile freshness.** Use indexers for discovery and direct RPC for live bytecode and balances. Label provider prices, holder tables, and graduation progress as estimates when delayed. Never convert unavailable or stale values into zero.
11. **Issue one read label.** Lead with one of the package's read labels, `study`, `watch`, `manual-review`, or `avoid-entry`, and the main failure mode. State contract mechanics, current market phase, liquidity/exit risk, holder overhang, issuer evidence, invalidation, and exact conditions to reconsider. Do not force a paper action.

For reusable Blockscout and JSON-RPC recipes, load `references/blockscout-rpc-recipes.md`.

## Always-On Risk Rules

- Treat a contract address presented as a “wallet” as an address-classification problem first.
- Treat bonding-curve inventory as protocol inventory, not holder concentration; creator and buyer balances remain sellable overhang.
- Do not call LP burned or locked while the asset is still on a bonding curve. Verify the post-graduation pool and lock on-chain after graduation.
- Do not equate a curve's raw native balance with graduation progress when fees, phantom reserves, or accounting buckets coexist.
- Do not infer common ownership from one funder, shared router, launchpad, or same-address activity on another chain.
- Distinguish mechanical safety, current sellability, economic edge, issuer authenticity, and issuer commitment. Passing one does not pass the others.

## Output Shape

- **Verdict:** one read label (`study`, `watch`, `manual-review`, or `avoid-entry`), confidence, and the main failure mode.
- **Identity:** chain, chain ID, EOA/contract, contract role.
- **Mechanics:** privileges, supply controls, sell evidence, curve/pool phase.
- **Distribution:** creator and non-protocol concentration.
- **Market:** liquidity, volume, age, price regime, executable exit.
- **Issuer:** exact authorization evidence, commitment, contradictions.
- **Risk:** main failure mode and invalidation.
- **Next check:** the exact evidence required before reconsidering.

## Common Pitfalls

1. **Contract treated as wallet.** Run `eth_getCode`; wallet PnL logic is invalid for tokens, curves, and safes.
2. **Explorer snapshot treated as live.** Re-read bytecode and balances through a chain-ID-validated RPC because fast launches move before indexers refresh.
3. **Verified token treated as verified system.** Inspect every contract that controls trading, liquidity, fees, upgrades, or graduation.
4. **Metadata treated as endorsement.** Require the official account to confirm the exact address or launch URL.
5. **Successful sell treated as adequate liquidity.** A non-honeypot can still be impossible to exit at useful size.
6. **Top holders include the curve.** Exclude protocol inventory before judging concentration, then show creator overhang separately.
