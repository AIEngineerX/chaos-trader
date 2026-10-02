import json
import subprocess
import tomllib
import unittest
from pathlib import Path

import yaml

import chaos_trader
from tests.test_readme_rules import BANNED, RECOMMENDATION

ROOT = Path(__file__).resolve().parents[1]


class DistributionManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = yaml.safe_load((ROOT / "distribution.yaml").read_text(encoding="utf-8"))

    def test_identity_and_version(self):
        # Not "chaos": `hermes profile install --alias` names its wrapper after the profile, and a
        # `chaos` wrapper would shadow the package's own `chaos` command.
        self.assertEqual(self.manifest["name"], "chaos-trader")
        self.assertEqual(self.manifest["version"], chaos_trader.__version__)
        with (ROOT / "pyproject.toml").open("rb") as f:
            self.assertEqual(tomllib.load(f)["project"]["version"], chaos_trader.__version__)
        self.assertEqual(self.manifest["hermes_requires"], ">=0.21.0")

    def test_env_requires_are_documented_in_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        names = [entry["name"] for entry in self.manifest["env_requires"]]
        self.assertTrue(names)
        for name in names:
            self.assertIn(f"`{name}`", readme)


class ProfileConfigTests(unittest.TestCase):
    def test_config_holds_only_the_model_block(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(config), {"model"})
        self.assertEqual(config["model"]["provider"], "custom")
        self.assertEqual(config["model"]["api_key"], "")


class CronJobTests(unittest.TestCase):
    def setUp(self):
        self.jobs = json.loads((ROOT / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]

    def test_three_jobs_ship_paused(self):
        self.assertEqual([j["id"] for j in self.jobs],
                         ["chaos-elite-ingest", "chaos-paper-tick", "chaos-paper-report"])
        for job in self.jobs:
            self.assertFalse(job["enabled"], job["id"])
            self.assertEqual(job["state"], "paused", job["id"])
            self.assertIn(f"hermes -p chaos-trader cron resume {job['id']}", job["paused_reason"])

    def test_each_job_names_a_real_skill_and_a_chaos_command(self):
        for job in self.jobs:
            skill = job["skills"][0]
            self.assertEqual(job["skill"], skill)
            self.assertTrue((ROOT / "skills" / "blockchain" / skill / "SKILL.md").is_file(), skill)
            self.assertIn("chaos ", job["prompt"])
            self.assertEqual(job["schedule"]["kind"], "interval")
            self.assertEqual(job["schedule_display"], job["schedule"]["display"])


class SoulTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "SOUL.md").read_text(encoding="utf-8")

    def test_length_and_headings(self):
        self.assertLess(len(self.text.splitlines()), 80)
        for line in self.text.splitlines():
            self.assertFalse(line.startswith("###"), line)

    def test_mentions_onboard(self):
        self.assertIn("chaos onboard", self.text)

    def test_install_is_left_to_the_user(self):
        # A pip line run from the user's shell can land in a Python Hermes does not use, so SOUL points at the
        # README section that installs into Hermes's own Python and never gives a pip line itself.
        self.assertIn("Install for Hermes", self.text)
        self.assertNotIn("pip install", self.text)
        self.assertIn('chaos onboard --home "$HERMES_HOME" --yes', self.text)
        self.assertIn("HERMES_HOME is set to this profile in your terminal", self.text)

    def test_no_banned_words(self):
        low = self.text.lower()
        self.assertEqual([w for w in BANNED if w in low], [])

    def test_no_buy_or_sell_verdicts(self):
        # The recommendation sentence names buy and sell only to rule them out, so it is the one place allowed.
        low = f" {self.text.replace(RECOMMENDATION, '').lower()} "
        self.assertNotIn(" buy ", low)
        self.assertNotIn(" sell ", low)


class IgnoreRuleTests(unittest.TestCase):
    def check_ignore(self, *paths):
        return subprocess.run(["git", "check-ignore", *paths], cwd=ROOT,
                              capture_output=True, text=True)

    def test_shipped_files_are_not_ignored(self):
        result = self.check_ignore("config.yaml", "cron/jobs.json")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 1)

    def test_profile_state_is_ignored(self):
        for path in ("cron/output/x", "cron/.tick.lock", "auth.json", "state.db", "memories/a", "trading/state/x"):
            self.assertEqual(self.check_ignore(path).returncode, 0, path)


if __name__ == "__main__":
    unittest.main()
