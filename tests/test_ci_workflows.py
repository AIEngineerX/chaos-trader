"""The two GitHub workflows, parsed as YAML: CI stays read-only, and the PyPI upload uses trusted publishing."""
import shlex
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load(name: str) -> dict:
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # YAML 1.1 reads the bare key `on` as the boolean True.
    workflow["on"] = workflow.pop(True, workflow.get("on"))
    return workflow


def step_inputs(job: dict) -> list[str]:
    return [key.lower() for step in job["steps"] for key in (step.get("with") or {})]


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load("publish.yml")
        self.jobs = self.workflow["jobs"]

    def test_runs_only_on_a_version_tag(self):
        # One trigger: a tag push plus a published release would upload the same files twice.
        self.assertEqual(self.workflow["on"], {"push": {"tags": ["v*"]}})

    def test_the_build_refuses_a_tag_that_is_not_the_package_version(self):
        steps = self.jobs["build"]["steps"]
        names = [s.get("name", "") for s in steps]
        check = steps[names.index("The tag matches the package version")]
        self.assertLess(names.index("The tag matches the package version"), names.index("Build the wheel and sdist"))
        python, flag, code, tag_arg = shlex.split(check["run"].strip())
        self.assertEqual((python, flag, tag_arg), ("python", "-c", "${GITHUB_REF_NAME#v}"))
        with (ROOT / "pyproject.toml").open("rb") as f:
            version = tomllib.load(f)["project"]["version"]
        # The shell strips the v from the tag, so the script receives the bare version.
        ok = subprocess.run([sys.executable, "-c", code, version], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        bad = subprocess.run([sys.executable, "-c", code, "9.9.9"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(bad.stderr.strip(), f"tag v9.9.9 does not match the pyproject.toml version {version}")

    def test_only_the_publish_job_gets_an_oidc_token(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["publish"]["permissions"], {"id-token": "write"})
        for name, job in self.jobs.items():
            if name != "publish":
                self.assertNotIn("permissions", job, name)

    def test_publish_runs_in_the_pypi_environment_after_the_build(self):
        publish = self.jobs["publish"]
        self.assertEqual(publish["environment"], "pypi")
        self.assertEqual(publish["needs"], "build")
        uses = [step.get("uses", "") for step in publish["steps"]]
        self.assertTrue(any(u.startswith("pypa/gh-action-pypi-publish@") for u in uses), uses)
        self.assertFalse(any(step.get("run") for step in publish["steps"]), "the publish job runs no commands")

    def test_no_stored_credential_reaches_the_upload(self):
        text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8").lower()
        for word in ("password", "twine", "secrets.", "api-token", "api_token"):
            self.assertNotIn(word, text)
        for name, job in self.jobs.items():
            self.assertEqual([i for i in step_inputs(job) if i in ("password", "user", "username")], [], name)

    def test_the_build_job_uploads_what_publish_downloads(self):
        build_steps = self.jobs["build"]["steps"]
        upload = next(s for s in build_steps if s.get("uses", "").startswith("actions/upload-artifact@"))
        download = next(s for s in self.jobs["publish"]["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(upload["with"]["name"], download["with"]["name"])
        self.assertEqual(download["with"]["path"], "dist/")
        self.assertIn("python -m build", "\n".join(s.get("run", "") for s in build_steps))

    def test_every_action_is_pinned_by_commit_sha_with_its_tag_beside_it(self):
        # These actions run next to an OIDC token that can upload to PyPI, so a moved tag must not change them.
        uses = [step["uses"] for job in self.jobs.values() for step in job["steps"] if "uses" in step]
        self.assertEqual(len(uses), 5)
        for use in uses:
            self.assertRegex(use, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        lines = [line.strip() for line in (WORKFLOWS / "publish.yml").read_text(encoding="utf-8").splitlines() if "uses:" in line]
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertRegex(line, r"uses: \S+@[0-9a-f]{40} # v\d+(\.\d+)*$")

    def test_the_build_tool_is_pinned(self):
        run = "\n".join(s.get("run", "") for s in self.jobs["build"]["steps"])
        self.assertRegex(run, r"pip install build==\d+\.\d+\.\d+\n")

    def test_the_build_runs_the_tests_before_it_builds(self):
        steps = self.jobs["build"]["steps"]
        names = [s.get("name", "") for s in steps]
        test = steps[names.index("Install the package and run the tests")]
        self.assertLess(names.index("The tag matches the package version"), names.index("Install the package and run the tests"))
        self.assertLess(names.index("Install the package and run the tests"), names.index("Build the wheel and sdist"))
        # A temp PROFILE keeps the test home out of the tree that python -m build packs.
        self.assertEqual(test["run"].strip(), 'python -m pip install -e . && make test PROFILE="$RUNNER_TEMP/h"')
        self.assertNotIn("continue-on-error", test)


class CiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load("ci.yml")

    def test_ci_stays_read_only(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for name, job in self.workflow["jobs"].items():
            self.assertNotIn("permissions", job, name)

    def test_the_history_scan_is_a_hard_gate(self):
        steps = self.workflow["jobs"]["checks"]["steps"]
        scan = next(s for s in steps if s.get("run") == "python tools/history_name_scan.py")
        self.assertNotIn("continue-on-error", scan)
        self.assertFalse([s.get("name") for s in steps if s.get("continue-on-error")])


if __name__ == "__main__":
    unittest.main()
