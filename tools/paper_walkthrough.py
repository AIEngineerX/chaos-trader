#!/usr/bin/env python3
"""Offline installed-CLI walkthrough. Synthetic inputs, never market evidence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import sysconfig
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MINT = "8wxkvAfEns76yBzu4MnbV7VnXWjg3iDPA9uwAQ6cpump"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New directory for isolated state and evidence")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    home = out / "home"
    home.mkdir()
    site = out / "site"
    site.mkdir()
    cli = Path(sysconfig.get_path("scripts")) / ("chaos.exe" if os.name == "nt" else "chaos")
    if not cli.is_file():
        raise SystemExit("Install the wheel in this Python environment first")
    fixture = ROOT / "chaos_trader/trading/scripts/fixtures"
    config = out / "http.json"
    config.write_text(json.dumps({"log": str(out / "http.log"), "xai": "fixture", "demo": True, "mint": MINT}), encoding="utf-8")
    # Import only the HTTP fixture from the checkout; all product modules come from the installation.
    (site / "sitecustomize.py").write_text(
        "import sys, socket\n"
        "def deny(*a, **k): raise RuntimeError('offline walkthrough: network disabled')\n"
        "socket.socket.connect = deny\nsocket.create_connection = deny\n"
        "import chaos_trader\nfrom pathlib import Path\n"
        "scripts = str(Path(chaos_trader.__file__).parent / 'trading' / 'scripts')\n"
        "sys.path.insert(0, scripts)\nimport no_redirect\nsys.path.remove(scripts)\n"
        f"sys.path.insert(0, {str(fixture)!r})\nimport xai_http_fake\nsys.path.remove({str(fixture)!r})\n"
        f"xai_http_fake.install({str(config)!r})\n",
        encoding="utf-8",
    )
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "LANG") if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), CHAOS_HOME=str(home), PYTHONPATH=str(site),
               X_SEARCH_PROVIDER="none", PYTHONIOENCODING="utf-8",
               SOLANA_RPC_URL="https://127.0.0.1:8899/", CHAOS_ALLOW_PRIVATE_RPC="1")
    probe = subprocess.run([sys.executable, "-c", "import chaos_trader; print(chaos_trader.__file__)"],
                           cwd=out, env=env, capture_output=True, text=True, check=True)
    installed = Path(probe.stdout.strip()).resolve()
    if installed.is_relative_to(ROOT / "chaos_trader"):
        raise SystemExit("An installed wheel is required, not an editable/source import")
    (out / "installation.txt").write_text(str(installed), encoding="utf-8")
    steps = []

    def run(label: str, *words: str, raw: bool = False) -> Any:
        proc = subprocess.run([str(cli), *words], cwd=out, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=180)
        (out / f"{label}.stdout").write_text(proc.stdout, encoding="utf-8")
        (out / f"{label}.stderr").write_text(proc.stderr, encoding="utf-8")
        steps.append({"step": label, "command": ["chaos", *words], "exit": proc.returncode})
        print(f"{label}: exit {proc.returncode}", flush=True)
        if proc.returncode:
            raise RuntimeError(f"{label} failed; inspect its stdout/stderr in the output directory")
        return json.loads(proc.stdout) if raw else proc.stdout

    run("01-onboard", "onboard", "--yes")
    run("02-empty-readiness", "readiness", "--json", raw=True)
    import yaml
    paper_config = home / "trading/config/paper_autopilot.yaml"
    settings = yaml.safe_load(paper_config.read_text(encoding="utf-8"))
    settings["deep_analyze"]["cooldown_per_mint_min"] = 0
    paper_config.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    # An empty external-input tape is enough: discovery below uses synthetic public-market HTTP responses.
    import chaos_trader
    schema = Path(chaos_trader.__file__).parent / "trading/schemas/smart_wallets_schema.sql"
    con = sqlite3.connect(home / "trading/db/smart_wallets.sqlite")
    try:
        con.executescript(schema.read_text(encoding="utf-8"))
    finally:
        con.close()
    run("03-token-evidence", "token", MINT, "--no-x", "--json", raw=True)
    run("04-paper-read", "strategy-paper", MINT, "--json", raw=True)
    first = run("05-first-tick", "run", "chaos_paper_autopilot", "--once", "--analyze-top", "1", "--limit", "1", "--raw", raw=True)
    assert first["decisions"][0]["decision"] == "paper_wait"
    # Reuse the existing synthetic indexed-wallet input fixture, never decision/book injection.
    elite = out / "synthetic-wallet-input.sqlite"
    seed = (f"import sys; sys.path.insert(0, {str(fixture.parent)!r}); "
            "from test_chaos_paper_autopilot import seed_wallet_event_db; "
            "from datetime import datetime, timezone, timedelta; from pathlib import Path; "
            f"seed_wallet_event_db(Path({str(elite)!r}), run_id='elite-demo-input', "
            "observed_at=datetime.now(timezone.utc)-timedelta(minutes=5), wallets=2)")
    subprocess.run([sys.executable, "-c", seed], cwd=out, env=env, check=True, timeout=30)
    settings["paths"]["smart_wallet_sqlite"] = str(elite)
    paper_config.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    second = run("06-second-tick", "run", "chaos_paper_autopilot", "--once", "--analyze-top", "1", "--limit", "1", "--raw", raw=True)
    assert second["decisions"][0]["decision"] == "paper_enter", second["decisions"]
    run("07-positions", "paper-positions", "--state", "all", "--json", raw=True)
    inputs = json.loads(config.read_text(encoding="utf-8"))
    inputs.update(price_usd=0.0005, liquidity_usd=1000)
    config.write_text(json.dumps(inputs), encoding="utf-8")
    # Let the existing 10-second market cache expire; do not edit engine state.
    import time
    time.sleep(11)
    run("07b-manage-exit", "run", "chaos_paper_autopilot", "--once", "--analyze-top", "1", "--limit", "1", "--raw", raw=True)
    closed = run("07c-closed-positions", "paper-positions", "--state", "all", "--json", raw=True)["data"]
    assert closed["count"] == 1
    assert closed["positions"][0]["state"] == "PAPER_CLOSED"
    assert closed["positions"][0]["exit_reason"] == "liquidity_break"
    avoid = run("07d-avoid", "strategy-paper", MINT, "--json", raw=True)["data"]
    assert avoid["decision"] == "paper_avoid"
    report = run("08-paper-report", "paper-report", "--json", raw=True)["data"]
    assert report["fills"] == 2
    assert report["positions"]["closed"] == 1 and report["positions"]["open"] == 0
    run("09-outcomes", "outcomes", "--json", raw=True)
    run("10-readiness", "readiness", "--json", raw=True)
    (out / "steps.json").write_text(json.dumps(steps, indent=2), encoding="utf-8")
    print("Offline synthetic walkthrough finished; outputs are not live evidence or profitability results.")


if __name__ == "__main__":
    main()
