"""Every script `chaos run` reaches writes under CHAOS_HOME unless the user points one of its flags elsewhere.

The scripts' argparse calls are read from source; each path flag's default is then evaluated by importing the
script in a subprocess whose CHAOS_HOME is a temp folder, so the test sees the paths a real run would use.
A flag the user sets may point anywhere (README and SECURITY.md say so); a default may not.
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "chaos_trader" / "trading" / "scripts"
RUNNABLE = [*SCRIPTS.glob("*.py"), *(ROOT / "chaos_trader" / "jobs").glob("*.py"), *(ROOT / "skills").glob("blockchain/*/scripts/*.py")]
# A flag that names where something is read or written: --out, --out-dir, --db, --evidence-db, --report-dir, --root...
PATH_FLAG = re.compile(r"^--(?:[a-z]+-)*(?:out|db|dir|root|prefix)(?:-[a-z]+)*$")
# Flags with no default whose script falls back to a module path, or a name prefix the script joins to module
# paths. The test checks those module paths instead. A new flag of either kind needs a row here.
FALLBACKS = {
    ("chaos_cmd", "--db"): [("smart_wallet_promoter", "DEFAULT_DB")],  # chaos_cmd imports it as SMART_DB
    ("token_event_analyzer", "--out-dir"): [("token_event_analyzer", "OUT_ROOT")],
    ("trending_token_sweep", "--out-dir"): [("trending_token_sweep", "OUT_ROOT")],
    ("token_event_case_builder", "--out-prefix"): [("token_event_case_builder", "ALPHA")],
    ("wallet_watchlist_initial_review", "--out-prefix"): [("wallet_watchlist_initial_review", "REVIEW_ROOT"),
                                                          ("wallet_watchlist_initial_review", "WATCHLIST_ROOT")],
}
# Environment overrides for a state root; unset, each module path must sit under the home.
ENV_ROOTS = {
    "CHAOS_ALPHA_ROOT": ("alpha_claim_ledger", "ALPHA_ROOT"),
    "CHAOS_WATCHLIST_ROOT": ("alpha_claim_ledger", "WATCHLIST_ROOT"),
    "CHAOS_JOURNAL_ROOT": ("paper_trade_journal", "JOURNAL_ROOT"),
}
PROBE = """
import importlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
out = {}
for module, expr in json.loads(sys.argv[2]):
    out[module + " " + expr] = str(Path(str(eval(expr, vars(importlib.import_module(module))))).expanduser().resolve())
print(json.dumps(out))
"""


def path_flags() -> list[dict]:
    """Every path flag in a runnable script: its module, flag, default expression, and whether it is required."""
    found = []
    for path in RUNNABLE:
        if path.stem.startswith("test_") or path.stem == "__init__":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            # A switch or a number is not a path.
            action = kw.get("action")
            if (isinstance(action, ast.Constant) and action.value in ("store_true", "store_false", "count")) or \
                    ("type" in kw and ast.unparse(kw["type"]) in ("int", "float")):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and PATH_FLAG.match(arg.value):
                    default = kw.get("default")
                    found.append({"module": path.stem, "dir": str(path.parent), "flag": arg.value,
                                  "default": None if default is None or (isinstance(default, ast.Constant) and default.value is None) else ast.unparse(default),
                                  "required": isinstance(kw.get("required"), ast.Constant) and kw["required"].value is True})
    return found


class WriteBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flags = path_flags()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.home = (Path(tmp.name) / "home").resolve()
        cls.env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME", "PYTHONPATH", *ENV_ROOTS)}
        cls.env.update(CHAOS_HOME=str(cls.home), PYTHONIOENCODING="utf-8")

    def evaluate(self, pairs: list[tuple[str, str]]) -> dict[str, str]:
        p = subprocess.run([sys.executable, "-c", PROBE, str(SCRIPTS), json.dumps(pairs)], cwd=self.home.parent,
                           env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def assert_under_home(self, resolved: dict[str, str]) -> None:
        outside = {k: v for k, v in resolved.items() if not Path(v).is_relative_to(self.home)}
        self.assertEqual(outside, {}, f"defaults outside CHAOS_HOME={self.home}")

    def test_the_scan_finds_the_known_flags(self):
        names = {(f["module"], f["flag"]) for f in self.flags}
        self.assertGreater(len(names), 25)
        for known in (("elite_paper_cohort", "--evidence-db"), ("event_tape", "--out"), ("wallet_ledger_mapper", "--out-dir"),
                      ("signal_backfill_artifacts", "--root"), ("token_account_timing_probe", "--out"), *FALLBACKS):
            self.assertIn(known, names)

    def test_every_path_flag_default_is_under_the_home(self):
        with_default = [f for f in self.flags if f["default"] is not None and (f["module"], f["flag"]) not in FALLBACKS]
        self.assertTrue(all(f["dir"] == str(SCRIPTS) for f in with_default), "a path flag outside the pipeline scripts needs a probe path")
        self.assert_under_home(self.evaluate([(f["module"], f["default"]) for f in with_default]))

    def test_a_flag_without_a_default_is_required_or_falls_back_under_the_home(self):
        loose = [(f["module"], f["flag"]) for f in self.flags if f["default"] is None and not f["required"] and (f["module"], f["flag"]) not in FALLBACKS]
        self.assertEqual(loose, [], "a path flag with no default needs a FALLBACKS row naming where the script writes instead")
        self.assert_under_home(self.evaluate([pair for pairs in FALLBACKS.values() for pair in pairs]))

    def test_a_name_prefix_default_is_a_bare_file_name(self):
        for f in self.flags:
            if f["flag"].endswith("-prefix"):
                value = ast.literal_eval(f["default"])
                self.assertNotRegex(value, r"[\\/]|\.\.", f)

    def test_env_root_overrides_default_under_the_home(self):
        found = set()
        for path in RUNNABLE:
            if not path.stem.startswith("test_"):
                found.update(re.findall(r"""os\.environ\.get\(\s*["'](CHAOS_[A-Z_]+_ROOT)["']""", path.read_text(encoding="utf-8")))
        self.assertEqual(found, set(ENV_ROOTS), "a new CHAOS_*_ROOT override needs a row in ENV_ROOTS")
        self.assert_under_home(self.evaluate(list(ENV_ROOTS.values())))


if __name__ == "__main__":
    unittest.main()
