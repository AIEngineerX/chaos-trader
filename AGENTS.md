# Working in this repository

This file is for people and for coding agents (Codex, Hermes, Claude Code, Cursor). It says what the project is, what it must never become, and how a change gets in. `CONTRIBUTING.md` has the longer version of the gates; this file is the short contract.

## What this is

chaos-trader is a Solana research package: wallet reads, token ranking, paper trading, and agent skills. One repository is three things at once:

- a pip package (`chaos_trader/`), with the `chaos` command;
- a skills pack (`skills/blockchain/<name>/SKILL.md`, 14 skills) that Claude Code, Codex, and Hermes load;
- a Hermes profile distribution (`distribution.yaml`, `SOUL.md`, `config.yaml`, `cron/jobs.json`).

The code runs from the installed package. `CHAOS_HOME` holds state only: `.env`, the roster, the paper config, databases, reports. Nothing here signs, routes, or sends a transaction, and nothing in this repository may change that.

## The one sentence about what it recommends

Every file that describes verdicts uses this exact sentence, and a test pins it:

> This package labels tokens study, watch, manual-review, or avoid-entry; its paper loop decides enter, wait, or avoid; and for tokens your own wallets hold it reports a position state (hold-core, manage, trim-risk, or exit-watch) that describes risk, not an instruction. It never says buy or sell, and it cannot sign, route, or send.

Do not paraphrase it. Do not add buy or sell vocabulary anywhere.

## Before any change

1. Read `README.md` for how the commands behave, `docs/smart-wallets.md` for the wallet lane, `SECURITY.md` for the boundary.
2. Run the gates once on a clean checkout so you know they pass before you touch anything:

       make verify PY=.venv/bin/python     # Windows: PY=.venv/Scripts/python

   `make verify` runs: safety check, private-name gate, compile, both test suites with `-W error::ResourceWarning`, full-history gitleaks, full-history private-name scan, dependency audit. CI runs the same on Python 3.11 and 3.14.

## Rules a change must follow

- **Tests run real code.** Real SQLite files, real files, the real CLI as a subprocess. Replace only what leaves the process: the network boundary, DNS, the external `gmgn-cli` binary, clocks. A test that fakes a database or the filesystem will be rejected.
- **No warnings.** Both suites run with ResourceWarning as an error. Close what you open.
- **Every env var is documented.** A new variable needs a row in the README Configuration table; a test checks it.
- **Every verb is in `chaos help`.** A test checks that too. Verbs with JSON output use the envelope in `chaos_trader/trading/scripts/json_contract.py` (`--json`); `--raw` stays unchanged for compatibility.
- **No new runtime dependency without an issue first.** Today there is one: PyYAML. The MCP server is an optional extra.
- **No performance claims.** Not in docs, not in comments, not in commit messages. `chaos outcomes` is the only source for any statement about how past reads did, and it refuses to score under 20 reads.
- **Plain words in docs.** The README test bans a list of marketing words. Every claim sits next to the command that checks it.
- **Names.** No personal names, handles, machine names, local paths, or private product names anywhere, including commit messages and test fixtures. The private-name gate checks by digest; the history scan checks every past blob. If either fails, fix the text; never add an exception.
- **Skills.** Every `SKILL.md` keeps its frontmatter within the limits `tests/test_skill_frontmatter.py` enforces, starts with the shared Prerequisites block, uses `chaos` verbs or `chaos run <script>` only (no shell variables, no script paths), and shares no trigger phrase with another skill.
- **Scope.** Signing, wallet connection, swap routing, fund movement, live alerts, and public posting are out of scope and will be closed. The Hermes profile ships its cron jobs paused.

## Commits and releases

- One logical change per commit, `feat:`, `fix:`, `docs:`, `test:`, or `chore:` prefix, body says what changed and why. Never force-push `main`.
- CHANGELOG bullets go under the version being prepared.
- A release is a `vX.Y.Z` tag. `.github/workflows/publish.yml` refuses a tag that does not equal the version in `pyproject.toml` and `distribution.yaml`, runs the tests, builds, and publishes to PyPI by trusted publishing. Rollback is described in `CONTRIBUTING.md`.

## Where things are

| Need | Place |
|---|---|
| Command router and cards | `chaos_trader/trading/scripts/chaos_cmd.py` |
| Pipeline scripts (flat imports) | `chaos_trader/trading/scripts/` |
| Cron wrappers | `chaos_trader/jobs/` |
| MCP server | `chaos_trader/mcp_server.py` (`chaos mcp`) |
| Skills | `skills/blockchain/` |
| Seed roster | `chaos_trader/seed/roster.json` (nine public wallets, a starting set) |
| Schemas | `chaos_trader/trading/schemas/` |
| Gates | `Makefile`, `tools/safety_check.py`, `tools/history_name_scan.py`, `tests/test_no_vendor_names.py` |
| What was verified, and when | `docs/verification-2026-10-01.md` |
