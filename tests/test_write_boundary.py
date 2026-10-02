"""Every script `chaos run` reaches writes under CHAOS_HOME unless the user points one of its flags elsewhere.

The scripts' argparse calls are read from source, and every option default is evaluated by importing the script in
a subprocess whose CHAOS_HOME is a temp folder, so the test sees the values a real run would use. A default counts
as a path by its value, not by the flag's name: a Path, or a string that looks like a file or folder. Every such
default must sit under the home. A flag the user sets may point anywhere (README and SECURITY.md say so); a default
may not.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "chaos_trader" / "trading" / "scripts"
RUNNABLE = [p for p in (*SCRIPTS.glob("*.py"), *(ROOT / "chaos_trader" / "jobs").glob("*.py"), *(ROOT / "skills").glob("blockchain/*/scripts/*.py"))
            if not p.stem.startswith("test_") and p.stem != "__init__"]
SWITCHES = ("store_true", "store_false", "count", "help", "version")
FILE_SUFFIXES = {".json", ".jsonl", ".sqlite", ".sqlite3", ".db", ".md", ".csv", ".yaml", ".yml", ".txt", ".log"}
# Options with no default can still name a place to write; these are found by name. Each must be required or have
# a FALLBACKS row naming the module path its script uses instead. A name prefix joined to module paths is here too.
PATH_FLAG = re.compile(r"^--(?:[a-z]+-)*(?:out|output|db|dir|root|prefix)(?:-[a-z]+)*$")
FALLBACKS = {
    ("chaos_cmd", "--db"): [("smart_wallet_promoter", "DEFAULT_DB")],  # chaos_cmd imports it as SMART_DB
    ("token_event_analyzer", "--out-dir"): [("token_event_analyzer", "OUT_ROOT")],
    ("trending_token_sweep", "--out-dir"): [("trending_token_sweep", "OUT_ROOT")],
    ("token_event_case_builder", "--out-prefix"): [("token_event_case_builder", "ALPHA")],
    ("wallet_watchlist_initial_review", "--out-prefix"): [("wallet_watchlist_initial_review", "REVIEW_ROOT"),
                                                          ("wallet_watchlist_initial_review", "WATCHLIST_ROOT")],
}
# Defaults the value rule reads as paths that are not: a label with a slash in it.
NOT_PATHS = {"paper_trade_journal --venue"}  # "manual/paper", the venue name a journal entry records
# Environment overrides for a state root; unset, each module path must sit under the home.
ENV_ROOTS = {
    "CHAOS_ALPHA_ROOT": ("alpha_claim_ledger", "ALPHA_ROOT"),
    "CHAOS_WATCHLIST_ROOT": ("alpha_claim_ledger", "WATCHLIST_ROOT"),
    "CHAOS_JOURNAL_ROOT": ("paper_trade_journal", "JOURNAL_ROOT"),
}
# Evaluates each (module, expression) in the module's namespace and reports what kind of value it is.
PROBE = """
import importlib, json, sys
from pathlib import Path, PurePath
dirs, items = json.loads(sys.argv[1]), json.loads(sys.argv[2])
sys.path[:0] = dirs
out = {}
for module, expr in items:
    try:
        value = eval(expr, vars(importlib.import_module(module)))
    except Exception as exc:
        out[module + " " + expr] = {"kind": "error", "value": f"{type(exc).__name__}: {exc}"}
        continue
    if isinstance(value, PurePath):
        out[module + " " + expr] = {"kind": "path", "value": str(value), "resolved": str(Path(value).expanduser().resolve())}
    elif isinstance(value, str):
        out[module + " " + expr] = {"kind": "str", "value": value, "resolved": str(Path(value).expanduser().resolve()) if value else ""}
    else:
        out[module + " " + expr] = {"kind": "other", "value": repr(value)[:80]}
