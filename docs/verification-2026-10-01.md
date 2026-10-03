# Verification record, 2026-10-01

Clean-machine proof (spec gate 8) and a Claude Code skill load. Every block names the command that produced it. Paths use `~` for the home directory.

Host: Windows 11, Git Bash. `$TMP` is a scratch directory, `$REPO` is the chaos-trader checkout, `$CHAOS_HOME` is `$TMP/ct-clean-home`. Source: `f653e0d` on `main`, installed from a local git URL.

## 1. Fresh venv, install from git URL, onboard, first verdict

Command: `python --version`

```
Python 3.14.2
```

Command (one chained line, timed with `date +%s`):

```
cd $TMP && rm -rf ct-clean && python -m venv ct-clean && T0=$(date +%s) \
 && ./ct-clean/Scripts/python -m pip install -q git+file://$REPO \
 && export CHAOS_HOME=$TMP/ct-clean-home && rm -rf "$CHAOS_HOME" \
 && ./ct-clean/Scripts/chaos onboard --rpc-url https://api.mainnet-beta.solana.com --yes \
 && ./ct-clean/Scripts/chaos token So11111111111111111111111111111111111111112 \
 && echo "seconds=$(( $(date +%s) - T0 ))"
```

URL form that worked: `git+file://$REPO` (no Windows path rewrite needed).

Output of the `chaos onboard` step:

```
chaos-trader is set up in $CHAOS_HOME
Next: CHAOS_HOME=$CHAOS_HOME chaos token So11111111111111111111111111111111111111112
```

First 10 lines from `./ct-clean/Scripts/chaos token So11111111111111111111111111111111111111112 | head -10` (exit 0):

```
☄️ 🟡 TOKEN READ
$SOL · `So1111…1112`
MODE: **conviction trench**
ENTRY GATE: **study**
POSITION: **no-position**
CATALYST: none
FLOW: dead-churn · fake-flow high · conversion low
MC $1456.86M · Liq $37.20M · V/L 0.106x
WALLETS: Q 0 · timing none · owner 0/0
WHY:
```

Result:

```
seconds=17
```

Target was under 600. Network use: PyPI dependencies, the Solana RPC, and market data.

## 2. The install touched nothing outside `CHAOS_HOME` and the venv

Command: `stat -c '%y %n' ~/.hermes ct-clean-home/.env; date`

```
2026-08-07 20:54:25 -0400 ~/.hermes
2026-10-01 00:18:11 -0400 ct-clean-home/.env
Thu Oct  1 00:18:21 EDT 2026
```

`~/.hermes` is a pre-existing directory from other work. Its mtime is 2026-08-07, which predates the onboard by almost two months. The directory mtime is unchanged (2026-08-07), so no entries were added or removed.

Command: `find $TMP/ct-clean/Lib/site-packages/chaos_trader -newer $CHAOS_HOME/.env -type f | wc -l`

```
0
```

No package file was written after the onboard.

Command: `ls -A "$CHAOS_HOME"`

```
.env
logs
scripts
skills
trading
```

## 3. Claude Code skill load (headless)

No interactive session was available, so this ran headless.

Command: `mkdir -p $TMP/ct-skill-proj/.claude/skills && cp -r $REPO/chaos_trader/skills/blockchain/chaos-solana-intelligence $TMP/ct-skill-proj/.claude/skills/`

Result: `.claude/skills/chaos-solana-intelligence/SKILL.md` exists.

Command, run from `$TMP/ct-skill-proj`:

```
CHAOS_HOME=$CHAOS_HOME claude -p "Using the chaos-solana-intelligence skill, give me the verdict card for So11111111111111111111111111111111111111112. Run the script the skill names and paste the card." --allowedTools "Bash,Read,Glob,Grep" --max-turns 12
```

Exit code 0. First 10 lines of the card it returned:

```
☄️ 🟡 DEEP TOKEN ANALYSIS
$SOL · `So1111…1112`
MODE: **unknown**
ENTRY GATE: **study**
POSITION: **no-position**
CATALYST: none
FLOW: dead-churn · fake-flow high · conversion low
MC ? · Liq $37.29M · V/L 0.106x
WALLETS: Q 0 · timing none · owner 0/0
WHY:
```

Evidence a script ran, not model memory:

- The model reported running the skill's `chaos_cmd.py analyze … --no-x`.
- The card ends with a `Files:` line naming a sweep file under `$CHAOS_HOME`:

  ```
  Files: $CHAOS_HOME/trading/alpha/sweeps/2026-10-01/token_event_SOL_So111111_20261001T041858Z.md
  ```

  Command `ls "$CHAOS_HOME/trading/alpha/sweeps/2026-10-01/"` listed that file, captured before removal. The home was removed after capture; the `Files:` line above is the evidence. A model answering from memory could not have created it.
- The market cap is `?` here and `$1456.86M` in section 1. The two runs resolved the market differently, which a memorized answer would not do.

Status of this step: the script the skill names was run and produced the sweep file `token_event_SOL_So111111_20261001T041858Z.md`, and a verdict card for SOL came back.

Skill-registry loading (the skill listed by name in a Claude Code session): not verified in this run. The headless run had no Skill tool, so it could have found `SKILL.md` with file search instead.

### Finding from this step (fixed afterwards)

The first headless attempt crashed on output only. The analysis completed and wrote its sweep file (`...T041846Z`), but Python's Windows `cp1252` console encoding could not print the emoji header. The model re-ran the same command with `PYTHONIOENCODING=utf-8` and got the card above. The sweep directory holds three runs: the section 1 `chaos token`, the failed attempt, and the retry. The script was not modified. This is a Windows-only console-encoding gap in the skill script's printing, worth a follow-up.

