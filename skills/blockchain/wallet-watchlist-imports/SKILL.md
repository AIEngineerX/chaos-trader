---
name: wallet-watchlist-imports
description: Use when converting CSV, JSON, copied tables, or leaderboard exports into a wallet list that a named destination can import (a tracker, a terminal watchlist, or the home roster), and validating the result with `chaos run validate_wallet_import`. Format conversion first; wallet scoring and attribution belong to other skills.
version: 0.1.0
author: chaos-trader contributors
license: MIT
metadata:
  hermes:
    tags: [crypto, wallets, watchlists, import, export, axiom, terminal]
    related_skills: [chaos-wallet-attribution, chaos-smart-money-signals]
---

# Wallet Watchlist Imports

This skill turns wallet lists from any source into a file the named destination
can import, then checks that file. It is a format-conversion task first and an
attribution task only when the user asks for one separately.

## Prerequisites

- The package is installed: `pip install git+https://github.com/AIEngineerX/chaos-trader` (or it came with the Hermes profile).
- A home exists: `chaos onboard` was run once (`chaos onboard --yes` for the defaults).
- `CHAOS_HOME` points at that home, or you are inside the Hermes profile, which sets it. Check with `chaos --version` and `chaos help`.

## Purpose

Convert CSV, JSON, copied tables, or leaderboard exports into a wallet list that the
named destination can actually import, and validate it before handing it over.

## When to Use

- User wants a wallet list cleaned, merged, deduplicated, labeled, or reformatted for an importer
- User wants a leaderboard export turned into a tracked-wallet import file
- User wants a wallet import file checked before uploading it
- User wants wallets from a list added to the home roster

Load it before writing the artifact, even when a destination's format looks familiar.

Do not use it for wallet profitability or identity analysis unless that is a separate,
explicit request; route those questions to `chaos-wallet-attribution` or
`chaos-smart-money-signals`.

## First Gate: Identify the Destination

Never infer the destination from:

- the source website or filename;
- the word `Terminal`;
- a field name shared by two products;
- an old import produced for another platform.

Before generating a file, resolve the exact product and import surface, such as:

- Axiom tracked-wallet import;
- pump.fun Terminal wallet tracking;
- another terminal's watchlist importer;
- the home roster, through `chaos wallets --add <address> --tier A|B|C`.

If the destination is ambiguous and its choice changes the schema, ask one concise
question. Do not silently default to Axiom.

## Schema Proof Standard

Use the strongest available contract in this order:

1. A current export or template produced by the destination UI.
2. Current official documentation for that import surface.
3. A file that previously imported successfully into the same product and importer.
4. A one-record example the user supplies and states imports successfully.

Shared internal fields do not establish import compatibility. Seeing `trackedWalletAddress`
in two products proves only that both use the concept, not that their wrapper, optional
fields, limits, or importer are identical.

If no schema contract is available, do not label the output `import-ready`. Request a
one-wallet export or template. A syntactically valid JSON file is not the same as a
platform-accepted import.

## Conversion Procedure

1. Read every source file with a real CSV or JSON parser.
2. Normalize whitespace and preserve the original wallet case.
3. Determine the chain before merging. Do not place EVM addresses into a Solana-only import.
4. Validate Solana addresses by Base58 decoding to exactly 32 bytes; a regex alone is insufficient.
5. Deduplicate by exact validated wallet address.
6. Merge metadata from duplicate rows rather than discarding the stronger source.
7. Build concise, unique names within the destination's proven length and character limits.
8. Assign emojis only if the destination schema supports them. If the user requests unique emojis, verify uniqueness programmatically.
9. Emit the exact required root shape: bare array, wrapped object, CSV, or another contract.
10. Re-read the written artifact and validate schema, address count, duplicate count, name constraints, and emoji constraints. For a bare-array import, run the validator below.
11. When possible, exercise the destination importer. Otherwise report `schema-validated; UI acceptance not exercised`.

## Validator

`chaos run validate_wallet_import` checks a bare-array Solana wallet import: the root is a
JSON array, every row has exactly the expected fields, every `trackedWalletAddress`
decodes from Base58 to 32 bytes, names are non-empty strings, and no address or name
repeats.

