"""`chaos skills`: list the shipped skills, or copy them to where Claude Code, Codex, and Hermes look for them."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from chaos_trader import __version__

_PACKAGE_COPY = Path(__file__).resolve().parent / "skills" / "blockchain"
SOURCE = _PACKAGE_COPY if _PACKAGE_COPY.exists() else Path(__file__).resolve().parents[1] / "skills" / "blockchain"
TARGETS = ("claude", "codex", "hermes")
HERMES_SNIPPET = "skills:\n  external_dirs:\n    - ~/.agents/skills"
# Written into every installed skill folder. A same-named folder without it belongs to someone else.
MARKER = ".chaos-trader-skill"
WINDOWS = os.name == "nt"


def skill_dirs() -> list[Path]:
    return sorted(p for p in SOURCE.iterdir() if (p / "SKILL.md").is_file())


def _description(skill: Path) -> str:
    lines = (skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    meta = yaml.safe_load("\n".join(lines[1:lines.index("---", 1)]))
    return meta["description"]


def list_skills() -> None:
    for skill in skill_dirs():
        first = _description(skill).split(". ", 1)[0].rstrip(".")
        print(f"{skill.name}: {first}.")


def hermes_root(home: Path) -> Path | None:
    """The Hermes home, in the order Hermes resolves it: HERMES_HOME, then %LOCALAPPDATA%\\hermes on Windows, then ~/.hermes."""
    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if WINDOWS and local and (Path(local) / "hermes").is_dir():
        return Path(local) / "hermes"
    if (home / ".hermes").is_dir():
        return home / ".hermes"
    return None


def _roots(targets: set[str], project: bool, home: Path) -> list[Path]:
    """Destination roots in target order, without repeats."""
    chosen = set(TARGETS) if "all" in targets else targets
    base = Path.cwd() if project else home
    roots: list[Path] = []
    if "claude" in chosen:
        roots.append(base / ".claude" / "skills")
    if "codex" in chosen:
        roots.append(base / ".agents" / "skills")
    if "hermes" in chosen:
        hermes = hermes_root(home)
        if project:
            print("Hermes reads ./.agents/skills only after you run `hermes skills trust` in this project.")
            roots.append(base / ".agents" / "skills")
        elif hermes is not None:
            roots.append(hermes / "skills" / "blockchain")
        else:
            print(f"No Hermes home (HERMES_HOME, %LOCALAPPDATA%\\hermes, or {home / '.hermes'}); "
                  f"installing for Hermes into {home / '.agents' / 'skills'}.")
            print("Add this to your Hermes config.yaml so Hermes reads that directory:")
            print(HERMES_SNIPPET)
            roots.append(home / ".agents" / "skills")
    return list(dict.fromkeys(roots))


def install(targets: set[str], *, project: bool, dry_run: bool, force: bool = False, home: Path | None = None) -> list[Path]:
    """Copy every skill folder into each target's skill directory. Returns the destination folders written.

    A destination that exists without this installer's marker is someone else's skill: it is skipped,
    or replaced when `force` is set. A destination with the marker is updated in place.
    """
    home = Path.home() if home is None else home
    if dry_run:
        print("dry run: nothing is written")
    written: list[Path] = []
    for root in _roots(targets, project, home):
        for skill in skill_dirs():
            dest = root / skill.name
            foreign = dest.exists() and not (dest / MARKER).is_file()
            if foreign and not force:
                print(f"skipped {dest}: a skill with this name already exists (use --force to replace)")
                continue
            if not dry_run:
                if foreign:
                    shutil.rmtree(dest)
                shutil.copytree(skill, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                (dest / MARKER).write_text(__version__, encoding="utf-8")
            print(dest)
            written.append(dest)
    return written
