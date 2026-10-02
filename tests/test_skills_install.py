"""`chaos skills install` puts the 14 skills where Claude Code, Codex, and Hermes look for them."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chaos_trader import __version__, skills_install

SKILLS = Path(__file__).resolve().parents[1] / "skills" / "blockchain"
NAMES = sorted(p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file())
SNIPPET = "skills:\n  external_dirs:\n    - ~/.agents/skills"
MARKER = ".chaos-trader-skill"
# The Hermes root is read from these; a test must never see the machine's real Hermes home.
HERMES_ENV = ("HERMES_HOME", "LOCALAPPDATA")


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if "__pycache__" not in p.parts)


def skipped_line(dest: Path) -> str:
    return f"skipped {dest}: a skill with this name already exists (use --force to replace)"


class SkillsInstallTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in HERMES_ENV:
            os.environ.pop(name, None)

    def install(self, targets, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            paths = skills_install.install(set(targets), **kw)
        return paths, out.getvalue()

    def assert_skill_tree(self, root: Path):
        self.assertEqual(sorted(p.name for p in root.iterdir()), NAMES)
        for name in NAMES:
            self.assertEqual([p for p in tree(root / name) if p != MARKER], tree(SKILLS / name), name)
            self.assertEqual((root / name / MARKER).read_text(encoding="utf-8"), __version__, name)

    def test_hermes_home_env_wins_over_home_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = Path(tmp) / "home", Path(tmp) / "hermes-home"
            (home / ".hermes").mkdir(parents=True)
            os.environ["HERMES_HOME"] = str(hermes)
            paths, out = self.install({"hermes"}, project=False, dry_run=False, home=home)
            self.assert_skill_tree(hermes / "skills" / "blockchain")
            self.assertEqual(tree(home), [".hermes"])
            self.assertEqual(sorted(paths), sorted(hermes / "skills" / "blockchain" / n for n in NAMES))
            self.assertNotIn("external_dirs", out)

    def test_windows_localappdata_hermes_wins_over_home_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, local = Path(tmp) / "home", Path(tmp) / "local"
            (home / ".hermes").mkdir(parents=True)
            (local / "hermes").mkdir(parents=True)
            os.environ["LOCALAPPDATA"] = str(local)
            with mock.patch.object(skills_install, "WINDOWS", True):
                paths, out = self.install({"hermes"}, project=False, dry_run=False, home=home)
            self.assert_skill_tree(local / "hermes" / "skills" / "blockchain")
            self.assertEqual(tree(home), [".hermes"])
            self.assertNotIn("external_dirs", out)

    def test_windows_localappdata_without_hermes_falls_back_to_home_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, local = Path(tmp) / "home", Path(tmp) / "local"
            (home / ".hermes").mkdir(parents=True)
            local.mkdir()
            os.environ["LOCALAPPDATA"] = str(local)
            with mock.patch.object(skills_install, "WINDOWS", True):
                self.install({"hermes"}, project=False, dry_run=False, home=home)
            self.assert_skill_tree(home / ".hermes" / "skills" / "blockchain")
            self.assertEqual(list(local.iterdir()), [])

    def test_foreign_skill_is_skipped_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            foreign = home / ".claude" / "skills" / "solana"
            foreign.mkdir(parents=True)
            (foreign / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
            paths, out = self.install({"claude"}, project=False, dry_run=False, home=home)
            self.assertIn(skipped_line(foreign), out)
            self.assertEqual(tree(foreign), ["SKILL.md"])
            self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "someone else's skill")
            self.assertNotIn(foreign, paths)
            self.assertEqual(len(paths), len(NAMES) - 1)

    def test_foreign_skill_is_replaced_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            foreign = home / ".claude" / "skills" / "solana"
            foreign.mkdir(parents=True)
            (foreign / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
            (foreign / "extra.txt").write_text("not ours", encoding="utf-8")
            paths, out = self.install({"claude"}, project=False, dry_run=False, force=True, home=home)
            self.assertNotIn("skipped", out)
            self.assert_skill_tree(home / ".claude" / "skills")
            self.assertIn(foreign, paths)

    def test_prior_chaos_trader_install_is_updated_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.install({"claude"}, project=False, dry_run=False, home=home)
            dest = home / ".claude" / "skills" / "solana"
            (dest / "SKILL.md").write_text("an older version", encoding="utf-8")
            (dest / MARKER).write_text("0.0.1", encoding="utf-8")
            paths, out = self.install({"claude"}, project=False, dry_run=False, home=home)
            self.assertNotIn("skipped", out)
            self.assertEqual((dest / "SKILL.md").read_bytes(), (SKILLS / "solana" / "SKILL.md").read_bytes())
            self.assert_skill_tree(home / ".claude" / "skills")
            self.assertEqual(len(paths), len(NAMES))

    def test_dry_run_reports_a_skip_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            foreign = home / ".claude" / "skills" / "solana"
            foreign.mkdir(parents=True)
            _paths, out = self.install({"claude"}, project=False, dry_run=True, home=home)
            self.assertIn(skipped_line(foreign), out)
            self.assertEqual(tree(home), [".claude", ".claude/skills", ".claude/skills/solana"])

    def test_fourteen_skills_ship(self):
        self.assertEqual(len(NAMES), 14)

    def test_all_with_hermes_home_writes_three_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".hermes").mkdir()
            paths, out = self.install({"all"}, project=False, dry_run=False, home=home)
            self.assert_skill_tree(home / ".claude" / "skills")
            self.assert_skill_tree(home / ".agents" / "skills")
            self.assert_skill_tree(home / ".hermes" / "skills" / "blockchain")
            self.assertEqual(len(paths), 42)
            self.assertEqual(len(set(paths)), 42)
            for p in paths:
                self.assertIn(str(p), out)
            self.assertNotIn("external_dirs", out)

    def test_hermes_without_hermes_home_prints_snippet_and_uses_agents_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths, out = self.install({"hermes"}, project=False, dry_run=False, home=home)
            self.assertIn(SNIPPET, out)
            self.assertFalse((home / ".hermes").exists())
            self.assert_skill_tree(home / ".agents" / "skills")
            self.assertEqual(sorted(paths), sorted(home / ".agents" / "skills" / n for n in NAMES))

    def test_all_without_hermes_home_does_not_list_the_agents_dir_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths, out = self.install({"all"}, project=False, dry_run=False, home=home)
            self.assertIn(SNIPPET, out)
            self.assertEqual(len(paths), 28)
            self.assertEqual(len(set(paths)), 28)

    def test_project_writes_under_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            home.mkdir()
            proj.mkdir()
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                self.install({"all"}, project=True, dry_run=False, home=home)
            finally:
                os.chdir(cwd)
            self.assert_skill_tree(proj / ".claude" / "skills")
            self.assert_skill_tree(proj / ".agents" / "skills")
            self.assertEqual(list(home.iterdir()), [])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".hermes").mkdir()
            paths, out = self.install({"all"}, project=False, dry_run=True, home=home)
            self.assertEqual(len(paths), 42)
            for p in paths:
                self.assertIn(str(p), out)
            self.assertEqual(tree(home), [".hermes"])

    def test_list_prints_every_name(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            skills_install.list_skills()
        for name in NAMES:
            self.assertIn(name, out.getvalue())

    def test_sdist_manifest_grafts_the_skills_tree(self):
        manifest = Path(__file__).resolve().parents[1] / "MANIFEST.in"
        self.assertIn("graft skills", manifest.read_text(encoding="utf-8").splitlines())

    def test_list_prints_14_lines_without_a_package_copy(self):
        package_copy = Path(skills_install.__file__).resolve().parent / "skills"
        if package_copy.exists():
            self.skipTest("a build left chaos_trader/skills on disk")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            skills_install.list_skills()
        self.assertEqual(len(out.getvalue().splitlines()), 14)
        self.assertEqual(skills_install.SOURCE, SKILLS)


class SkillsCliTests(unittest.TestCase):
    def run_cli(self, *args, home: Path):
        env = {k: v for k, v in os.environ.items() if k not in HERMES_ENV}
        env.update(HOME=str(home), USERPROFILE=str(home))
        return subprocess.run([sys.executable, "-m", "chaos_trader.cli", "skills", *args],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_install_all_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".hermes").mkdir()
            first = self.run_cli("install", "--for", "all", home=home)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            before = tree(home)
            second = self.run_cli("install", "--for", "all", home=home)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(tree(home), before)
            for root in (".claude/skills", ".agents/skills", ".hermes/skills/blockchain"):
                self.assertEqual(sorted(p.name for p in (home / root).iterdir()), NAMES)
                for name in NAMES:
                    self.assertFalse((home / root / name / name).exists())

    def test_force_flag_replaces_a_foreign_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            foreign = home / ".claude" / "skills" / "solana"
            foreign.mkdir(parents=True)
            (foreign / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
            plain = self.run_cli("install", "--for", "claude", home=home)
            self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
            self.assertIn(skipped_line(foreign), plain.stdout)
            forced = self.run_cli("install", "--for", "claude", "--force", home=home)
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            self.assertNotIn("skipped", forced.stdout)
            self.assertEqual((foreign / "SKILL.md").read_bytes(), (SKILLS / "solana" / "SKILL.md").read_bytes())
            self.assertEqual((foreign / MARKER).read_text(encoding="utf-8"), __version__)

    def test_list_verb(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.run_cli("list", home=Path(tmp))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(len([line for line in p.stdout.splitlines() if line.strip()]), 14)

    def test_install_requires_a_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.run_cli("install", home=Path(tmp))
            self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
