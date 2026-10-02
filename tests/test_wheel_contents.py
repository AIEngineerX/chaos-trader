"""What the wheel ships: the package, the 14 skills, the seed, the schemas, the paper config, LICENSE and NOTICE,
and no test code, fixtures, scratch folders, or databases.

The wheel is built for real with pip from a copy of the working tree, so uncommitted changes are what gets checked.
pip fetches setuptools from PyPI for the isolated build; without network the test skips and says why.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOT_SOURCE = shutil.ignore_patterns(".git", ".tmp", ".venv", ".superpowers", "build", "dist", "*.egg-info", "__pycache__", "*.pyc")


def pypi_reachable() -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request("https://pypi.org/simple/", method="HEAD"), timeout=3):
            return True
    except OSError:
        return False


class WheelContentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not pypi_reachable():
            raise unittest.SkipTest("no network: pypi.org did not answer in 3 s, and pip needs it for the isolated wheel build")
        (ROOT / ".tmp").mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="wheel-contents-", dir=ROOT / ".tmp"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        shutil.copytree(ROOT, work / "src", ignore=NOT_SOURCE)
        p = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--disable-pip-version-check", "--wheel-dir", str(work / "dist"),
                            str(work / "src")], capture_output=True, text=True, encoding="utf-8", timeout=600)
        if p.returncode != 0:
            raise AssertionError(f"pip wheel exited {p.returncode}\n{p.stdout}\n{p.stderr}")
        wheels = list((work / "dist").glob("chaos_trader-*.whl"))
        assert len(wheels) == 1, wheels
        with zipfile.ZipFile(wheels[0]) as wheel:
            cls.names = set(wheel.namelist())

    def test_fourteen_skills(self):
        skills = {n for n in self.names if n.startswith("chaos_trader/skills/blockchain/") and n.endswith("/SKILL.md") and n.count("/") == 4}
        self.assertEqual(len(skills), 14, sorted(skills))
        self.assertEqual(len(skills), len(list((ROOT / "skills" / "blockchain").glob("*/SKILL.md"))))

    def test_license_and_notice(self):
        for name in ("LICENSE", "NOTICE"):
            self.assertTrue(any(n.endswith(f".dist-info/licenses/{name}") for n in self.names), name)

    def test_seed_schemas_and_paper_config(self):
        for name in ("chaos_trader/seed/roster.json", "chaos_trader/trading/config/paper_autopilot.yaml",
                     *(f"chaos_trader/trading/schemas/{p.name}" for p in (ROOT / "chaos_trader" / "trading" / "schemas").glob("*.sql"))):
            self.assertIn(name, self.names)

    def test_the_pipeline_and_the_cli_ship(self):
        for name in ("chaos_trader/cli.py", "chaos_trader/mcp_server.py", "chaos_trader/trading/scripts/chaos_cmd.py",
                     "chaos_trader/trading/scripts/no_redirect.py", "chaos_trader/jobs/chaos_paper_autopilot_tick.py"):
            self.assertIn(name, self.names)

    def test_no_test_code_or_fixtures(self):
        self.assertEqual(sorted(n for n in self.names if n.rsplit("/", 1)[-1].startswith("test_")), [])
        self.assertEqual(sorted(n for n in self.names if "/fixtures/" in n), [])

    def test_no_scratch_build_or_database_entries(self):
        stray = [n for n in self.names if n.startswith((".tmp/", "build/")) or "/.tmp/" in n or "/build/" in n
                 or n.endswith((".sqlite", ".sqlite3", ".db", ".pyc"))]
        self.assertEqual(stray, [])


if __name__ == "__main__":
    unittest.main()
