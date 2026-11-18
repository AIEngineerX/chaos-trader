"""tools/history_name_scan.py run against real throwaway git repositories, and once against this one.

The banned names exist only as digests, so the planted case adds one fixture digest to the vendor
gate's table inside the scan's own process; everything else is the real scan over a real history."""
import hashlib
import os
import shutil
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

    def test_private_metadata_is_rejected_without_echoing_values(self):
        for surface in ("name", "email", "message", "tag", "path"):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as tmp:
                repo = new_repo(Path(tmp))
                if surface == "name":
                    git(repo, "config", "user.name", PLANTED)
                if surface == "email":
                    git(repo, "config", "user.email", PLANTED + "@example.invalid")
                commit(repo, PLANTED + ".md" if surface == "path" else "notes.md",
                       "safe text\n", PLANTED if surface == "message" else "safe message")
                if surface == "tag":
                    git(repo, "tag", "-a", "v1", "-m", PLANTED)
                p = run(["-c", SCAN_WITH_PLANTED_DIGEST], repo)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn(PLANTED_KIND, p.stdout)
                self.assertNotIn(PLANTED, p.stdout + p.stderr)

    def test_exact_public_commit_identity(self):
        name = "".join(chr(n) for n in (71, 104, 111, 115, 116))
        email = "195990077+AIEngineerX@users.noreply.github.com"
        for role in ("author", "committer", "both"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                repo = new_repo(Path(tmp))
                commit(repo, "notes.md", "safe text\n", "safe message")
                env = dict(os.environ)
                for identity in ("author", "committer"):
                    prefix = "GIT_" + identity.upper()
                    env[prefix + "_NAME"] = name if role in (identity, "both") else "Scan Test"
                    env[prefix + "_EMAIL"] = email if role in (identity, "both") else "scan-test@example.invalid"
                subprocess.run(["git", "commit", "--amend", "--reset-author", "--no-edit", "-q"],
                               cwd=repo, env=env, capture_output=True, check=True)
                p = run([str(SCAN)], repo)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_public_identity_exception_does_not_hide_private_surfaces(self):
        name = "".join(chr(n) for n in (71, 104, 111, 115, 116))
        email = "195990077+AIEngineerX@users.noreply.github.com"
        for surface in ("other-email", "email-case", "name-case", "extended-name",
                        "message", "header-in-message", "path", "blob", "private-email", "tag"):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as tmp:
                repo = new_repo(Path(tmp))
                git(repo, "config", "user.name", name.lower() if surface == "name-case" else
                    name + " Extra" if surface == "extended-name" else name)
                git(repo, "config", "user.email",
                    "other@example.invalid" if surface == "other-email" else
                    email.lower() if surface == "email-case" else
                    name + "@example.invalid" if surface == "private-email" else email)
                message = name if surface == "message" else (
                    f"author {name} <{email}> 1700000000 +0000" if surface == "header-in-message"
                    else "safe message")
                commit(repo, name + ".md" if surface == "path" else "notes.md",
                       name if surface == "blob" else "safe text\n", message)
                if surface == "tag":
                    git(repo, "tag", "-a", "v1", "-m", "safe message")
                p = run([str(SCAN)], repo)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("owner first name", p.stdout)
                self.assertNotIn(name, p.stdout + p.stderr)

    def test_this_repository_history_is_clean(self):
        # Fails on any history that predates the fresh public root; the public root scans clean.
        p = run([str(SCAN)], ROOT)
        self.assertEqual(p.returncode, 0, "the full history carries private names; publish only from the fresh root:\n"
                         + p.stdout[-2000:] + p.stderr)
        self.assertRegex(p.stdout.strip(), r"^history scan: clean \(\d+ blobs\)$")

    def test_standalone_checks_branch_and_lightweight_tag_names(self):
        for command in ("branch", "tag"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                repo = new_repo(Path(tmp))
                commit(repo, "notes.md", "safe text\n", "safe message")
                git(repo, command, PLANTED)
                p = run(["-c", SCAN_WITH_PLANTED_DIGEST], repo)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn(PLANTED_KIND + " <redacted ref>", p.stdout)
                self.assertNotIn(PLANTED, p.stdout + p.stderr)


class PrePushTests(unittest.TestCase):
    """Use the shipped hook and scanner against a local bare remote, without network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = new_repo(root)
        self.remote = root / "remote.git"
        git(root, "init", "--bare", "-q", str(self.remote))
        git(self.repo, "remote", "add", "origin", str(self.remote))
        for name in ("tools/history_name_scan.py", "tools/safety_check.py",
                     "tests/test_no_vendor_names.py", ".githooks/pre-push"):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        gate = self.repo / "tests/test_no_vendor_names.py"
        with gate.open("a", encoding="utf-8") as stream:
            digest = hashlib.sha256(PLANTED.encode()).hexdigest()
            stream.write(f"\nFORBIDDEN_TOKEN_DIGESTS[{digest!r}] = {PLANTED_KIND!r}\n")
        git(self.repo, "config", "core.hooksPath", ".githooks")
        commit(self.repo, "notes.md", "safe text\n", "safe message")
        self.clean = self.oid()
        self.env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])

    def oid(self):
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

    def refs(self):
        return subprocess.check_output(
            ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
            cwd=self.remote, text=True)

    def push(self, *specs):
        return subprocess.run(["git", "push", "origin", *specs], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)

    def rejected(self, *specs):
        before = self.refs()
        p = self.push(*specs)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(PLANTED_KIND, p.stdout + p.stderr)
        self.assertNotIn(PLANTED, p.stdout + p.stderr)
        self.assertEqual(self.refs(), before)

    def test_clean_push_and_noop_are_accepted(self):
        for _ in range(2):
            p = self.push("HEAD:refs/heads/public")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("refs/heads/public " + self.clean, self.refs())

    def test_explicit_detached_and_unreferenced_tips_are_rejected(self):
        branch = subprocess.check_output(["git", "symbolic-ref", "--short", "HEAD"],
                                         cwd=self.repo, text=True).strip()
        git(self.repo, "checkout", "--detach", "-q")
        commit(self.repo, "notes.md", PLANTED + "\n", "safe message")
        dirty = self.oid()
        self.rejected(dirty + ":refs/heads/detached")
        git(self.repo, "checkout", "-q", branch)
        self.rejected(dirty + ":refs/heads/unreferenced")

    def test_private_destination_branch_and_lightweight_tag_are_rejected(self):
        self.rejected("HEAD:refs/heads/" + PLANTED)
        self.rejected("HEAD:refs/tags/" + PLANTED)
        git(self.repo, "branch", PLANTED)
        self.rejected(PLANTED + ":refs/heads/public")
        git(self.repo, "tag", PLANTED)
        self.rejected("refs/tags/" + PLANTED + ":refs/tags/public")

    def test_deletions_allow_private_ref_cleanup_and_mixed_updates(self):
        private_ref = "refs/heads/" + PLANTED
        git(self.remote, "fetch", "-q", str(self.repo), "HEAD:" + private_ref)
        p = self.push(":" + private_ref, "HEAD:refs/heads/public")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn(private_ref, self.refs())
        self.assertIn("refs/heads/public " + self.clean, self.refs())
        p = self.push(":refs/heads/public")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.refs(), "")

    def test_multiple_updates_do_not_hide_dirty_tip(self):
        p = self.push("HEAD:refs/heads/obsolete")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        commit(self.repo, "notes.md", PLANTED + "\n", "safe message")
        self.rejected(":refs/heads/obsolete", self.clean + ":refs/heads/clean", "HEAD:refs/heads/dirty")

    def test_git_replace_cannot_hide_dirty_outgoing_history(self):
        commit(self.repo, "notes.md", PLANTED + "\n", "safe message")
        dirty = self.oid()
        git(self.repo, "replace", dirty, self.clean)
        self.rejected("HEAD:refs/heads/public")

    def test_direct_blob_and_annotated_tag_objects_are_scanned(self):
        blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"],
                                       cwd=self.repo, input=PLANTED + "\n", text=True).strip()
        self.rejected(blob + ":refs/tags/blob")
        git(self.repo, "tag", "-a", "public", "-m", PLANTED)
        self.rejected("refs/tags/public:refs/tags/public")

    def test_invalid_input_fails_closed_without_echoing_values(self):
        for data in (PLANTED + "\n", f"{PLANTED} invalid refs/heads/public {'0' * 40}\n",
                     f"{PLANTED} {'f' * 40} refs/heads/public {'0' * 40}\n"):
            with self.subTest(data=data):
                p = subprocess.run([str(self.repo / ".githooks/pre-push")], cwd=self.repo,
                                   input=data, env=self.env, capture_output=True, text=True)
                self.assertNotEqual(p.returncode, 0)
                self.assertNotIn(PLANTED, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
