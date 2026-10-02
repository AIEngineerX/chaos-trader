#!/usr/bin/env python3
"""A corrupt paper book must not read as "no paper activity yet".

`chaos paper-report` and the report script it wraps, each run as a real subprocess on a temp CHAOS_HOME.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
NO_ACTIVITY = 'No paper activity yet. Run the ingest job and the paper tick first (see "Running it on a schedule" in the README).'


class PaperLearningReportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.book = self.home / "trading" / "db" / "paper_autopilot.sqlite"
        self.book.parent.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("CHAOS_PROFILE_HOME", "HERMES_HOME")}
        self.env.update(CHAOS_HOME=str(self.home), PYTHONIOENCODING="utf-8")

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], cwd=str(SCRIPT_DIR), capture_output=True, text=True,
                              encoding="utf-8", env=self.env, timeout=120, check=False)

    def test_no_book_still_reports_no_activity(self) -> None:
        p = self.run_script("chaos_cmd.py", "paper-report")
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertEqual(NO_ACTIVITY, p.stdout.strip())

    def test_a_corrupt_book_exits_1_with_one_line(self) -> None:
        self.book.write_bytes(bytes(range(256)) * 24)
        line = f"{self.book} is not a readable SQLite database. Move it aside and the next paper tick starts a new book."
        for args in (("chaos_cmd.py", "paper-report"), ("paper_learning_report.py",)):
            with self.subTest(command=args[0]):
                p = self.run_script(*args)
                self.assertEqual(1, p.returncode, p.stdout + p.stderr)
                self.assertEqual("", p.stdout)
                self.assertEqual(line, p.stderr.strip())


if __name__ == "__main__":
    unittest.main()