Fixed afterwards in `chaos_trader/home.py` and `chaos_trader/trading/scripts/chaos_home.py`: both reconfigure stdout and stderr to UTF-8 with `errors="replace"` when the stream is not already UTF-8, and the job scripts do the same.

## 4. Base58 classification

Every base58-shaped string (32 to 44 characters, containing a digit) in the tracked files falls into one of the groups below. The inventory came from a one-off scan of `git ls-files`, and each non-roster string was read in place.

- **Seed-roster addresses.** The 12 wallets in `chaos_trader/seed/roster.json`, which declares `"source": "public"` and holds address and tier only.
- **Program ids.** Constants such as SPL Token, Token-2022, Associated Token, pump.fun, pump.swap, Raydium, Orca, Meteora, Jupiter, the System Program, and the incinerator, in `holder_resolver.py`, `pumpfun_launch_read.py`, `wallet_graph.py`, `wallet_scan.py`, and the solana skill's client.
- **Mints.** Wrapped and native SOL, USDC, USDT, the token-symbol table in the solana skill's client, BONK in that skill's `SKILL.md`, and pump.fun mints used as test fixtures and in the `.gitleaks.toml` allowlist.
- **Synthetic test placeholders.** Strings made of repeated characters, such as `Mint111…` or `2222…`, in `test_*.py` files only.
- **The solana skill's upstream example wallet.** `9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM` at `chaos_trader/skills/blockchain/solana/SKILL.md:89`, `:132`, and `:141`. It comes from the upstream skill file, as do the other strings in that skill.

### Finding from this step (fixed afterwards)

Three Raydium program-id constants do not exist on-chain. A `getMultipleAccounts` call to `https://api.mainnet-beta.solana.com` returned no account for them:

```
holder_resolver.py:25       (a mistyped id, labeled Raydium AMM v4)
pumpfun_launch_read.py:20   (a different mistyped id, labeled Raydium AMM v4)
pumpfun_launch_read.py:21   (a mistyped id, labeled Raydium CPMM)
```

The same call showed `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` and `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` are executable programs owned by the upgradeable BPF loader. Pool detection that depends on the three missing ids cannot match.

Fixed afterwards: see section 5.

## 5. Program ids

A later audit found two more non-existent ids: the same two mistyped Raydium literals in `wallet_graph.py:23-24`, and a mistyped Raydium CLMM id in `holder_resolver.py:24`. `tools/check_program_ids.py` then found a fifth, the Orca Whirlpool id in `holder_resolver.py:26`, which the RPC rejected as the wrong size. All were replaced with ids verified executable on the public RPC. The Raydium ids are AMM v4 `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8`, CLMM `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK`, CPMM `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C`, and Orca Whirlpool is `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc`.

Command (public RPC, 2026-10-01, exit 0):

```
python tools/check_program_ids.py
```

```
executable     6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P  holder_resolver.py:21, pumpfun_launch_read.py:18, wallet_graph.py:20
executable     pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA  holder_resolver.py:22
executable     CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C  holder_resolver.py:23, pumpfun_launch_read.py:21, wallet_graph.py:24
executable     CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK  holder_resolver.py:24
executable     675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8  holder_resolver.py:25, pumpfun_launch_read.py:20, wallet_graph.py:23
executable     whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc  holder_resolver.py:26
executable     LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo  holder_resolver.py:27
executable     Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB  holder_resolver.py:28
executable     JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4  holder_resolver.py:29, wallet_graph.py:21
executable     TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA  holder_resolver.py:30, wallet_graph.py:18
executable     TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb  holder_resolver.py:31
executable     11111111111111111111111111111111  holder_resolver.py:32, holder_resolver.py:67, wallet_graph.py:17
executable     ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL  wallet_graph.py:19
13/13 executable
```

The mistyped literals are not repeated in this record; the tree holds none of them (a grep for each returns nothing).

## Round 2 — 2026-10-01

Source: `2fb1ed3` on `main`, installed from a local git URL into a fresh venv. Host: Windows 11, Git Bash. `$TMP` is a scratch directory, `$REPO` is the checkout, `$CHAOS_HOME` is `$TMP/home`, and every skills install ran under a fake `~` at `$TMP/fakehome`. Each block names the command that produced it. Output is trimmed to the first lines; exit codes are as run.

### R2.1 Fresh venv, install, onboard

Command: `python -m venv $TMP/ct && $TMP/ct/Scripts/python -m pip install -q git+file://$REPO` — exit 0. The URL form that worked is `git+file://$REPO`, as in round 1.

Command: `chaos onboard --home $TMP/home --rpc-url https://api.mainnet-beta.solana.com --yes` — exit 0.

```
CHAOS_HOME resolves to $CHAOS_HOME (chosen by --home)
wrote $CHAOS_HOME/.env with SOLANA_RPC_URL
chaos-trader is set up in $CHAOS_HOME
Next: CHAOS_HOME=$CHAOS_HOME chaos token So11111111111111111111111111111111111111112
```

Command: `chaos --version` — exit 0.

```
chaos-trader 0.1.0
```

Command: `chaos help` — exit 0. First lines:

```
☄️ Chaos simple commands

Commands, run as `chaos <command>`:
- onboard                       (create the home, copy the code and seed roster, write .env)
- update                        (refresh the code in the home; data, roster, config and .env are kept)
```

The list includes the line `skills list | install --for claude|codex|hermes|all  (shipped agent skills; ...)`.

### R2.2 Commands on the public RPC, before any ingest

