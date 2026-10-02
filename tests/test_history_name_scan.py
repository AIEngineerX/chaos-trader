"""tools/history_name_scan.py run against real throwaway git repositories, and once against this one.

The banned names exist only as digests, so the planted case adds one fixture digest to the vendor
gate's table inside the scan's own process; everything else is the real scan over a real history."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "tools" / "history_name_scan.py"
PLANTED = "plantedfixturename"
PLANTED_KIND = "planted test name"
SCAN_WITH_PLANTED_DIGEST = (
    f"import sys; sys.path.insert(0, {str(SCAN.parent)!r}); import history_name_scan as scan; "
    f"scan.vendor.FORBIDDEN_TOKEN_DIGESTS[scan.vendor.digest({PLANTED!r})] = {PLANTED_KIND!r}; "
    "raise SystemExit(scan.main())"
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def new_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Scan Test")
    git(repo, "config", "user.email", "scan-test@example.invalid")
    return repo


def commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")


class HistoryNameScanTests(unittest.TestCase):
    def test_a_name_removed_from_the_tree_is_still_found_in_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = new_repo(Path(tmp))
            commit(repo, "notes.md", f"ask {PLANTED} about the host\n", "add notes")
            commit(repo, "notes.md", "ask the operator about the host\n", "scrub notes")
            p = run(["-c", SCAN_WITH_PLANTED_DIGEST], repo)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertRegex(p.stdout, rf"(?m)^{PLANTED_KIND} [0-9a-f]{{10}} notes\.md$")
            self.assertNotIn(PLANTED, p.stdout)
            self.assertIn("history scan: 1 hits in 1 blobs (2 blobs scanned)", p.stdout)

    def test_a_clean_history_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = new_repo(Path(tmp))
            commit(repo, "notes.md", "ask the operator about the host\n", "add notes")
            p = run(["-c", SCAN_WITH_PLANTED_DIGEST], repo)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(p.stdout.strip(), "history scan: clean (1 blobs)")

    def test_this_repository_history_is_clean(self):
        # Fails on any history that predates the fresh public root; the public root scans clean.
        p = run([str(SCAN)], ROOT)
        self.assertEqual(p.returncode, 0, "the full history carries private names; publish only from the fresh root:\n"
                         + p.stdout[-2000:] + p.stderr)
        self.assertRegex(p.stdout.strip(), r"^history scan: clean \(\d+ blobs\)$")


if __name__ == "__main__":
    unittest.main()
