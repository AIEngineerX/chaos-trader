"""Create or refresh a CHAOS_HOME, which holds state only: .env, the seed roster, the paper config, the
wallet schema, and the data folders. The code runs from the installed package and is never copied here."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
RUNTIME_DIRS = ("trading/db", "trading/reports", "trading/alpha", "trading/alpha/secondary", "trading/alpha/mint_scores", "trading/state")
PAPER_CONFIG = PACKAGE / "trading" / "config" / "paper_autopilot.yaml"
# (package source, path in the home). The roster's presence is what "set up" means, so onboard writes it last.
ROSTER = (PACKAGE / "seed" / "roster.json", "trading/config/roster.json")
PAPER = (PAPER_CONFIG, "trading/config/paper_autopilot.yaml")
SCHEMA = (PACKAGE / "trading" / "schemas" / "smart_wallets_schema.sql", "trading/db/smart_wallets_schema.sql")
# Overwritten on every onboard and update, so a user can diff their config against the shipped one.
DEFAULTS = (PAPER_CONFIG, "trading/config/paper_autopilot.defaults.yaml")
ALREADY_SET_UP = "{home} is already set up. Run `chaos update` to refresh the defaults, or pick another CHAOS_HOME."


def _copy_if_absent(home: Path, src: Path, rel: str) -> None:
    """The home copy wins forever after."""
    if not (home / rel).exists():
        shutil.copy2(src, home / rel)


def _write_env(home: Path, rpc_url: str | None, helius_key: str | None, rpc_is_default: bool = False) -> list[str]:
    """Write .env, or merge into an existing one. Returns what was added or skipped, one line each.

    A default RPC URL is not added to an existing .env that already holds HELIUS_API_KEY: SOLANA_RPC_URL
    beats the key at read time, so adding it would move every read off Helius."""
    env_path = home / ".env"
    wanted = [(k, v) for k, v in (("SOLANA_RPC_URL", rpc_url), ("HELIUS_API_KEY", helius_key)) if v]
    if not env_path.exists():
        lines = ["# Written by `chaos onboard`. Edit by hand any time.", ""]
        lines.extend(f"{k}={v}" for k, v in wanted)
        # Created already restricted, so the keys are never readable by others, even for a moment.
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return [f"wrote {env_path} with {', '.join(k for k, _ in wanted) or 'no keys'}"]
    text = env_path.read_text(encoding="utf-8")
    present = {line.split("=", 1)[0].strip().removeprefix("export ").strip() for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")}
    report: list[str] = []
    appended: list[str] = []
    for key, value in wanted:
        if key == "SOLANA_RPC_URL" and rpc_is_default and "HELIUS_API_KEY" in present:
            report.append("SOLANA_RPC_URL: skipped (HELIUS_API_KEY already set; the key builds the Helius RPC URL)")
        elif key in present:
            report.append(f"skipped {key}: already set in {env_path}; edit that line by hand to change it")
        else:
            appended.append(f"{key}={value}")
            report.append(f"added {key} to {env_path}")
            if key == "SOLANA_RPC_URL" and "HELIUS_API_KEY" in present:
                report.append("SOLANA_RPC_URL now takes precedence over the existing HELIUS_API_KEY")
    if appended:
        if text and not text.endswith("\n"):
            text += "\n"
        env_path.write_text(text + "\n".join(appended) + "\n", encoding="utf-8")
    return report


def onboard(home: Path, rpc_url: str | None, helius_key: str | None, update: bool = False, env_report: list[str] | None = None, rpc_is_default: bool = False) -> Path:
    """Onboard: create `home` with .env, the seed roster, the paper config, the wallet schema, and the data
    folders, leaving any other files already in it alone. Update: refresh the default paper config and restore
    a missing schema; .env, the roster, the paper config, and the data are never touched."""
    home = Path(home).expanduser().resolve()
    if PACKAGE in home.parents or home == PACKAGE:
        raise SystemExit(f"CHAOS_HOME must not be inside the installed package ({PACKAGE}).")
    set_up = (home / ROSTER[1]).exists()
    if not update and set_up:
        raise SystemExit(ALREADY_SET_UP.format(home=home))
    if update and not set_up:
        raise SystemExit(f"{home} is not set up. Run `chaos onboard` first.")
    for rel in RUNTIME_DIRS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    (home / "trading" / "config").mkdir(exist_ok=True)
    _copy_if_absent(home, *SCHEMA)
    shutil.copy2(DEFAULTS[0], home / DEFAULTS[1])
    if update:
        return home
    _copy_if_absent(home, *PAPER)
    report = _write_env(home, rpc_url, helius_key, rpc_is_default)
    if env_report is not None:
        env_report.extend(report)
    _copy_if_absent(home, *ROSTER)
    return home