All with `CHAOS_HOME=$TMP/home`, `SOLANA_RPC_URL=https://api.mainnet-beta.solana.com`, no Helius key.

| Command | Exit | First line of output |
|---|---|---|
| `chaos token So11111111111111111111111111111111111111112` | 0 | `☄️ 🟡 TOKEN READ` then `$SOL · So1111…1112`, `ENTRY GATE: study` |
| `chaos sweep --limit 5` | 0 | `☄️ TREND SWEEP` then `Mode alpha · Scanned 45 · ranked 5 · deep 1 · filtered 0 · X off` |
| `chaos paper-report` | 0 | `No paper activity yet. Run the ingest job and the paper tick first (see "Running it on a schedule" in the README).` |
| `chaos sweep --fast` | 0 | `The roster tape is empty until the ingest job has run. Run the ingest job, or use chaos sweep without --fast for the trending sweep.` |
| `chaos wallets --discover 5` | 0 | `Wallet discovery needs transfer edges from the Helius wallet API. Set HELIUS_API_KEY and run the ingest job first; nothing to do yet.` |
| `chaos paper` | 0 | `No paper database yet. Run the paper tick once (see "Running it on a schedule").` |

The three empty-state commands tell the user the next step and exit 0.

### R2.3 Elite ingest on the standard RPC path, bounded

The wrapper `$CHAOS_HOME/scripts/chaos_alpha_elite_ingest.py` has no limit flag; it runs the pipeline with `--history-limit 50 --pages 1` for all 12 roster wallets. To keep the run small, the pipeline it wraps was run directly on a roster file holding one seed wallet (the first, tier A), with `--history-limit 5`.

Command (from `$CHAOS_HOME/trading`, public RPC, exit 0, 3.6 seconds):

```
python scripts/elite_wallet_pipeline.py ingest --roster $TMP/one-roster.json --history-limit 5 --pages 1
```

```
☄️ ALPHA ELITE INGEST · ok
Roster seed-v1 · 1 wallets
Attempted 1 · succeeded 1 · failed 0
Txs 5 · raw preserved 5 · events seen 5 · new 5 · duplicates 0
Latest event 2026-09-01T23:03:12+00:00
smart-wallet DB only · no paper/X/Dex/execution
```

Command: `SELECT source_id, COUNT(*) FROM wallet_token_events GROUP BY 1` on `trading/db/smart_wallets.sqlite`:

```
[('solana_rpc', 5)]
```

Command: `SELECT metadata_json FROM wallet_token_events LIMIT 1` (the column is `metadata_json`):

```
{"fetch": "standard_rpc", "native_sol_delta": 0.0, "non_sol_mint_count": 1, "wsol_delta": 0.0}
```

Five rows with source id `solana_rpc`, none with `helius_rpc`, and `metadata.fetch` is `standard_rpc`. The ingest therefore worked without a Helius key for one seed wallet and 5 transactions on the public RPC; the full 12-wallet fill is not verified.

Command: `chaos paper-report` — exit 0. Still empty, because no paper tick has run:

```
No paper activity yet. Run the ingest job and the paper tick first (see "Running it on a schedule" in the README).
```

Command: `chaos sweep --fast` — exit 0. No longer says the tape is empty. It reports no candidates because the five events are 29 days old:

```
☄️ ⚡ ALPHA TAPE SWEEP
Candidates 0 · latency 1.92ms · local-alpha-tape
FRESHNESS: **stale** · wallet 29d · signal unknown
No local candidates surfaced.
```

Not verified in this round: the full 12-wallet wrapper run on the public RPC, which the README says can reach its 540-second bound.

### R2.4 Skills install into all three harnesses

Command: `chaos skills list` — exit 0. Twelve skills:

```
chaos-alpha-decoder, chaos-crypto-trader, chaos-execution-control, chaos-paper-autopilot-operations,
chaos-position-aware-alpha, chaos-risk-engine, chaos-smart-money-signals, chaos-solana-intelligence,
chaos-trade-journal, chaos-wallet-attribution, gmgn-alpha-intelligence, solana
```

Command: `chaos skills install --for all --dry-run` — exit 0. First line `dry run: nothing is written`, then 36 target directories: 12 under `~/.claude/skills/<name>`, 12 under `~/.agents/skills/<name>`, and 12 under `~/.hermes/skills/blockchain/<name>`. This ran with the real home and wrote nothing (`ls ~/.claude/skills | grep -c chaos` printed `0` afterwards).

Command: `HOME=$TMP/fakehome USERPROFILE=$TMP/fakehome chaos skills install --for all` — exit 0. The fake home had no `.hermes`, so Hermes got the shared directory and this note:

```
No $TMP/fakehome/.hermes; installing for Hermes into $TMP/fakehome/.agents/skills.
Add this to your Hermes config.yaml so Hermes reads that directory:
skills:
  external_dirs:
    - ~/.agents/skills
```

Command: `find $TMP/fakehome -name SKILL.md | sort` (shown relative to the fake `~`, 24 files, two trees of 12):

```
~/.agents/skills/chaos-alpha-decoder/SKILL.md
~/.agents/skills/chaos-crypto-trader/SKILL.md
~/.agents/skills/chaos-execution-control/SKILL.md
~/.agents/skills/chaos-paper-autopilot-operations/SKILL.md
~/.agents/skills/chaos-position-aware-alpha/SKILL.md
~/.agents/skills/chaos-risk-engine/SKILL.md
~/.agents/skills/chaos-smart-money-signals/SKILL.md
~/.agents/skills/chaos-solana-intelligence/SKILL.md
~/.agents/skills/chaos-trade-journal/SKILL.md
~/.agents/skills/chaos-wallet-attribution/SKILL.md
~/.agents/skills/gmgn-alpha-intelligence/SKILL.md
~/.agents/skills/solana/SKILL.md
~/.claude/skills/ (the same 12 names)
```

