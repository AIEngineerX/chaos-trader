"""The `chaos` command. `onboard`, `update`, `skills`, `run`, and `mcp` are handled here; every other verb runs the
package's chaos_cmd.py against CHAOS_HOME."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from chaos_trader import __version__
from chaos_trader.home import ENV_PRECEDENCE, chaos_home
from chaos_trader.onboard import ALREADY_SET_UP, ROSTER, onboard

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
NEEDS_TERMINAL = "chaos onboard needs a terminal for its two questions; pass --yes to use the defaults."
PUBLIC_RPC_NUDGE = "The public RPC does not serve the largest-holder read at all; a Helius or other provider key is needed to check the largest holders for watch wallets."
PACKAGE = Path(__file__).resolve().parent
SCRIPTS = PACKAGE / "trading" / "scripts"
JOBS = PACKAGE / "jobs"
RUN_USAGE = "chaos run <script> [args]"
MCP_EXTRA = 'chaos mcp needs the MCP extra: pip install "chaos-trader[mcp] @ git+https://github.com/AIEngineerX/chaos-trader"'

USAGE = """chaos-trader

  chaos onboard [--home DIR] [--rpc-url URL] [--helius-key KEY] [--yes]
      create DIR (default ~/.chaos-trader) with .env, the seed roster, the paper config, and the data folders
  chaos update [--home DIR]
      refresh the default paper config beside yours; code updates come from pip
  chaos run <script> [args...]
      run one pipeline script or job from the package by file name without .py; `chaos run` alone lists them
  chaos skills list
      name each shipped skill and say what it is for
  chaos skills install --for {claude,codex,hermes,all} [--project] [--dry-run] [--force]
      copy the skills to ~/.claude/skills, ~/.agents/skills, or <Hermes home>/skills/blockchain;
      --project writes ./.claude/skills and ./.agents/skills instead; --dry-run writes nothing;
      --force replaces a same-named skill this command did not install
  chaos token <mint> | sweep | analyze token <mint> | paper-report | outcomes | wallets | help
      run the pipeline commands; `chaos help` lists them
  chaos mcp
      serve seven tools over these commands to an MCP agent on stdio; no tool signs, sends, or edits
      the roster, and each writes only what its command writes under CHAOS_HOME; needs the mcp extra
  chaos --version
