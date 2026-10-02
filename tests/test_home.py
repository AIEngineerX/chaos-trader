import os
import unittest
from pathlib import Path
from unittest import mock

from chaos_trader.home import chaos_home


class ChaosHomeTests(unittest.TestCase):
    def _with_env(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in ("CHAOS_HOME", "CHAOS_PROFILE_HOME", "HERMES_HOME")}
        clean.update(env)
        return mock.patch.dict(os.environ, clean, clear=True)

    def test_default_is_dot_chaos_trader_in_home(self):
        with self._with_env():
            self.assertEqual(chaos_home(), Path.home() / ".chaos-trader")

    def test_chaos_home_wins(self):
        with self._with_env(CHAOS_HOME="/tmp/a", CHAOS_PROFILE_HOME="/tmp/b", HERMES_HOME="/tmp/c"):
            self.assertEqual(chaos_home(), Path("/tmp/a").resolve())

    def test_profile_home_beats_hermes_home(self):
        with self._with_env(CHAOS_PROFILE_HOME="/tmp/b", HERMES_HOME="/tmp/c"):
            self.assertEqual(chaos_home(), Path("/tmp/b").resolve())

    def test_hermes_home_is_last_fallback(self):
        with self._with_env(HERMES_HOME="/tmp/c"):
            self.assertEqual(chaos_home(), Path("/tmp/c").resolve())

    def test_tilde_and_spaces_expand(self):
        with self._with_env(CHAOS_HOME="~/dir with spaces"):
            got = chaos_home()
            self.assertTrue(got.is_absolute())
            self.assertEqual(got.name, "dir with spaces")
            self.assertNotIn("~", str(got))

    def test_blank_value_is_ignored(self):
        with self._with_env(CHAOS_HOME="   ", HERMES_HOME="/tmp/c"):
            self.assertEqual(chaos_home(), Path("/tmp/c").resolve())


if __name__ == "__main__":
    unittest.main()