With no `~/.hermes` present, Hermes shares the `~/.agents/skills` tree with Codex, so two trees hold the three harnesses. The `~/.hermes/skills/blockchain/<name>` layout appears only in the dry run, because the fake home had no `.hermes` directory.

Command: the same install run a second time — exit 0, same output.

Command: `chaos skills install --for all --project`, run in `$TMP/proj` — exit 0. First line: `Hermes reads ./.agents/skills only after you run `hermes skills trust` in this project.` It created 12 skills under `.claude/skills` and 12 under `.agents/skills`. The flag needs `--for`; `chaos skills install --project` alone exits 2 with `the following arguments are required: --for`.

### R2.5 Program ids

Command: `python tools/check_program_ids.py` (public RPC, exit 0). Last line `13/13 executable`; the five in the plan are among them:

```
executable     CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK  holder_resolver.py:24
executable     675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8  holder_resolver.py:25, pumpfun_launch_read.py:20, wallet_graph.py:23
executable     CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C  holder_resolver.py:23, pumpfun_launch_read.py:21, wallet_graph.py:24
executable     6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P  holder_resolver.py:21, pumpfun_launch_read.py:18, wallet_graph.py:20
executable     pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA  holder_resolver.py:22
13/13 executable
```

### R2.6 cp1252 stdio

Command: `python -m unittest tests.test_stdio -v` from `$REPO` — exit 0:

```
test_child_output_round_trips_through_run_raw_with_pythonioencoding_unset ... ok
test_help_and_fast_token_run_with_pythonioencoding_unset ... ok
test_help_runs_under_cp1252_stdio ... ok

Ran 3 tests in 1.234s

OK
```

### R2.7 One headless run per harness

All three ran from `$TMP/proj` after the project install in R2.4, with `CHAOS_HOME=$TMP/home` exported and the `chaos` binary not on the harness PATH unless stated.

**Claude Code: verified.** Command: `claude -p "Using the chaos-solana-intelligence skill, give me the verdict card for So11111111111111111111111111111111111111112. Run the script the skill names and paste the card." --allowedTools "Bash,Read,Glob,Grep" --max-turns 12` — exit 0. The reply says it ran `chaos analyze token So111…1112 --no-x` through `chaos_cmd.py` in `$CHAOS_HOME`, and pasted a card ending in a `Files:` line:

```
☄️ 🟡 DEEP TOKEN ANALYSIS
$SOL · `So1111…1112`
MODE: **unknown**
ENTRY GATE: **study**
POSITION: **no-position**
...
Files: $CHAOS_HOME/trading/alpha/sweeps/2026-10-01/token_event_SOL_So111111_20261001T152610Z.md
Advisory + paper only. No wallet, signing, routing, or live execution.
```

The artifact file name carries a timestamp that the script generated, so the card came from running the script. The card also showed `HOLDERS: unavailable (rate limited)`, which is the public RPC without a Helius key.

**Codex: verified on the fourth attempt, after four setup changes.** Codex CLI 0.156.1.

Two of the four changes come from this machine's Codex config: the default model id set in the local config (`gpt-6.1-sol`, fixed with `-m gpt-5.5`) and `shell_environment_policy.inherit = "core"` (fixed with `-c 'shell_environment_policy.inherit="all"'`, or by setting `CHAOS_HOME` in that policy). Two will hit anyone: the workspace-write sandbox blocks network by default (needs `-c 'sandbox_workspace_write.network_access=true'` or the same key in `config.toml`), and `chaos` is not on the PATH inside the sandbox (needs the venv's `Scripts` or `bin` directory exported on PATH).

1. `codex exec "<prompt>" -o $TMP/codex-out.txt` — exit 1, no output file. The configured default model was refused:

```
ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."}}
```

2. The same command with `-m gpt-5.5` — exit 0. Codex listed the 12 skill names and read the skill file from the project's `.agents/skills`, but produced no card. Its shell did not have the venv on PATH, and its workspace-write sandbox blocked outbound sockets: `RPC connection error: [WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions`.
3. The same command with `-m gpt-5.5 -c 'sandbox_workspace_write.network_access=true'` and the venv on PATH — exit 0. It ran `chaos analyze token ... --no-x` and got `No install at ~/.chaos-trader. Run chaos onboard first (or set CHAOS_HOME to an existing install).` The local Codex config sets `shell_environment_policy.inherit = "core"`, which drops `CHAOS_HOME` from the shell it starts.
4. The same command with `-m gpt-5.5 -c 'sandbox_workspace_write.network_access=true' -c 'shell_environment_policy.inherit="all"'` and the venv on PATH — exit 0. First lines of the reply, then the card:

```
Using `chaos-solana-intelligence`, I ran:

chaos analyze token So11111111111111111111111111111111111111112 --no-x

**Skills Available**
imagegen
openai-docs
plugin-creator
skill-creator
skill-installer
chaos-alpha-decoder
chaos-crypto-trader
chaos-execution-control
chaos-paper-autopilot-operations
chaos-position-aware-alpha
chaos-risk-engine
chaos-smart-money-signals
chaos-solana-intelligence
chaos-trade-journal
chaos-wallet-attribution
gmgn-alpha-intelligence
solana
(list continues with the other user-level skills; all 12 chaos names were present)
```

