"""Metadata lives in pyproject.toml. This hook copies the repo-root skills tree into the wheel and leaves the test
modules and their fixtures out of it. The sdist carries the tests and the fixtures (MANIFEST.in); the wheel carries
neither."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildPyWithSkills(build_py):
    def run(self):
        super().run()
        if not self.editable_mode:
            package = Path(self.build_lib) / "chaos_trader"
            shutil.copytree(ROOT / "skills", package / "skills",
                            dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for test_module in list(package.rglob("test_*.py")):
                test_module.unlink()
            for fixtures in [p for p in package.rglob("fixtures") if p.is_dir()]:
                shutil.rmtree(fixtures)


setup(cmdclass={"build_py": BuildPyWithSkills})
