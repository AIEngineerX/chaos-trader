# Security

## Reporting a vulnerability

Use the "Report a vulnerability" button on the repository's Security tab. It opens a private advisory that only the maintainer can read. If that button is not available, open an issue titled "security" with no details, and the maintainer replies with a private channel.

Reports are acknowledged within 7 days. There is no bounty.

Do not put a key leak, a signing path, or exploit details in a public issue.

## Supported versions

Only the latest 0.1.x release receives fixes.

## What this package stores

The code never stores wallet keys, signer material, seed phrases, or transaction-authority secrets. Optional API credentials, such as Helius or xAI keys, live only in your local `CHAOS_HOME/.env`. The optional GMGN cross-check, off by default, also reads `GMGN_API_KEY` from `~/.config/gmgn/.env` when the variable is unset. It reads public chain data through the RPC you configure. The `chaos` verbs write only under `CHAOS_HOME` (plus `chaos skills install`, which writes the harness skill folders it prints, and `--force` can replace a same-named skill folder there). Scripts run through `chaos run` default to `CHAOS_HOME` but write wherever their own `--out`/`--db` flags point.

`CHAOS_PYTHON` and `GMGN_CLI` name an interpreter or a binary that the package runs as a child process. They are local trust settings: point them only at executables you trust.

## Execution boundary

This package labels tokens study, watch, manual-review, or avoid-entry; its paper loop decides enter, wait, or avoid; and for tokens your own wallets hold it reports a position state (hold-core, manage, trim-risk, or exit-watch) that describes risk, not an instruction. It never says buy or sell, and it cannot sign, route, or send.

No signing, transaction construction, swap routing, wallet connection, public posting, unattended external alerts, process controls, or live execution enablement.

## Forbidden in the repository

- `.env`, `.env.*`, `auth.json`, `auth.lock`, `channel_directory.json`.
- Secrets, credentials, keypairs, signer material, private keys, any wallet list that is not the public seed.
- Runtime DBs (`*.sqlite`, `*.db`, WAL/SHM files), sessions, memories, logs, caches.
- Operator telemetry, reports, research outputs, watchlists, learning/training data.
- Archives, nested archives, encrypted backups, package outputs.
- Absolute local user paths.

## Release gate

Every release passes these checks:

- `python tools/safety_check.py`: no secrets and no local user paths.
- `make vendor-check`: no private names in the tracked files.
- `make test`: both test suites, with `ResourceWarning` as an error.
- A full-history gitleaks scan.
- A wheel build and a check of the wheel's contents.
- `pip-audit` on the wheel.
- A full-history private-name scan.

`make verify` runs seven gates in this order: the safety check, the vendor check, a compile pass, the tests, the gitleaks scan, the full-history private-name scan, and the dependency audit. Any one that fails stops it with an error. CI runs the seven checks listed above on every push to `main` and every pull request.