```
☄️ 🟡 DEEP TOKEN ANALYSIS
$SOL · So1111…1112
MODE: unknown
ENTRY GATE: study
...
Artifact written by the analyzer: $CHAOS_HOME/trading/alpha/sweeps/2026-10-01/token_event_SOL_So111111_20261001T153019Z.md
```

The skill name appears in Codex's own list and the card came from running the script. What this does not show is a default Codex setup working: the card needed a supported model, network access in the sandbox, `chaos` on PATH, and `CHAOS_HOME` passed through to the shell.

**Hermes: verified, listing only.** No Hermes chat turn was run, because that needs a model key. `HERMES_HOME` was pointed at an empty scratch directory so the real Hermes state was not touched.

1. `hermes skills trust` in `$TMP/proj` before `git init` — the command exits 0 (re-checked against the raw run) even though it refuses with this message: `Not inside a git checkout. Run from a project directory or pass the project root path explicitly.` Project skills need a git checkout.
2. `git init` in `$TMP/proj`, then `hermes skills trust` — exit 0:

```
Trusted: $TMP/proj
12 project skill(s) will load in sessions started inside this repo (they take precedence over same-named profile skills).
```

3. `hermes skills list` — exit 0. The table shows 12 enabled local skills, and the summary line is `0 hub-installed, 0 builtin, 12 local — 12 enabled, 0 disabled`. The 12 are the names in R2.4:

```
│ chaos-alpha-decoder              │          │ local  │ local │ enabled │
│ chaos-crypto-trader              │          │ local  │ local │ enabled │
│ chaos-execution-control          │          │ local  │ local │ enabled │
│ chaos-paper-autopilot-operations │          │ local  │ local │ enabled │
│ chaos-position-aware-alpha       │          │ local  │ local │ enabled │
│ chaos-risk-engine                │          │ local  │ local │ enabled │
│ chaos-smart-money-signals        │          │ local  │ local │ enabled │
│ chaos-solana-intelligence        │          │ local  │ local │ enabled │
│ chaos-trade-journal              │          │ local  │ local │ enabled │
│ chaos-wallet-attribution         │          │ local  │ local │ enabled │
│ gmgn-alpha-intelligence          │          │ local  │ local │ enabled │
│ solana                           │          │ local  │ local │ enabled │
```

Not verified: Hermes loading a skill in a model turn, and the `skills.external_dirs` route for `~/.agents/skills`.

### Round 2 summary

| Claim | Status | Command |
|---|---|---|
| Fresh install, onboard, version, help | verified | R2.1 |
| Public-RPC commands and empty states | verified | R2.2 |
| Ingest on the standard path writes `solana_rpc` rows with `metadata.fetch = "standard_rpc"` | verified for one wallet and 5 transactions | R2.3 |
| Full 12-wallet ingest on the public RPC | not verified | none run |
| `chaos skills install --for all` into fake home, idempotent | verified | R2.4 |
| Program ids executable | verified, 13 of 13 | R2.5 |
| cp1252 stdio | verified, 3 tests | R2.6 |
| Claude Code skill run | verified | R2.7 |
| Codex skill run | verified with four setup changes | R2.7 |
| Hermes skill list | verified, listing only | R2.7 |

## Round 4 — 2026-10-01: X search provider

### R4.1 Live `--with-x` call with an xAI key

Not verified: no XAI_API_KEY on this machine. It was absent from the process environment and from `~/.chaos-trader/.env` (presence checked only, no value printed). No xAI request was made in this round, and the recorded fixture `fixtures/xai_x_search_response.json` stays synthetic.

The commands that would run, with the key copied into a throwaway home only:

```
export CHAOS_HOME="$TMP/x-home"
chaos onboard
printf 'XAI_API_KEY=%s\n' "$XAI_API_KEY" >> "$CHAOS_HOME/.env"
X_SEARCH_PROVIDER=xai  chaos token So11111111111111111111111111111111111111112 --with-x
X_SEARCH_PROVIDER=none chaos token So11111111111111111111111111111111111111112 --with-x
rm -rf "$TMP/x-home"
```

Expected from the second run, on stderr: `X search: no provider configured (set XAI_API_KEY or HERMES_AGENT_SRC)`, with the card identical to a run without `--with-x`. The first run is the only way to confirm the fixture's shape against a real answer and to see real citation URLs.

### R4.2 What was verified without a key

The provider rules run against the real analyzer, paper engine, `chaos` router and paper autopilot, with only `urllib.request.urlopen` replaced (`test_x_integration.py`, no network):

| Claim | Status | Check |
|---|---|---|
| `--with-x` with provider `none` gives the same decision JSON as X off, plus one stderr notice | verified | `test_with_x_and_no_provider_matches_x_off_byte_for_byte` |
| An unknown `X_SEARCH_PROVIDER` such as `grok` prints `X search: unknown provider 'grok' (use hermes, xai, or none)` and behaves as `none` | verified | `test_unknown_provider_prints_its_own_notice_and_is_x_off` |
| `chaos_paper_autopilot.py --with-x` with no provider prints the same notice | verified | `test_autopilot_with_x_and_no_provider_prints_the_notice` |
| `trending_token_sweep.py --x` reports `x_enabled` from the provider decision | verified | `test_sweep_x_flag_reports_the_provider_aware_decision` |
| HTTP 429 and 503 are unavailable, charged once, verdict unchanged | verified | the two `http_429` / `http_503` tests, analyzer and budget |
| An answer without citations is no evidence | verified | `test_catalyst_answer_without_citations_is_no_evidence` |
| A real xAI answer has the fixture's shape | not verified | needs R4.1 |
| The HTTPError path leaves no unclosed response | verified | `python -W error::ResourceWarning -X dev -m unittest test_x_integration.XRuleTests test_x_integration.XBudgetTests` is clean; with the `exc.close()` line removed the same command prints `ResourceWarning: Implicitly cleaning up <HTTPError 429: 'Too Many Requests'>` |


