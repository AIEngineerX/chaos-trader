# Changelog

All notable changes to this package. Dates are UTC.

## 0.1.0 — 2026-10-02 (initial public release)

- Releases go to PyPI as `chaos-trader` through trusted publishing: a `vX.Y.Z` tag runs `.github/workflows/publish.yml`, which refuses a tag that differs from the package version, builds the wheel and sdist, and uploads them with no stored token.
- `chaos wallets --add <address> --tier A|B|C` and `chaos wallets --remove <address>` edit the home roster; they never call the chain and mark the file `user-edited`.
- The recommendation sentence names the position states a card prints for tokens your own wallets hold, and `docs/smart-wallets.md` names the two caution gate states. `chaos-crypto-trader` answers what the read says and what the paper loop would do, with the paper engine's plan labelled as simulated. `chaos onboard` with fd 0 closed exits 2 with the needs-a-terminal sentence instead of a traceback.
- The full-history private-name scan is a hard gate in `make verify` and CI.
- Skills stand alone: each `SKILL.md` opens with a Prerequisites block, speaks of "the agent" and "the user", and gives read labels and paper decisions instead of "decisive" buy/sell advice; `chaos-position-aware-alpha` triggers on positions your own wallets hold, so no trigger phrase is shared. `docs/smart-wallets.md` documents the your-own-wallets file. Tests pin all three, and a README and skills guard bans trade recommendations outside the one recommendation sentence.
- CI audits the wheel's dependencies and scans the full git history for private names; onboard checks the home before prompting.
- SECURITY.md is a policy (reporting, supported versions, what is stored); one statement of what the package recommends, used everywhere; CONTRIBUTING matches the gates.
- The home roster is a watch source for token reads (A and B count as quality hits, C as scout); the promoter's candidate and sensor verdicts count too.
- Seed roster v2: nine live wallets. `chaos wallets --review` reports each roster wallet's last activity and track record from the local database. README gains Keeping it current.
- Bearer tokens no longer follow HTTP redirects (signal API, xAI); the safety check scans journal directories; build requires setuptools 83 or newer.
- Known: `alpha_intake_local.py` prints source names and raw note text from your own imported export. The output stays on your machine.
- Makefile works with a relative `PY`; macOS temp-path test fix; key-storage wording; 60-second path; PEP 639 license metadata.
- `chaos run` keeps the caller's working directory, so relative `--roster` and `--out` paths resolve where you are; all of `trading/reports/` is ignored; the data folders are declared to setuptools; wording fixes from the public-release review.
- Known: the wheel carries the tests under `chaos_trader/trading/scripts/test_*.py`; accepted for 0.1.0.
- The 12 skills live at the repository root under `skills/blockchain/`; the wheel still carries them.
- The repository is a Hermes profile distribution (`distribution.yaml`, `SOUL.md`, `config.yaml`, `cron/jobs.json` with three jobs shipped paused), installed with `hermes profile install`. The profile is named `chaos-trader` so the `--alias` wrapper cannot shadow the `chaos` command.
- `chaos run <script> [args]` runs any pipeline script, job wrapper, or skill helper from the installed package; `chaos run` alone lists them.
- `chaos onboard` no longer copies code into `CHAOS_HOME`; the home holds state only. `chaos update` refreshes `paper_autopilot.defaults.yaml` and prints the pip command for code updates, which installs from the repository.
- `chaos paper` is retired: it read `token_signals`, which nothing writes; `chaos paper-report` is the paper book and `chaos strategy-paper <mint>` the one-mint paper read.
- `chaos_trader/deploy/sync_to_profile.py` is removed.
- Wallet reads on any Solana RPC (`getSignaturesForAddress` + `getTransaction`), rows tagged `solana_rpc`; Helius rows keep `helius_rpc` and both are read together.
- Token reads, structural gates, candidate tape, paper loop with fees, slippage, and stops.
- `chaos onboard`, `chaos update`, `chaos skills install --for claude | codex | hermes | all`.
- 12 skills in `SKILL.md` form, harness-neutral.
- X evidence through `X_SEARCH_PROVIDER` (`hermes`, `xai`, `none`); unsourced answers never change a verdict.
- Fresh-home behaviour: every command exits 0 with a plain sentence when there is nothing to show.
- Vendor and owner-name gate by digest; CI with full-history gitleaks and a wheel-content check.
- Not in this release: any execution, signing, routing, or key handling. Wallet discovery still needs a Helius key.
