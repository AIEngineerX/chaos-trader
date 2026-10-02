"""`chaos mcp`: a stdio MCP server whose read-only tools run the pipeline verbs with `--json`.

Each tool runs the verb the CLI would run, in the environment the CLI builds, and returns the envelope that
verb prints, so the CLI and the server share one implementation. Roster edits and the ingest stay on the
command line.
"""
# No `from __future__ import annotations`: mcp 1.12 reads the tool annotations as classes, not strings.
import re
import subprocess
import sys
from functools import partial
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

from chaos_trader.cli import SCRIPTS, _script_env

# The pattern chaos_cmd.MINT_RE checks; tests/test_mcp_server.py pins the two together.
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,88}$")
NOT_A_MINT = "not a Solana mint address"


def _mint(mint: str) -> str:
    mint = mint.strip()
    if not MINT_RE.match(mint):
        raise ValueError(NOT_A_MINT)
    return mint


def _x_flag(with_x: bool) -> str:
    return "--with-x" if with_x else "--no-x"


def build(home: Path) -> FastMCP:
    env = _script_env(home)

    def verb(*args: str) -> str:
        # stdin and stdout carry the MCP stream, so the verb gets neither; a failing verb's one line becomes the tool error.
        proc = subprocess.run([sys.executable, str(SCRIPTS / "chaos_cmd.py"), *args, "--json"], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip() or f"chaos {args[0]} exited {proc.returncode}")
        return proc.stdout

    async def run(*args: str) -> str:
        # A token read takes minutes; a worker thread keeps the server answering other requests meanwhile.
        return await anyio.to_thread.run_sync(partial(verb, *args))

    server = FastMCP("chaos-trader", log_level="WARNING")  # no INFO line per request on the agent's stderr

    @server.tool()
    async def token_read(mint: str, with_x: bool = False) -> str:
        """Read one Solana token live and return the `chaos token --json` envelope."""
        return await run("token", _mint(mint), _x_flag(with_x))

    @server.tool()
    async def analyze_token(mint: str, with_x: bool = False) -> str:
        """Run the deeper one-token analysis and return the `chaos analyze --json` envelope."""
        return await run("analyze", _mint(mint), _x_flag(with_x))

    @server.tool()
    async def sweep(limit: int = 5, fast: bool = False) -> str:
        """Sweep trending Solana tokens, or only the local alpha tape when fast is true, and return the `chaos sweep --json` envelope."""
        return await run("sweep", "--limit", str(limit), *(["--fast"] if fast else []))

    @server.tool()
    async def strategy_paper(mint: str) -> str:
        """Return the paper loop's simulated decision for one Solana mint as the `chaos strategy-paper --json` envelope."""
        return await run("strategy-paper", _mint(mint))

    @server.tool()
    async def wallets_review(days: int = 14) -> str:
        """Return each roster wallet's last activity and track record from the local database as the `chaos wallets --review --json` envelope."""
        return await run("wallets", "--review", "--days", str(days))

    @server.tool()
    async def paper_report() -> str:
        """Return the paper book's outcomes and blockers as the `chaos paper-report --json` envelope."""
        return await run("paper-report")

    @server.tool()
    async def roster_list() -> str:
        """Return the wallets on the home roster with their tiers as the `chaos wallets --list --json` envelope."""
        return await run("wallets", "--list")

    return server


def serve(home: Path) -> None:
    build(home).run()
