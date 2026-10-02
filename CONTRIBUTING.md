# Contributing

Thanks for looking. This file says how to get a change in and what the gates are.

## Before you start

- Open an issue first for anything bigger than a bug fix, so the shape is agreed before the code exists.
- The package is read-only by design. Changes that add signing, wallet connection, swap routing, or fund movement are out of scope here and will be closed. See `BOUNDARY.md`.
- No performance claims anywhere in docs or code comments. If you measured something, put the command and the number next to each other.

## Setup

    git clone https://github.com/AIEngineerX/chaos-trader
    cd chaos-trader
    python -m venv .venv
    .venv/bin/pip install -e .        # Windows: .venv\Scripts\pip install -e .
    make verify PY=.venv/bin/python   # Windows: make verify PY=.venv/Scripts/python

`make verify` is the one command. Pass `PY` so it uses the venv; without it, `make` uses whatever `python` is first on PATH.

## The gates every change must pass

`make verify` runs these in order:

    python tools/safety_check.py      # no secrets, no local user paths
    make vendor-check                 # no private names (checked by digest)
    make compile                      # every pipeline script compiles
    make test                         # both suites, strict warnings
    make gitleaks-scan                # full-history secret scan
    make history-scan dep-audit       # private names in every past blob; known vulnerabilities in the dependencies (needs pip-audit)

`make test` runs both suites with `-W error::ResourceWarning`. A warning is a failure. It needs a git checkout: tests in four files shell out to git, so they fail in a source archive.

`make verify` needs `gitleaks` on PATH, `pip-audit` in the venv (`pip install pip-audit`), and network access, because the dependency audit queries a vulnerability database. With `build` installed as well, the audit reads the same wheel metadata CI audits. On Windows you also need `make`, from MSYS2 or the Git for Windows SDK. Without them, run the commands above by hand with the venv's Python.

CI runs the same checks on Python 3.11 and 3.14, then builds the wheel, checks its contents, and runs a clean-home onboard. `SECURITY.md` lists the full release gate.

## Rules for code

- Tests use real SQLite files, real files, and the real CLI. They replace what leaves the process: the network (`urlopen`, the no-redirect opener, `rpc_request`, and the paper autopilot's market fetch and analysis subprocess), DNS, the external `gmgn-cli` binary, and clocks. A few stub one in-package step to test its caller alone; keep that rare.
- Every new env var gets a row in the README Configuration table; a test checks that.
- Every new verb goes into `chaos help`; a test checks that too.
- No new runtime dependency without an issue first. Today there is one: PyYAML.
- Scripts under `chaos_trader/trading/scripts/` use flat imports and run from the installed package, through `chaos run <name>`; `CHAOS_HOME` holds state only. Keep it that way.

## Rules for docs

- Plain words. No marketing language; the README test bans a list of them.
- Every claim sits next to the command that checks it.
- Nothing may imply an executor exists today.

## Commits

- One logical change per commit, with a `feat:`, `fix:`, `docs:`, `test:`, or `chore:` prefix.
- Do not force-push to `main`.

## Releases

A release is cut by pushing a `vX.Y.Z` tag: `.github/workflows/publish.yml` builds the wheel and sdist and uploads them to PyPI by trusted publishing. The tag must equal the package version: the workflow refuses to build when the tag without its `v` differs from the version in `pyproject.toml`. `tests/test_distribution.py` keeps `pyproject.toml`, `chaos_trader/__init__.py`, and `distribution.yaml` on the same version.

## Reporting a security problem

See `SECURITY.md`. Do not open a public issue for a key leak or a signing path.