```bash
chaos run validate_wallet_import <file>
chaos run validate_wallet_import <file> --fields trackedWalletAddress,name,emoji,alertsOn --max-name 48 --unique-emoji
```

- `--fields` is the exact comma-separated field set each row must have. The default is `trackedWalletAddress,name,emoji,alertsOn`.
- `--max-name N` fails any name longer than N characters.
- `--unique-emoji` fails repeated emojis.

It prints a JSON report with `ok`, `rows`, and `errors`, and exits 0 when the file passes
and 1 when it does not. A pass proves the file matches the contract you gave it, not that
the destination accepts it.

## Naming Rules

Names should identify the wallet without turning labels into miniature reports.

Prefer:

```text
PF PNL 01 flatstarfish
PF CALL 04 whereslodo
PF DUAL UniteTrenches P35 C02
```

Avoid unverified performance claims, corrupted peak multiples, excessive punctuation,
and labels longer than the platform allows. Keep detailed metrics in an audit companion
file, not in the import label.

When two source lists contain the same address, use one import row and a compact
dual-source label. Preserve full source ranks and metrics in the audit file.

## Axiom Contract

A known-good Axiom tracked-wallet import is a bare JSON array containing only:

```json
[
  {
    "trackedWalletAddress": "<address>",
    "name": "<unique concise name>",
    "emoji": "<emoji>",
    "alertsOn": true
  }
]
```

Cap names at 48 characters unless a current Axiom template proves a different limit. Do
not add metadata wrappers, Markdown fences, comments, source fields, ranks, or URLs to
the import file. Put those in a separate audit artifact.

Do not reuse this contract for another product merely because its frontend contains
`trackedWalletAddress`.

## Home Roster Contract

The home roster takes Solana wallets one at a time, each with a tier:

```bash
chaos wallets --add <address> --tier A|B|C
```

Run it once per wallet. It edits the home roster only and never calls the chain. It
refuses an address that is not base58 and an address already on the roster, so
deduplicate first and report each refusal. Choose the tier from the user's instruction;
when the user gives none, ask. `docs/smart-wallets.md` describes how roster wallets are
read afterwards.

## Deliverables

Default outputs:

1. The exact import artifact, or the list of `chaos wallets --add` commands for the home roster.
2. A separate audit CSV or JSON with source, rank, merged duplicates, exclusions, and validation notes.
3. A compact result card stating:
   - input rows;
   - valid rows;
   - duplicates merged;
   - exclusions by chain or reason;
   - final unique wallets;
   - destination and schema source;
   - whether the real importer was exercised.

Attach the import file when the user asked for a file. Do not bury it behind a report.

## Pitfalls

- **Axiom equals Terminal.** Product names and schemas must be resolved independently.
- **Source determines destination.** A pump.fun leaderboard can still be imported elsewhere; its origin says nothing about the requested importer.
- **Valid JSON means import-ready.** Platform constraints may reject valid JSON.
- **Regex-only Solana validation.** Decode Base58 and require 32 bytes.
- **Overloaded names.** Rich metrics belong in the audit file.
- **Silent exclusions.** Report EVM, malformed, duplicate, or unsupported rows explicitly.
- **Claiming success without importer proof.** Distinguish artifact validation from UI acceptance.

## Supporting Files

- `references/platform-schema-notes.md` records known contracts and evidence boundaries for specific wallet importers.
- `chaos run validate_wallet_import` validates bare-array wallet imports, Solana addresses, duplicate names, addresses, and emojis, and optional name limits.

## Verification Checklist

- [ ] Exact destination product and import surface identified.
- [ ] Schema backed by a current template, docs, or known-good same-product artifact.
- [ ] Chain separated correctly.
- [ ] Solana addresses decode to 32 bytes.
- [ ] Duplicate addresses merged.
- [ ] Names fit proven limits and are unique when required.
- [ ] Emoji field is supported and uniqueness checked when requested.
- [ ] Root shape and field set exactly match the target contract.
- [ ] Written file was re-parsed and validated.
- [ ] Importer exercise state reported honestly.