"""


def _resolve_home(flag: str | None) -> tuple[Path, str]:
    """The home `chaos onboard` will write to, and what chose it: --home, an env variable, or the default."""
    if flag:
        return Path(flag).expanduser(), "--home"
    for name in ENV_PRECEDENCE:
        if os.environ.get(name, "").strip():
            return chaos_home(), name
    return chaos_home(), "default"


def _prompt(label: str, default: str | None) -> str | None:
    shown = f" [{default}]" if default else ""
    value = input(f"{label}{shown}: ").strip()
    return value or default


def _onboard(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="chaos onboard", add_help=True)
    p.add_argument("--home", default=None)
    p.add_argument("--rpc-url", default=None)
    p.add_argument("--helius-key", default=None)
    p.add_argument("--yes", action="store_true", help="do not prompt; use flags and defaults")
    a = p.parse_args(argv)
    home, source = _resolve_home(a.home)
    resolved = Path(home).expanduser().resolve()
    print(f"CHAOS_HOME resolves to {resolved} (chosen by {source})")
    # Refuse a set-up home before asking anything; onboard() repeats this check as the backstop.
    if (resolved / ROSTER[1]).exists():
        print(ALREADY_SET_UP.format(home=resolved), file=sys.stderr)
        return 1
    # A closed fd 0 leaves sys.stdin as None, where input() raises RuntimeError: no terminal either.
    if not a.yes and sys.stdin is None:
        print(NEEDS_TERMINAL, file=sys.stderr)
        return 2
    key = a.helius_key
    try:
        if not a.yes:
            key = _prompt("Helius API key, needed for the wallet lane: wallet discovery (`smart_wallet_tracker.py`, `wallets --discover`) and deep wallet reads (`wallet_deep.py`) (blank to skip)", key)
        # SOLANA_RPC_URL beats HELIUS_API_KEY at read time, so a key with no explicit URL must leave
        # the URL unset; otherwise every read, including the wallet lane, goes to the public RPC.
        rpc = a.rpc_url or (None if key else DEFAULT_RPC_URL)
        if not a.yes:
            rpc = _prompt("Solana JSON-RPC URL (https)" + ("; blank to use the Helius key" if key else ""), rpc)
    except EOFError:
        print(NEEDS_TERMINAL, file=sys.stderr)
        return 2
    if rpc == DEFAULT_RPC_URL and not key:
        print(PUBLIC_RPC_NUDGE)
    env_report: list[str] = []
    done = onboard(home, rpc_url=rpc, helius_key=key or None, env_report=env_report, rpc_is_default=rpc == DEFAULT_RPC_URL and not a.rpc_url)
    for line in env_report:
        print(line)
    print(f"chaos-trader is set up in {done}")
    print(f"Next: CHAOS_HOME={done} chaos token So11111111111111111111111111111111111111112")
    return 0


def _update(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="chaos update")
    p.add_argument("--home", default=None)
    a = p.parse_args(argv)
    home = Path(a.home).expanduser() if a.home else chaos_home()
    done = onboard(home, rpc_url=None, helius_key=None, update=True)
    print(f"defaults refreshed in {done}")
    print("code is updated with: pip install -U git+https://github.com/AIEngineerX/chaos-trader")
    return 0


def _skills(argv: list[str]) -> int:
    import argparse
    from chaos_trader import skills_install
    p = argparse.ArgumentParser(prog="chaos skills")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    inst = sub.add_parser("install")
    inst.add_argument("--for", dest="target", required=True, choices=[*skills_install.TARGETS, "all"])
    inst.add_argument("--project", action="store_true", help="write ./.claude/skills and ./.agents/skills under the current directory")
    inst.add_argument("--dry-run", action="store_true", help="print the destinations; write nothing")
    inst.add_argument("--force", action="store_true", help="replace a same-named skill folder this command did not install")
    a = p.parse_args(argv)
    if a.action == "list":
        skills_install.list_skills()
    else:
        skills_install.install({a.target}, project=a.project, dry_run=a.dry_run, force=a.force)
    return 0


def _script_env(home: Path) -> dict[str, str]:
    """The environment every package script runs with: state from `home`, code from the package."""
    env = dict(os.environ)
    env["CHAOS_HOME"] = str(home)
    env.setdefault("HERMES_HOME", str(home))
    env.setdefault("PYTHONIOENCODING", "utf-8")  # script output carries emoji; a cp1252 pipe would crash it
    env["PYTHONPATH"] = os.pathsep.join(p for p in [str(SCRIPTS), env.get("PYTHONPATH", "")] if p)
    return env


def _set_up_home() -> Path | None:
    """CHAOS_HOME if `chaos onboard` has set it up; otherwise say so on stderr and return None."""
    home = chaos_home()
    if (home / ROSTER[1]).exists():
        return home
    print(f"No chaos-trader home at {home}. Run `chaos onboard` first (or set CHAOS_HOME to an existing home).", file=sys.stderr)
    return None


def _run_pipeline(argv: list[str]) -> int:
    home = _set_up_home()
    if home is None:
        return 2
    return subprocess.call([sys.executable, str(SCRIPTS / "chaos_cmd.py"), *argv], env=_script_env(home))


def _mcp(argv: list[str]) -> int:
    import argparse
    argparse.ArgumentParser(prog="chaos mcp", description="Serve the pipeline tools to an MCP agent on stdio. No tool signs, sends, or edits the roster.").parse_args(argv)
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(MCP_EXTRA, file=sys.stderr)
        return 2
    home = _set_up_home()
    if home is None:
        return 2
    from chaos_trader.mcp_server import serve
    serve(home)
    return 0


def _runnable() -> dict[str, dict[str, Path]]:
    """Every script `chaos run` accepts, by source in search order. Test modules and __init__ are not scripts."""
    from chaos_trader.skills_install import SOURCE
    sources = {"pipeline": SCRIPTS.glob("*.py"), "jobs": JOBS.glob("*.py"), "skills": SOURCE.glob("*/scripts/*.py")}
    found: dict[str, dict[str, Path]] = {}
    for header, paths in sources.items():
        found[header] = {}
        for path in sorted(paths):
            if not path.stem.startswith("test_") and path.stem != "__init__":
                found[header].setdefault(path.stem, path)
    return found


def _run_script(argv: list[str]) -> int:
    runnable = _runnable()
    name = argv[0] if argv else None
    for scripts in runnable.values():
        if name in scripts:
            home = _set_up_home()
            if home is None:
                return 2
            return subprocess.call([sys.executable, str(scripts[name]), *argv[1:]], env=_script_env(home))
    print(RUN_USAGE)
    print("available:")
    for header, scripts in runnable.items():
        print(f"{header}:")
        for script in sorted(scripts):
            print(f"  {script}")
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] == "--version":
        print(f"chaos-trader {__version__}")
        return 0
    if argv[0] == "onboard":
        return _onboard(argv[1:])
    if argv[0] == "update":
        return _update(argv[1:])
    if argv[0] == "skills":
        return _skills(argv[1:])
    if argv[0] == "run":
        return _run_script(argv[1:])
    if argv[0] == "mcp":
        return _mcp(argv[1:])
    return _run_pipeline(argv)


if __name__ == "__main__":
    raise SystemExit(main())
