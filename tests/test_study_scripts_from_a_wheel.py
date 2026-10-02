"""Three study scripts run from a non-editable wheel install: `chaos run <script> --help` works, and every file
a script writes lands under CHAOS_HOME, never in the installed package or anywhere else in the venv.

Real build, real venv, real subprocesses. The wheel is built from a copy of the working tree, so uncommitted
changes are what gets tested and the checkout gains no build directory. pip needs PyPI for the build and for
PyYAML; without network the test skips and says why."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
NOT_SOURCE = shutil.ignore_patterns(".git", ".tmp", ".venv", ".superpowers", "build", "dist", "*.egg-info", "__pycache__", "*.pyc")


def files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


def pypi_reachable() -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request("https://pypi.org/simple/", method="HEAD"), timeout=3):
            return True
    except OSError:
        return False


class StudyScriptsFromAWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not pypi_reachable():
            raise unittest.SkipTest("no network: pypi.org did not answer in 3 s, and pip needs it to build the wheel and install PyYAML")
        (ROOT / ".tmp").mkdir(exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix="wheel-install-", dir=ROOT / ".tmp"))
        cls.addClassCleanup(shutil.rmtree, cls.work, ignore_errors=True)
        cls.env = {k: v for k, v in os.environ.items()
                   if k not in ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME", "HELIUS_API_KEY", "SOLANA_RPC_URL", "PYTHONPATH")}
        # No bytecode writes, so the venv snapshot below catches any file a script writes there.
        cls.env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")

        src = cls.work / "src"
        shutil.copytree(ROOT, src, ignore=NOT_SOURCE)
        cls.must([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(cls.work / "dist"), str(src)], timeout=600)
        wheels = list((cls.work / "dist").glob("chaos_trader-*.whl"))
        assert len(wheels) == 1, wheels
        cls.venv = cls.work / "venv"
        cls.must([sys.executable, "-m", "venv", str(cls.venv)])
        bindir = cls.venv / ("Scripts" if os.name == "nt" else "bin")
        cls.python = bindir / ("python.exe" if os.name == "nt" else "python")
        cls.chaos = bindir / ("chaos.exe" if os.name == "nt" else "chaos")
        cls.must([str(cls.python), "-m", "pip", "install", "--quiet", str(wheels[0])], timeout=600)
        where = cls.must([str(cls.python), "-c", "import chaos_trader, pathlib; print(pathlib.Path(chaos_trader.__file__).resolve().parent)"])
        cls.package = Path(where.stdout.strip())

    @classmethod
    def must(cls, cmd, env=None, timeout=180):
        p = subprocess.run(cmd, cwd=cls.work, env=env or cls.env, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if p.returncode != 0:
            raise AssertionError(f"{cmd} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="home-", dir=self.work))
        self.home_env = dict(self.env, CHAOS_HOME=str(self.home))
        self.must([str(self.chaos), "onboard", "--rpc-url", PUBLIC_RPC, "--yes"], env=self.home_env)
        self.venv_before = files_under(self.venv)

    def tearDown(self):
        self.assertEqual(files_under(self.venv) ^ self.venv_before, set(), "a script wrote inside the venv")

    def chaos_run(self, *args):
        return self.must([str(self.chaos), "run", *args], env=self.home_env)

    def assert_in_home(self, *paths):
        for path in paths:
            self.assertTrue(Path(path).is_file(), path)
            self.assertTrue(Path(path).resolve().is_relative_to(self.home.resolve()), path)

    def test_the_package_is_the_installed_wheel_not_the_checkout(self):
        self.assertTrue(self.package.is_relative_to(self.venv.resolve()), self.package)
        self.assertTrue((self.package / "trading" / "scripts" / "runner_reality_review.py").is_file())

    def test_runner_reality_review_reports_under_the_home(self):
        self.assertIn("--owner-analysis", self.chaos_run("runner_reality_review", "--help").stdout)
        out = json.loads(self.chaos_run("runner_reality_review").stdout)
        self.assert_in_home(out["json"], out["md"])
        self.assertEqual(Path(out["json"]).parent, self.home.resolve() / "trading" / "reports" / "runner_reality")
        self.assertEqual(out["candidate_stats"]["checked"], 0)
        # A fresh home has no paper book; the review must not create an empty one.
        self.assertFalse((self.home / "trading" / "db" / "paper_autopilot.sqlite").exists())

    def test_wallet_copyability_study_writes_under_the_home(self):
        self.assertIn("ledger_json", self.chaos_run("wallet_copyability_study", "--help").stdout)
        ledger = self.work / "wallet_edge_empty.json"
        ledger.write_text(json.dumps({"ledger": []}), encoding="utf-8")
        out = json.loads(self.chaos_run("wallet_copyability_study", str(ledger), "--sleep", "0").stdout)
        self.assert_in_home(out["json"], out["md"])
        self.assertEqual(Path(out["json"]).parent.parent, self.home.resolve() / "trading" / "alpha" / "wallet_studies")

    def test_wallet_edge_study_writes_under_the_home(self):
        self.assertIn("address", self.chaos_run("wallet_edge_study", "--help").stdout)
        # A full run needs a Helius key for the transaction history, so this calls the installed function
        # that writes the study, with the same import path `chaos run` gives the script.
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import wallet_edge_study as w; "
                "print(json.dumps(w.write_outputs(sys.argv[2], [], [], [], [], 1, 0)))")
        p = self.must([str(self.python), "-c", code, str(self.package / "trading" / "scripts"), WSOL], env=self.home_env)
        out = json.loads(p.stdout)
        self.assert_in_home(out["json"], out["csv"], out["md"])
        self.assertEqual(Path(out["json"]).parent.parent, self.home.resolve() / "trading" / "alpha" / "wallet_studies")


if __name__ == "__main__":
    unittest.main()
