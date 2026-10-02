"""The one JSON envelope every `chaos_cmd.py` verb prints with `--json`.

`--raw` keeps each verb's older unwrapped payload; `--json` wraps the same payload as `data`. The schema
version changes only when a field's meaning changes.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaos_home import chaos_home
from helius_common import load_env, safe_print

SCHEMA_VERSION = "1"
HOME_TOKEN = "$CHAOS_HOME"


def envelope(command: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": data,
    }


def status(state: str, message: str) -> dict[str, str]:
    """`data` for a verb that has nothing to show yet and prints one sentence instead of a payload."""
    return {"status": state, "message": message}


def _home_prefixes() -> list[str]:
    """Every spelling of the home that can appear in a payload, longest first so a nested form is not cut."""
    homes = [chaos_home()]
    raw = os.environ.get("CHAOS_HOME", "").strip()
    if raw:
        homes.append(Path(raw).expanduser())
    forms = {form for home in homes for form in (str(home), home.as_posix())}
    return sorted(forms, key=len, reverse=True)


def scrub_home(value: Any, prefixes: list[str]) -> Any:
    """Replace the home's absolute path with `$CHAOS_HOME` in every string value and every dict key.

    On Windows the match ignores case, as the file system does, so `c:\\users\\...` is the same home as `C:\\Users\\...`."""
    if not prefixes:
        return value  # an empty alternation would match at every position
    pattern = re.compile("|".join(re.escape(prefix) for prefix in prefixes), re.IGNORECASE if os.name == "nt" else 0)
    return _scrub(value, pattern)


def _scrub(value: Any, pattern: re.Pattern[str]) -> Any:
    if isinstance(value, dict):
        return {_scrub(key, pattern): _scrub(item, pattern) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, pattern) for item in value]
    if isinstance(value, str):
        return pattern.sub(lambda _match: HOME_TOKEN, value)
    return value


def print_envelope(command: str, data: Any) -> None:
    """Print the envelope with home paths replaced and the Helius and xAI keys redacted (`safe_print`)."""
    load_env()  # a key that lives only in CHAOS_HOME/.env must be known before safe_print can redact it
    plain = json.loads(json.dumps(envelope(command, data), ensure_ascii=False, default=str))
    safe_print(scrub_home(plain, _home_prefixes()))