## Round 5 — 2026-10-01: Hermes profile install on a local host

Hermes Agent v0.21.0 on a Windows host, with a local llama.cpp server as the model (an OpenAI-compatible endpoint on the loopback address; Hermes requires a context window of at least 64K, so the server was started with a 64K context). No Nous subscription was involved. The repository was at ffc4c85, after the distribution files were added.

### R5.1 Install

```
hermes profile install <path to the repository checkout> --name chaos-test -y
```

Verified: the installer printed the manifest (name, version 0.1.0, description, author, `Hermes >=0.21.0`), the four env vars from `env_requires` as optional, and the notice that shipped cron jobs will not run automatically. The profile directory then held `distribution.yaml`, `SOUL.md`, `config.yaml`, `skills/` with 12 `SKILL.md` files, `cron/jobs.json` with three jobs, and an `.env.EXAMPLE` generated from the manifest.

Also verified, and worth knowing: on install, Hermes copies every top-level entry of the source into the profile except its own user-owned names (`.env`, `auth.json`, `memories/`, `sessions/`, `logs/`, `cache/`, and a few others). A profile installed from this repository therefore holds a copy of the whole repository, including `chaos_trader/`, `tests/`, and `docs/`. That copy is inert: the `chaos` command runs from the pip-installed package, not from the profile. A git-URL install drops `.git` first; a local-directory install does not, and also copies untracked files, so install from the git URL rather than from a working checkout.

`hermes -p chaos-test cron list` printed `No scheduled jobs.`; `hermes -p chaos-test cron list --all` listed the three jobs as `[paused]`, each with its schedule, `Deliver: origin`, and its attached skill. The plain `cron list` hides disabled jobs.

### R5.2 Package and home

```
<Hermes venv python> -m pip install -e <repository>
chaos --version                                   → chaos-trader 0.1.0
chaos onboard --home <profile dir> --rpc-url https://api.mainnet-beta.solana.com --yes
```

Verified: onboard reported `chosen by --home`, wrote `.env` with `SOLANA_RPC_URL`, and created `trading/{alpha,config,db,reports,state}` with `roster.json`, `paper_autopilot.yaml`, and `paper_autopilot.defaults.yaml` under `trading/config`. `SOUL.md`, `config.yaml`, `skills/`, and `cron/` in the profile were untouched. For the test the package was installed editable from the checkout into the Python that Hermes runs on; a user runs `pip install chaos-trader` into that same Python.

### R5.3 A skill run through the agent, with no extra environment

The shipped `config.yaml` was left exactly as it ships (`provider: custom`, `base_url: http://127.0.0.1:8080/v1`, `default: your-local-model`); the local server accepts any model name. No `CHAOS_HOME` was set.

```
hermes -p chaos-test chat --query-file <prompt> --oneshot -Q
```

Prompt: use the `chaos-solana-intelligence` skill to produce the verdict card for `So11111111111111111111111111111111111111112` by running the command the skill names, and reply with the command and its unchanged output.

Verified: the agent ran `chaos token So11111111111111111111111111111111111111112` through its terminal tool and returned the token card (mode unknown, entry gate `study`, holders unavailable because the public RPC rate-limited the holder read, the paper-only boundary line at the end). The command found its state root through `HERMES_HOME`, which Hermes sets to the profile directory for the agent process and its subprocesses; nothing else was configured. Exit code 0.

### R5.4 A cron job's command, run by hand inside the profile

```
HERMES_HOME=<profile dir> chaos run chaos_paper_autopilot_tick --timeout 300
```

Verified: exit 0; `Discovered: 5`, `Decisions: 2`, `Open positions checked: 0`; `trading/db/paper_autopilot.sqlite` in the profile then held 5 candidate rows and 3 event rows. This is the same command the shipped `chaos-paper-tick` cron prompt tells the agent to run.

### Round 5 summary

| Claim | Status |
|---|---|
| `hermes profile install` of this repository produces a working profile with the manifest, SOUL, config, 12 skills, and 3 paused cron jobs | verified |
| The profile directory is the state root for `chaos` with no extra environment | verified (R5.3) |
| A skill's command runs through the Hermes terminal tool and returns the card | verified (R5.3) |
| The cron prompts' commands work inside the profile | verified for the paper tick (R5.4); the ingest and the report were not run in this round |
| The shipped placeholder model block is usable as-is against a local OpenAI-compatible server | verified |
| Install from a git URL | not verified in this round (the repository is private); the local-directory path was used |


## Round 6 — 2026-10-02: the roster reaches the token read

The home roster is now a watch source for the slow token read: tiers A and B count as quality hits, tier C as a scout hit. This round tried to show a roster hit live on one mint, then proved the matching path offline.

A roster wallet shows as a hit only when it is among the token's sampled holders, which are up to its 20 largest accounts plus a 10-row token-account sample, or among pump.fun's top signers. On a rate-limited public RPC the holder sample is often unavailable; in this round it was unavailable in every live run.

### R6.1 The mint

`CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7` ($X7). Roster tier C wallet `3AoQmpHaVTV3tcZqWAFrYgtCPqkMkdgxib9MjtvDRsom` held 234,250 of it on 2026-10-02. Whether that is one of the mint's 20 largest accounts could not be confirmed. The search checked four live roster wallets' twelve largest holdings against each mint's 20 largest accounts on the public RPC and found no hit, but several `getTokenLargestAccounts` reads were rate-limited, so the search is not conclusive.

