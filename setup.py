"""Metadata lives in pyproject.toml. This hook only copies the repo-root skills tree into the wheel."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildPyWithSkills(build_py):
    def run(self):
        super().run()
        if not self.editable_mode:
            shutil.copytree(ROOT / "skills", Path(self.build_lib) / "chaos_trader" / "skills",
                            dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


setup(cmdclass={"build_py": BuildPyWithSkills})