print(json.dumps(out))
"""


def options() -> list[dict]:
    """Every argparse option in a runnable script: module, option name, default expression, required, switch."""
    found = []
    for path in RUNNABLE:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
                continue
            names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not names:
                continue
            kw = {k.arg: k.value for k in node.keywords}
            default = kw.get("default")
            found.append({
                "module": path.stem,
                "flag": next((n for n in names if n.startswith("--")), names[0]),
                "default": None if default is None or (isinstance(default, ast.Constant) and default.value is None) else ast.unparse(default),
                "required": isinstance(kw.get("required"), ast.Constant) and kw["required"].value is True,
                # A switch, or an option whose type is a number, cannot hold a path.
                "switch": (isinstance(kw.get("action"), ast.Constant) and kw["action"].value in SWITCHES)
                          or ("type" in kw and ast.unparse(kw["type"]) in ("int", "float")),
            })
    return found


class WriteBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.options = options()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.home = (Path(tmp.name) / "home").resolve()
        # `chaos run` refuses a home without a roster, so the probe home holds one, placed where onboard puts it;
        # scripts that read the roster then default to the home copy, as they do in a real run.
        (cls.home / "trading" / "config").mkdir(parents=True)
        shutil.copyfile(ROOT / "chaos_trader" / "seed" / "roster.json", cls.home / "trading" / "config" / "roster.json")
        cls.env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME", "PYTHONPATH", *ENV_ROOTS)}
        cls.env.update(CHAOS_HOME=str(cls.home), PYTHONIOENCODING="utf-8")
        cls.dirs = sorted({str(p.parent) for p in RUNNABLE})

    def evaluate(self, pairs: list[tuple[str, str]]) -> dict[str, dict]:
        p = subprocess.run([sys.executable, "-c", PROBE, json.dumps(self.dirs), json.dumps(pairs)], cwd=self.home.parent,
                           env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def is_path(self, result: dict) -> bool:
        """A Path value, or a string that names a file or folder: a separator, a home-relative or file-like name,
        or a value that resolves inside the home or the package. A URL is not a path."""
        if result["kind"] == "path":
            return True
        if result["kind"] != "str" or not result["value"] or "://" in result["value"]:
            return False
        value, resolved = result["value"], Path(result["resolved"])
        return ("/" in value or "\\" in value or value.startswith("~") or Path(value).suffix.lower() in FILE_SUFFIXES
                or resolved.is_relative_to(self.home) or resolved.is_relative_to(ROOT))

    def assert_under_home(self, results: dict[str, dict]) -> None:
        outside = {k: r.get("resolved", r["value"]) for k, r in results.items() if not Path(r.get("resolved", r["value"])).is_relative_to(self.home)}
        self.assertEqual(outside, {}, f"defaults outside CHAOS_HOME={self.home}")

    def test_every_path_default_is_under_the_home(self):
        with_default = [o for o in self.options if o["default"] is not None and not o["switch"]]
        results = self.evaluate([(o["module"], o["default"]) for o in with_default])
        errors = {k: r["value"] for k, r in results.items() if r["kind"] == "error"}
        self.assertEqual(errors, {}, "a default the probe cannot evaluate cannot be checked")
        paths = {f"{o['module']} {o['flag']}": results[f"{o['module']} {o['default']}"] for o in with_default
                 if self.is_path(results[f"{o['module']} {o['default']}"]) and f"{o['module']} {o['flag']}" not in NOT_PATHS}
        # Found by value: the known output and database flags, under any name.
        for known in ("elite_paper_cohort --evidence-db", "elite_paper_cohort --report-dir", "event_tape --out", "imported_wallet_deep_batch --out",
                      "wallet_ledger_mapper --out-dir", "signal_backfill_artifacts --root", "smart_wallet_tracker --db"):
            self.assertIn(known, paths)
        self.assertGreater(len(paths), 25)
        self.assert_under_home(paths)

    def test_the_value_rule_catches_a_path_under_any_name(self):
        # A future `--output` defaulting into the package, or a bare file name, is a path whatever the flag is called.
        package_file = {"kind": "str", "value": "reports/x.json", "resolved": str(SCRIPTS / "reports" / "x.json")}
        self.assertTrue(self.is_path(package_file))
        self.assertTrue(self.is_path({"kind": "str", "value": "out.csv", "resolved": str(self.home.parent / "out.csv")}))
        self.assertTrue(self.is_path({"kind": "path", "value": "x", "resolved": str(ROOT / "x")}))
        self.assertFalse(self.is_path({"kind": "str", "value": "https://api.dexscreener.com/x", "resolved": ""}))
        self.assertFalse(self.is_path({"kind": "str", "value": "desc", "resolved": str(self.home.parent / "desc")}))
        with self.assertRaises(AssertionError):
            self.assert_under_home({"x --output": package_file})

    def test_a_path_flag_without_a_default_is_required_or_falls_back_under_the_home(self):
        loose = [(o["module"], o["flag"]) for o in self.options
                 if PATH_FLAG.match(o["flag"]) and o["default"] is None and not o["required"] and not o["switch"]
                 and (o["module"], o["flag"]) not in FALLBACKS]
        self.assertEqual(loose, [], "a path flag with no default needs a FALLBACKS row naming where the script writes instead")
        names = {(o["module"], o["flag"]) for o in self.options}
        self.assertTrue(set(FALLBACKS) <= names, set(FALLBACKS) - names)
        self.assert_under_home(self.evaluate([pair for pairs in FALLBACKS.values() for pair in pairs]))

    def test_a_name_prefix_default_is_a_bare_file_name(self):
        prefixes = [o for o in self.options if o["flag"].endswith("-prefix") and o["default"] is not None]
        self.assertTrue(prefixes)
        for o in prefixes:
            self.assertNotRegex(ast.literal_eval(o["default"]), r"[\\/]|\.\.", o)

    def test_env_root_overrides_default_under_the_home(self):
        found = set()
        for path in RUNNABLE:
            found.update(re.findall(r"""os\.environ\.get\(\s*["'](CHAOS_[A-Z_]+_ROOT)["']""", path.read_text(encoding="utf-8")))
        self.assertEqual(found, set(ENV_ROOTS), "a new CHAOS_*_ROOT override needs a row in ENV_ROOTS")
        self.assert_under_home(self.evaluate(list(ENV_ROOTS.values())))


if __name__ == "__main__":
    unittest.main()