### R6.2 Before and after, live

Both cards come from the same throwaway home, onboarded on the public RPC with the seed v1 roster (12 wallets, which include the tier C wallet above), and the same command:

```
chaos token CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7 --no-x
```

Before, at 21166c2, 06:12 UTC:

````text
☄️ 🟡 TOKEN READ
$X7 · `CdhZy8…Kfx7`
MODE: **conviction trench**
ENTRY GATE: **study**
POSITION: **no-position**
CATALYST: none
FLOW: clean-flow · fake-flow none · conversion unknown
MC $315.4K · Liq $114.8K · V/L 1.36x
WALLETS: Q 0 · timing none · owner 0/0
HOLDERS: unavailable (rate limited)
WHY:
- structure exists but wallet/source confirmation is weak
RISK:
- holder data unavailable (rate limited)

📋 Copy CA
```text
CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7
```
🧭 Open: [DEX](https://dexscreener.com/solana/CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7) · [SOL](https://solscan.io/token/CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7)
⚡ Next
```text
analyze token CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7
```
Advisory + paper only. No wallet, signing, routing, or live execution.
````

After, at d69c0d7. One attempt and three retries ran at 06:43, 06:45, 06:47, and 06:49 UTC, each retry starting 60 s after the previous run ended. All four cards showed `HOLDERS: unavailable (rate limited)`. The last one:

````text
☄️ 🟡 TOKEN READ
$X7 · `CdhZy8…Kfx7`
MODE: **conviction trench**
ENTRY GATE: **study**
POSITION: **no-position**
CATALYST: none
FLOW: clean-flow · fake-flow none · conversion unknown
MC $305.8K · Liq $113.0K · V/L 1.205x
WALLETS: Q 0 · timing none · owner 0/0
HOLDERS: unavailable (rate limited)
WHY:
- structure exists but wallet/source confirmation is weak
RISK:
- holder data unavailable (rate limited)

📋 Copy CA
```text
CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7
```
🧭 Open: [DEX](https://dexscreener.com/solana/CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7) · [SOL](https://solscan.io/token/CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7)
⚡ Next
```text
analyze token CdhZy8wrRxNxoV8HtoByXKN7mvS46avtuZ1b39tKfx7
```
Advisory + paper only. No wallet, signing, routing, or live execution.
````

Not verified live: holder sample rate-limited. In every run, before and after, `getTokenLargestAccounts` returned HTTP 429, so the 20 largest accounts were not read. The token-account sample returned 10 holders, none of them a roster wallet. The two cards differ only in market numbers. The read's JSON record differs in one field: `watch_wallet_file_count` was 0 before and 12 after, the twelve wallets of that home's roster. The live mint's roster holder is tier C, so even a successful holder sample would have shown the hit in the analysis file, not in the card's Q count.

### R6.3 Offline proof

A short script runs the real analyzer with no patching and no network. It points `CHAOS_HOME` at a fresh temporary home before the analyzer is imported, copies the package's seed v2 roster to the home's roster path as `chaos onboard` does, and gives `extract_wallet_touch` a holder sample with one roster tier A wallet and one unrelated address. It then runs `classify` on that result and on one without the roster wallet. Output, labelled offline:

```
home roster: seed-v2, 9 wallets; tier-A wallet 4hwPam…ZAi8
holder sample: 4hwPam…ZAi8 (roster A), 111111…1111 (unrelated)
watch_wallet_file_count 9
watch_wallet_hit_count 1 · scout_wallet_hit_count 0
hit: 4hwPam…ZAi8 kind quality role roster-A source roster.json
classify with the roster hit: score 4 · reasons ['watch wallet touched sample']
  validation positives ['quality wallet/sample hits: 1']
classify without it:          score 0 · reasons []
offline proof: all assertions passed
```

The same check is kept as a permanent test, `test_seed_tier_a_wallet_in_a_holder_sample_adds_the_watch_score` in `test_watch_roster.py`.

### R6.4 `chaos wallets --review` on that home

```
No ingest yet. Run chaos run chaos_alpha_elite_ingest first.
```

Exit 0. That home has had no ingest run, and this round did not run one, so the roster review card itself was not shown.

### Round 6 summary

| Claim | Status |
|---|---|
| A roster wallet among a token's sampled holders is a watch hit, with tier A as a quality hit | verified offline (R6.3) |
| A quality roster hit adds 4 to the read's score and counts as wallet validation | verified offline (R6.3) |
| The token read loads the home roster as watch wallets | verified live: `watch_wallet_file_count` 0 before, 12 after (R6.2) |
| A live token read shows a roster hit | not verified live: holder sample rate-limited in all four runs (R6.2) |
| `chaos wallets --review` prints one line and exits 0 on a home with no ingest | verified (R6.4) |
| The roster review card on a home with ingest data | not verified in this round |

## Round 7 — 2026-10-02: a skill loaded cold in Claude Code

This round installed the skills into an empty project with the package's own installer and asked Claude Code, headless, to use one of them. Unlike section 3, the skills came from `chaos skills install`, not a copy, and each installed `SKILL.md` carried the new Prerequisites block. HOME was not redirected, so Claude Code used its normal login. One attempt was run, at 1539d6f plus this change.

Setup, with `$PROJECT` an empty folder and `$HOME_DIR` a throwaway home:

```
chaos onboard --home $HOME_DIR --yes
cd $PROJECT && CHAOS_HOME=$HOME_DIR chaos skills install --for claude --project
```

Both exited 0. The install wrote the 12 skills to `.claude/skills/`.

Command, run from `$PROJECT` with `CHAOS_HOME=$HOME_DIR` and the `chaos` console script on `PATH`:

```
claude -p "Using the chaos-solana-intelligence skill, print the verdict card for So11111111111111111111111111111111111111112 by running the command it names, and reply with the command and its output unchanged" --allowedTools "Bash,Read,Glob,Grep"
```

Exit code 0, nothing on stderr. The reply is 38 lines. The first 30, unchanged:

````text
Command (from the skill's Fast Routing table, "token/CA read"):

```text
chaos token So11111111111111111111111111111111111111112
```

Output, unchanged:

```text
☄️ 🟡 TOKEN READ
$SOL · `So1111…1112`
MODE: **unknown**
ENTRY GATE: **study**
POSITION: **no-position**
CATALYST: none
FLOW: dead-churn · fake-flow high · conversion low
MC ? · Liq $37.63M · V/L 0.107x
WALLETS: Q 0 · timing none · owner 0/0
HOLDERS: unavailable (rate limited)
WHY:
- mode unknown; collect more evidence
RISK:
- extreme micro-churn: 73,033 txns avg $55
- validation missing: no watch-wallet validation

📋 Copy CA
```text
So11111111111111111111111111111111111111112
```
🧭 Open: [DEX](https://dexscreener.com/solana/So11111111111111111111111111111111111111112) · [SOL](https://solscan.io/token/So11111111111111111111111111111111111111112)
````

The remaining lines are the card's `Next` block, its final boundary line, and one closing sentence from the model.

Evidence the command ran, not model memory: the throwaway home gained a token-event JSON and markdown file under `trading/alpha/sweeps/2026-10-02/`, stamped 08:43:52 UTC, a DexScreener cache file for the mint, and `trading/db/signal_ledger.sqlite`. None of them existed after onboard. The card's numbers also differ from section 3's run of the same mint.

The model named the skill's Fast Routing row "token/CA read", which routes to `chaos token <MINT>`, so it read the installed `SKILL.md`. The Skill tool was not in `--allowedTools`, so this run does not show the skill being picked from Claude Code's skill registry.

### Round 7 summary

| Claim | Status |
|---|---|
| `chaos skills install --for claude --project` writes all 12 skills into an empty project | verified |
| A headless Claude Code run follows the installed skill to its command and returns the card | verified: exit 0, sweep files written in the throwaway home |
| Claude Code picks the skill from its registry by name | not verified: the Skill tool was not allowed in this run |

## Round 8 — 2026-10-02: the public release, installed from PyPI and from the public repository

Run after the repository went public and `v0.1.0` was published. The tag ran `.github/workflows/publish.yml` (run 37079193513): the build job checked the tag against both version files, ran the tests, and built the wheel and sdist; the publish job waited for the owner to approve the `pypi` environment, then uploaded through the pending trusted publisher. PyPI lists `chaos-trader` 0.1.0 with `chaos_trader-0.1.0-py3-none-any.whl` and `chaos_trader-0.1.0.tar.gz`.

### 1. `pip install chaos-trader` into an empty venv

Command: `python -m venv $TMP/pypi && $TMP/pypi/Scripts/python -m pip install -q chaos-trader` on Windows, Python 3.14. Exit 0. `pip show` reports version 0.1.0 from `site-packages`.

    CHAOS_HOME=$TMP/home chaos onboard --yes
    -> wrote $TMP/home/.env with SOLANA_RPC_URL
    -> chaos-trader is set up in $TMP/home
    chaos --version
    -> chaos-trader 0.1.0

From outside the repository (so the import cannot pick up the source tree), the installed package reports 14 skill folders under `chaos_trader/skills/blockchain/` and `importlib.metadata.files` lists no `test_*` module: the wheel carries the skills and no tests, as `tests/test_wheel_contents.py` pins.

### 2. `hermes profile install github.com/AIEngineerX/chaos-trader --alias`

Hermes read the distribution from the public repository, showed the four optional environment variables and the cron warning, and after confirmation reported `Installed 'chaos-trader' v0.1.0` at `<hermes>/profiles/chaos-trader`. `hermes profile list` shows the profile with alias `chaos-trader` and distribution `chaos-trader@0.1.0`. `hermes -p chaos-trader cron list --all` lists `chaos-elite-ingest`, `chaos-paper-tick`, `chaos-paper-report`, and `chaos-outcome-tick`, all paused, and says the gateway is not running. The profile folder holds the 14 skills.

Observed: the git-URL install copies the whole clone into the profile, including its `.git` folder (the local-directory install in Round 5 did the same). The repository is public with the single-root history, so nothing private travels with it; it costs disk space only.

The scratch profile was deleted after the check (`hermes profile delete chaos-trader`, confirmed by name).

### 3. Install lines

After the upload, the README, the skills' Prerequisites block, `chaos update`, and the `chaos mcp` hint switched to `pip install chaos-trader` (commit `46ea80c`); `tests/test_readme_rules.py` now pins those lines and the one remaining git URL for the unreleased `main` branch.

### Round 8 summary

| Claim | Status |
|---|---|
| A `v*` tag publishes to PyPI through trusted publishing with an owner approval step | verified: run 37079193513, both files on PyPI |
| `pip install chaos-trader` in an empty venv installs 0.1.0, onboards a home, ships 14 skills and no tests | verified |
| `hermes profile install <public git URL> --alias` installs the profile with four paused cron jobs | verified |
| The git-URL install leaves no `.git` folder in the profile | not the case: the clone's `.git` is copied; harmless on the public history |
