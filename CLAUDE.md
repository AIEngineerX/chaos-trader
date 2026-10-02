# Claude Code in this repository

Read `AGENTS.md` first; every rule in it applies here. Two notes specific to this harness:

- Run `make verify PY=.venv/bin/python` (Windows: `PY=.venv/Scripts/python`) before you say a change is done. Report the gate output, not a summary of it.
- The skills under `skills/blockchain/` are the ones this repository ships; `chaos skills install --for claude --project` copies them into `.claude/skills/` for local use. Edit the originals, never the copies.
