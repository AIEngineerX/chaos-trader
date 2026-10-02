import io
import os
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

import chaos_cmd

NOTICE = "X search: no provider configured (set XAI_API_KEY or HERMES_AGENT_SRC)\n"
# Blank values count as unset, and a key already in the environment is never replaced from
# CHAOS_HOME/.env, so these cases do not depend on the home the tests run against.
NO_PROVIDER = {"X_SEARCH_PROVIDER": "", "HERMES_AGENT_SRC": "", "XAI_API_KEY": ""}


def args(**overrides):
    return SimpleNamespace(**{"default_x": True, "with_x": False, "no_x": False, **overrides})


def requested(env, namespace):
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, {**NO_PROVIDER, **env}), redirect_stderr(stderr):
        return chaos_cmd.x_requested(namespace), stderr.getvalue()


class XDefaultTests(unittest.TestCase):
    def test_default_off_without_a_provider(self):
        self.assertEqual(requested({}, args()), (False, ""))

    def test_default_on_with_a_provider(self):
        for env in ({"XAI_API_KEY": "xai-test-key"}, {"HERMES_AGENT_SRC": "/some/src"}, {"X_SEARCH_PROVIDER": "xai"}):
            with self.subTest(env=env):
                self.assertEqual(requested(env, args()), (True, ""))

    def test_no_x_vetoes_even_with_a_provider(self):
        self.assertEqual(requested({"XAI_API_KEY": "xai-test-key"}, args(no_x=True)), (False, ""))
        self.assertEqual(requested({"XAI_API_KEY": "xai-test-key"}, args(with_x=True, no_x=True)), (False, ""))

    def test_with_x_and_no_provider_is_off_with_one_notice(self):
        self.assertEqual(requested({}, args(with_x=True)), (False, NOTICE))
        self.assertEqual(requested({"X_SEARCH_PROVIDER": "none", "XAI_API_KEY": "xai-test-key"}, args(with_x=True)), (False, "X search: provider is set to none\n"))

    def test_with_x_forces_the_request_when_a_provider_exists(self):
        self.assertEqual(requested({"XAI_API_KEY": "xai-test-key"}, args(default_x=False, with_x=True)), (True, ""))


if __name__ == "__main__":
    unittest.main()
