"""tools/safety_check.py, run as a subprocess on a temp copy of the repo's tracked files."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [name for name in out.stdout.decode("utf-8").split("\0") if name]


class SafetyCheckTests(unittest.TestCase):
    def copy_repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in tracked_files():
            source = ROOT / name
            if source.is_file():  # a tracked file deleted in the working tree is not copied
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        return root

    def run_check(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(root / "tools" / "safety_check.py")], cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
        )

    def test_clean_copy_passes(self):
        proc = self.run_check(self.copy_repo())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("safety_check OK", proc.stdout)

    def test_file_under_a_journal_directory_fails_and_is_named(self):
        root = self.copy_repo()
        planted = root / "trading" / "journal" / "x.md"
        planted.parent.mkdir(parents=True)
        planted.write_text("private trade notes\n", encoding="utf-8")
        proc = self.run_check(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"private/runtime path committed: {Path('trading/journal/x.md')}", proc.stderr)


if __name__ == "__main__":
    unittest.main()
