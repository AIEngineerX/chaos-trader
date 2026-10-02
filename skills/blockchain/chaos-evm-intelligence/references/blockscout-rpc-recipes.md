# Blockscout and JSON-RPC Recipes

## Minimal JSON-RPC gate

Call `eth_chainId` first, then `eth_getCode`:

```json
{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}
{"jsonrpc":"2.0","method":"eth_getCode","params":["<ADDRESS>","latest"],"id":2}
```

Reject the endpoint if the chain ID is wrong. Empty bytecode (`0x`) is only an EOA candidate; verify nonce and activity before attribution.

## Standard ERC-20 reads

Common selectors:

```text
name()        0x06fdde03
symbol()      0x95d89b41
totalSupply() 0x18160ddd
balanceOf()   0x70a08231 + 32-byte left-padded address
```

Use the contract ABI for decimals, deployer/creator, curve, factory, tax, reserves, graduation, and proxy-specific reads. Record the block number with current balances.

## Blockscout recovery

When dynamic pages or v2 JSON routes are guarded, try the explorer's legacy read-only endpoints:

```text
/api?module=contract&action=getsourcecode&address=<ADDRESS>
/api?module=token&action=getToken&contractaddress=<ADDRESS>
/api?module=token&action=getTokenHolders&contractaddress=<ADDRESS>
```

Use the contract page's exact-match status and constructor arguments to identify deployer, curve, factory, supply, and embedded metadata. Clone the official source repository only after matching its deployed factory/implementation addresses to explorer evidence.

## Fresh-launch state reconciliation

Re-read immediately before reporting:

```text
contract bytecode
creator balance
curve/pool token balance
zero/dead balances
curve/pool native balance
current block
official social confirmation
```

Calculate:

```text
creator % total = creator_balance / total_supply
sold or circulating candidate = total_supply - protocol_inventory - burned
creator % sold = creator_balance / sold_or_circulating_candidate
```

Label the circulating figure as a candidate when locks, bridges, vesting, or multiple pools are unresolved.

## Market-stage interpretation

- **Bonding curve:** sells can work, but LP lock/burn language is premature.
- **Graduation pending:** verify threshold accounting from contract state; raw ETH balance can include fees.
- **Graduated:** verify pool creation, pool reserves, position ownership, locker restrictions, and actual sell depth.
- **Provider “normal” label:** secondary evidence only; source privileges and live sell receipts decide mechanics.

## Issuer verification

Search the exact contract and launch URL on the embedded official account. If no result appears for a brand-new token, report `authorization unconfirmed` and search once more near delivery. If the account confirms the contract but calls it a test, bot experiment, or possible deprecation, classify authenticity as confirmed and commitment as weak; do not merge them into one safety verdict.
