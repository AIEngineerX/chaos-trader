import contextlib
import gc
import http.client
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import helius_common
import x_provider

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "xai_x_search_response.json"
FAKE_KEY = "xai-fake-k3y-0001"
ENV_NAMES = ("X_SEARCH_PROVIDER", "HERMES_AGENT_SRC", "XAI_API_KEY", "X_SEARCH_MODEL",
             "X_SEARCH_TIMEOUT_SECONDS", "X_SEARCH_REASONING_EFFORT", "X_SEARCH_RETRIES")
HERMES_KEYS = {
    "success", "provider", "credential_source", "tool", "model", "query",
    "answer", "citations", "inline_citations", "degraded", "degraded_reason",
}


@contextlib.contextmanager
def env(**values):
    """Blank every X variable, then apply `values`. Blank, not removed: provider_name() loads
    CHAOS_HOME/.env, which fills a missing variable but never replaces one that is set."""
    with mock.patch.dict(os.environ):
        for name in ENV_NAMES:
            os.environ[name] = ""
        os.environ.update(values)
        yield


class FakeResponse(io.BytesIO):
    """What urlopen hands back: a readable context manager."""


class TruncatedResponse(FakeResponse):
    def read(self, *args):
        raise http.client.IncompleteRead(b"")


def fixture_response():
    return FakeResponse(FIXTURE.read_bytes())


def fixture_with(**changes):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(changes)
    return FakeResponse(json.dumps(payload).encode())


def http_error(code, body=b""):
    return urllib.error.HTTPError(x_provider.XAI_RESPONSES_URL, code, "error", {}, io.BytesIO(body))


class ProviderSelectionTests(unittest.TestCase):
    def test_nothing_set_is_none(self):
        with env():
            self.assertEqual(x_provider.provider_name(), "none")

    def test_hermes_src_alone_is_hermes(self):
        with env(HERMES_AGENT_SRC="/some/src"):
            self.assertEqual(x_provider.provider_name(), "hermes")

    def test_xai_key_alone_is_xai(self):
        with env(XAI_API_KEY=FAKE_KEY):
            self.assertEqual(x_provider.provider_name(), "xai")

    def test_both_set_prefers_hermes(self):
        with env(HERMES_AGENT_SRC="/some/src", XAI_API_KEY=FAKE_KEY):
            self.assertEqual(x_provider.provider_name(), "hermes")

    def test_explicit_xai_beats_hermes_default(self):
        with env(X_SEARCH_PROVIDER="xai", HERMES_AGENT_SRC="/some/src", XAI_API_KEY=FAKE_KEY):
            self.assertEqual(x_provider.provider_name(), "xai")

    def test_explicit_none_beats_both(self):
        with env(X_SEARCH_PROVIDER="none", HERMES_AGENT_SRC="/some/src", XAI_API_KEY=FAKE_KEY):
            self.assertEqual(x_provider.provider_name(), "none")

    def test_blank_values_count_as_unset(self):
        with env(X_SEARCH_PROVIDER=" ", HERMES_AGENT_SRC="", XAI_API_KEY="  "):
            self.assertEqual(x_provider.provider_name(), "none")

    def test_notice_names_an_explicit_none_when_a_provider_is_configured(self):
        cases = (
            ({"X_SEARCH_PROVIDER": "none", "XAI_API_KEY": FAKE_KEY}, "X search: provider is set to none"),
            ({"X_SEARCH_PROVIDER": "None", "HERMES_AGENT_SRC": "/some/src"}, "X search: provider is set to none"),
            ({"X_SEARCH_PROVIDER": "none"}, x_provider.NO_PROVIDER_NOTICE),
            ({}, x_provider.NO_PROVIDER_NOTICE),
        )
        for values, expected in cases:
            with self.subTest(values=values), env(**values):
                self.assertEqual(x_provider.provider_name(), "none")
                self.assertEqual(x_provider.no_provider_notice(), expected)

    def test_unknown_value_is_none_and_says_why(self):
        with env(X_SEARCH_PROVIDER="grok", XAI_API_KEY=FAKE_KEY):
            self.assertEqual(x_provider.provider_name(), "none")
            result = x_provider.search("q")
        self.assertFalse(result["available"])
        self.assertIn("X_SEARCH_PROVIDER", result["error"])


class NoneProviderTests(unittest.TestCase):
    def test_none_shape(self):
        with env(), mock.patch.object(x_provider.no_redirect, "open_no_redirect") as urlopen:
            result = x_provider.search("anything")
        urlopen.assert_not_called()
        self.assertEqual(
            {k: result[k] for k in ("available", "answer", "citations", "error")},
            {"available": False, "answer": "", "citations": [], "error": "no provider"},
        )
        self.assertTrue(HERMES_KEYS <= set(result), HERMES_KEYS - set(result))
        self.assertFalse(result["success"])
        self.assertEqual(result["inline_citations"], [])
        self.assertFalse(result["degraded"])
        self.assertEqual(result["credential_source"], "none")
        self.assertEqual(result["credential_detail"], "")

    def test_unavailable_factory_returns_fresh_dicts(self):
        first = x_provider.UNAVAILABLE("no provider")
        first["citations"].append("x")
        self.assertEqual(x_provider.UNAVAILABLE("no provider")["citations"], [])


class XaiProviderTests(unittest.TestCase):
    def search(self, urlopen, query="placeholder query", extra_env=None, **kwargs):
        """Run search() on the xai provider with retries off unless extra_env turns them on."""
        settings = {"X_SEARCH_PROVIDER": "xai", "XAI_API_KEY": FAKE_KEY, "X_SEARCH_RETRIES": "0", **(extra_env or {})}
        with env(**settings), mock.patch.object(x_provider.no_redirect, "open_no_redirect", urlopen):
            return x_provider.search(query, **kwargs)

    def test_happy_path_maps_fixture_to_hermes_shape(self):
        urlopen = mock.Mock(return_value=fixture_response())
        result = self.search(urlopen, from_date="2026-09-30", to_date="2026-10-01")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.x.ai/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)
        self.assertEqual(urlopen.call_count, 1)
        body = json.loads(request.data)
        self.assertEqual(body, {
            "model": "grok-4.5",
            "input": [{"role": "user", "content": "placeholder query"}],
            "tools": [{"type": "x_search", "from_date": "2026-09-30", "to_date": "2026-10-01"}],
            "store": False,
        })

        self.assertTrue(HERMES_KEYS <= set(result), HERMES_KEYS - set(result))
        self.assertTrue(result["available"])
        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "xai")
        self.assertEqual(result["tool"], "x_search")
        self.assertEqual(result["model"], "grok-4.5")
        self.assertEqual(result["credential_source"], "api_key")
        self.assertEqual(result["credential_detail"], "xai")
        self.assertEqual(result["query"], "placeholder query")
        self.assertTrue(result["answer"].startswith("@placeholder_handle posted"))
        self.assertEqual(result["citations"], [
            "https://x.com/placeholder_handle/status/1",
            "https://x.com/placeholder_handle_two/status/2",
            "https://x.com/i/user/placeholder_user_id",
        ])
        self.assertEqual(result["inline_citations"][0], {
            "url": "https://x.com/placeholder_handle/status/1", "title": "1",
            "start_index": 57, "end_index": 106,
        })
        self.assertEqual(len(result["inline_citations"]), 2)
        self.assertFalse(result["degraded"])
        self.assertIsNone(result["degraded_reason"])
        self.assertIsNone(result["error"])

    def test_model_and_reasoning_effort_come_from_env(self):
        urlopen = mock.Mock(return_value=fixture_response())
        result = self.search(urlopen, extra_env={"X_SEARCH_MODEL": "grok-placeholder-model",
                                                 "X_SEARCH_REASONING_EFFORT": "low"})
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["model"], "grok-placeholder-model")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(result["model"], "grok-placeholder-model")

    def test_reasoning_effort_absent_unless_set(self):
        urlopen = mock.Mock(return_value=fixture_response())
        self.search(urlopen, extra_env={"X_SEARCH_REASONING_EFFORT": " "})
        self.assertNotIn("reasoning", json.loads(urlopen.call_args.args[0].data))

    def test_timeout_env_with_30_second_floor(self):
        for raw, expected in (("45", 45), ("5", 30), ("not-a-number", 180), ("", 180)):
            with self.subTest(raw=raw):
                urlopen = mock.Mock(return_value=fixture_response())
                self.search(urlopen, extra_env={"X_SEARCH_TIMEOUT_SECONDS": raw})
                self.assertEqual(urlopen.call_args.kwargs["timeout"], expected)

    def test_retries_env_default_two_floor_zero(self):
        for raw, expected in ((None, 2), ("", 2), ("0", 0), ("1", 1), ("-3", 0), ("many", 2)):
            with self.subTest(raw=raw), env(**({} if raw is None else {"X_SEARCH_RETRIES": raw})):
                self.assertEqual(x_provider._x_search_retries(), expected)

    def test_retries_capped_at_five(self):
        with env(X_SEARCH_RETRIES="1000"):
            self.assertEqual(x_provider._x_search_retries(), 5)
        # The backoff is skipped here (1.5+3+4.5+5+5 s would be real waiting); attempts are what is checked.
        # A 30 s timeout keeps six attempts inside the 500 s total, so the time clamp does not apply.
        urlopen = mock.Mock(side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(503)))  # a fresh error per call, as urlopen raises
        with mock.patch.object(x_provider.time, "sleep"):
            result = self.search(urlopen, extra_env={"X_SEARCH_RETRIES": "1000", "X_SEARCH_TIMEOUT_SECONDS": "30"})
        self.assertFalse(result["available"])
        self.assertEqual(urlopen.call_count, 6)  # one attempt plus at most five retries
        self.assertNotIn("retries_clamped", result)

    def test_retries_clamped_to_the_500_second_total(self):
        """180 s timeout and 5 retries: 2 attempts (360 s) fit in 500 s, 3 (540 s) do not."""
        self.assertEqual(x_provider._effective_retries(5, 180), 1)
        self.assertEqual(x_provider._effective_retries(2, 180), 1)
        self.assertEqual(x_provider._effective_retries(5, 30), 5)
        self.assertEqual(x_provider._effective_retries(2, 600), 0)
        urlopen = mock.Mock(side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(503)))
        with mock.patch.object(x_provider.time, "sleep"):
            result = self.search(urlopen, extra_env={"X_SEARCH_RETRIES": "5", "X_SEARCH_TIMEOUT_SECONDS": "180"})
        self.assertFalse(result["available"])
        self.assertEqual(urlopen.call_count, 2)
        self.assertIs(result["retries_clamped"], True)
        answered = self.search(mock.Mock(return_value=fixture_response()), extra_env={"X_SEARCH_RETRIES": "5"})
        self.assertTrue(answered["available"])
        self.assertIs(answered["retries_clamped"], True)
        unclamped = self.search(mock.Mock(return_value=fixture_response()), extra_env={"X_SEARCH_RETRIES": "1"})
        self.assertNotIn("retries_clamped", unclamped)

    def test_http_error_response_is_closed(self):
        """Every HTTPError is closed, so collecting it emits no ResourceWarning (a plain run does not fail on one, so this test does)."""
        bodies = []

        def urlopen(*args, **kwargs):
            body = io.BytesIO(b"{}")
            bodies.append(body)
            raise urllib.error.HTTPError(x_provider.XAI_RESPONSES_URL, 503, "error", {}, body)

        with warnings.catch_warnings(record=True) as caught, mock.patch.object(x_provider.time, "sleep"):
            warnings.simplefilter("always")
            result = self.search(urlopen, extra_env={"X_SEARCH_RETRIES": "2", "X_SEARCH_TIMEOUT_SECONDS": "30"})
            gc.collect()  # an unclosed HTTPError warns when it is collected
        self.assertFalse(result["available"])
        self.assertEqual(len(bodies), 3)
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_one_retry_makes_a_second_attempt(self):
        # Only urlopen is faked: the 1.5 s backoff really sleeps.
        urlopen = mock.Mock(side_effect=[http_error(502), fixture_response()])
        result = self.search(urlopen, extra_env={"X_SEARCH_RETRIES": "1"})
        self.assertTrue(result["available"])
        self.assertEqual(urlopen.call_count, 2)

    def test_retries_zero_means_one_attempt(self):
        for exc in (http_error(503), TimeoutError("timed out"), urllib.error.URLError(ConnectionRefusedError("refused"))):
            with self.subTest(exc=type(exc).__name__):
                urlopen = mock.Mock(side_effect=exc)
                result = self.search(urlopen)
                self.assertFalse(result["available"])
                self.assertEqual(urlopen.call_count, 1)

    def test_429_is_unavailable_without_retrying(self):
        urlopen = mock.Mock(side_effect=http_error(429, b'{"error":"rate limited"}'))
        result = self.search(urlopen, extra_env={"X_SEARCH_RETRIES": "2"})
        self.assertFalse(result["available"])
        self.assertFalse(result["success"])
        self.assertIn("429", result["error"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(urlopen.call_count, 1)

    def test_redirect_is_refused_and_unavailable_without_retrying(self):
        urlopen = mock.Mock(side_effect=http_error(302))
        result = self.search(urlopen, extra_env={"X_SEARCH_RETRIES": "2"})
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "xAI unavailable: redirect refused (HTTP 302)")
        self.assertEqual(urlopen.call_count, 1)

    def test_http_error_with_unreadable_body_is_unavailable(self):
        for code in (401, 503):
            with self.subTest(code=code):
                broken = urllib.error.HTTPError(x_provider.XAI_RESPONSES_URL, code, "error", {}, TruncatedResponse(b""))
                result = self.search(mock.Mock(side_effect=broken))
                self.assertFalse(result["available"])
                self.assertEqual(result["error"], f"xAI HTTP {code}")
                self.assertEqual(result["error_type"], "HTTPError")

    def test_5xx_is_unavailable(self):
        result = self.search(mock.Mock(side_effect=http_error(503)))
        self.assertFalse(result["available"])
        self.assertIn("503", result["error"])

    def test_timeout_is_unavailable_without_raising(self):
        for exc in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
            with self.subTest(exc=type(exc).__name__):
                result = self.search(mock.Mock(side_effect=exc))
                self.assertFalse(result["available"])
                self.assertEqual(result["error"], "xAI x_search timed out after 180 seconds")

    def test_connection_error_is_unavailable(self):
        result = self.search(mock.Mock(side_effect=urllib.error.URLError(ConnectionRefusedError("refused"))))
        self.assertFalse(result["available"])
        self.assertIn("xAI request failed", result["error"])

    def test_http_protocol_errors_are_unavailable_without_raising(self):
        cases = {
            "IncompleteRead from urlopen": mock.Mock(side_effect=http.client.IncompleteRead(b"")),
            "BadStatusLine from urlopen": mock.Mock(side_effect=http.client.BadStatusLine("")),
            "IncompleteRead from read": mock.Mock(return_value=TruncatedResponse(b"")),
        }
        for label, urlopen in cases.items():
            with self.subTest(label):
                result = self.search(urlopen)
                self.assertFalse(result["available"])
                self.assertIn("xAI request failed", result["error"])

    def test_no_dates_sends_bare_tool_spec(self):
        urlopen = mock.Mock(return_value=fixture_response())
        self.search(urlopen)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)["tools"], [{"type": "x_search"}])

    def test_no_citation_cap_by_default(self):
        result = self.search(mock.Mock(return_value=fixture_response()))
        self.assertEqual(len(result["citations"]), 3)

    def test_max_results_caps_both_citation_lists(self):
        result = self.search(mock.Mock(return_value=fixture_response()), max_results=1)
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(len(result["inline_citations"]), 1)

    def test_dates_with_no_citations_is_degraded(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["citations"] = []
        payload["output"][1]["content"][0]["annotations"] = []
        result = self.search(mock.Mock(return_value=FakeResponse(json.dumps(payload).encode())),
                             from_date="2026-09-30")
        self.assertTrue(result["available"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["degraded_reason"], "no citations returned despite filters: from_date")

    def test_bad_dates_are_unavailable_before_any_request(self):
        today = datetime.now(timezone.utc).date()
        tomorrow = (today + timedelta(days=1)).isoformat()
        for from_date, to_date in (("2026/09/30", ""), ("", "yesterday"), ("2026-10-01", "2026-09-30"), (tomorrow, "")):
            with self.subTest(from_date=from_date, to_date=to_date):
                urlopen = mock.Mock(return_value=fixture_response())
                result = self.search(urlopen, from_date=from_date, to_date=to_date)
                urlopen.assert_not_called()
                self.assertFalse(result["available"])
                self.assertEqual(result["error_type"], "bad_dates")

    def test_future_to_date_is_allowed(self):
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        urlopen = mock.Mock(return_value=fixture_response())
        result = self.search(urlopen, from_date="2026-09-30", to_date=tomorrow)
        self.assertTrue(result["available"])
        self.assertEqual(urlopen.call_count, 1)

    def test_empty_query_is_unavailable_before_any_request(self):
        urlopen = mock.Mock(return_value=fixture_response())
        result = self.search(urlopen, query="   ")
        urlopen.assert_not_called()
        self.assertFalse(result["available"])
        self.assertEqual(result["error_type"], "empty_query")

    def test_malformed_body_is_unavailable(self):
        for body in (b"not json", b"[1, 2]", b'{"output": [1]}'):
            with self.subTest(body=body):
                result = self.search(mock.Mock(return_value=FakeResponse(body)))
                self.assertFalse(result["available"])
                self.assertIn("malformed", result["error"])

    def test_non_list_citations_count_as_none_and_degrade(self):
        for bad in ("abc", 5, {"url": "https://x.com/placeholder_handle/status/1"}):
            with self.subTest(citations=bad):
                result = self.search(mock.Mock(return_value=fixture_with(citations=bad)))
                self.assertEqual(result["citations"], [])
                self.assertTrue(result["degraded"])
                self.assertIn("citations", result["degraded_reason"])

    def test_explicit_xai_without_key_is_unavailable_and_skips_network(self):
        with env(X_SEARCH_PROVIDER="xai"), mock.patch.object(x_provider.no_redirect, "open_no_redirect") as urlopen:
            result = x_provider.search("q")
        urlopen.assert_not_called()
        self.assertFalse(result["available"])
        self.assertIn("XAI_API_KEY", result["error"])


class KeyRedactionTests(unittest.TestCase):
    def test_key_echoed_in_error_body_never_reaches_result(self):
        echo = json.dumps({"error": f"bad bearer {FAKE_KEY}"}).encode()
        with env(X_SEARCH_PROVIDER="xai", XAI_API_KEY=FAKE_KEY), \
                mock.patch.object(x_provider.no_redirect, "open_no_redirect", side_effect=http_error(401, echo)):
            result = x_provider.search("q")
        self.assertFalse(result["available"])
        self.assertNotIn(FAKE_KEY, str(result))
        self.assertNotIn(FAKE_KEY, json.dumps(result))

    def test_key_absent_from_success_result(self):
        with env(X_SEARCH_PROVIDER="xai", XAI_API_KEY=FAKE_KEY), \
                mock.patch.object(x_provider.no_redirect, "open_no_redirect", return_value=fixture_response()):
            result = x_provider.search("q")
        self.assertNotIn(FAKE_KEY, str(result))

    def test_malformed_key_is_unavailable_before_any_request_and_never_echoed(self):
        keys = {
            "embedded newline": ("xai-head-part\nTAIL-part-0001", ("xai-head-part", "TAIL-part-0001")),
            "non-latin-1 character": ("xai-head-part☃TAIL-part-0001", ("xai-head-part", "TAIL-part-0001")),
        }
        for label, (key, fragments) in keys.items():
            with self.subTest(label), env(X_SEARCH_PROVIDER="xai", XAI_API_KEY=key), \
                    mock.patch.object(x_provider.no_redirect, "open_no_redirect") as urlopen:
                result = x_provider.search("q")
            urlopen.assert_not_called()
            self.assertFalse(result["available"])
            self.assertEqual(result["error_type"], "bad_key")
            self.assertEqual(result["error"], x_provider.BAD_KEY_ERROR)
            for text in (str(result), json.dumps(result)):
                for fragment in fragments:
                    self.assertNotIn(fragment, text)

    def test_header_encoding_error_from_urlopen_is_unavailable_and_not_echoed(self):
        errors = (
            ValueError(f"Invalid header value b'Bearer {FAKE_KEY}\\r\\n'"),
            UnicodeEncodeError("latin-1", f"Bearer {FAKE_KEY}☃", 24, 25, "ordinal not in range(256)"),
        )
        for exc in errors:
            with self.subTest(type(exc).__name__), env(X_SEARCH_PROVIDER="xai", XAI_API_KEY=FAKE_KEY), \
                    mock.patch.object(x_provider.no_redirect, "open_no_redirect", side_effect=exc):
                result = x_provider.search("q")
            self.assertFalse(result["available"])
            self.assertEqual(result["error_type"], "bad_request")
            self.assertNotIn(FAKE_KEY, str(result))

    def test_safe_print_redacts_xai_key(self):
        buf = io.StringIO()
        with env(XAI_API_KEY=FAKE_KEY), contextlib.redirect_stdout(buf):
            helius_common.safe_print({"auth": f"Bearer {FAKE_KEY}", "nested": [FAKE_KEY]})
        self.assertNotIn(FAKE_KEY, buf.getvalue())
        self.assertIn("<XAI_API_KEY_REDACTED>", buf.getvalue())


def hermes_checkout() -> Path | None:
    """HERMES_AGENT_SRC if set, else a hermes-agent checkout beside this repo."""
    configured = os.environ.get("HERMES_AGENT_SRC", "").strip()
    candidate = Path(configured) if configured else Path(__file__).resolve().parents[3].parent / "hermes-agent"
    return candidate if (candidate / "tools" / "x_search_tool.py").is_file() else None


def write_broken_hermes_tree(src: Path) -> None:
    """A Hermes source tree whose tools/x_search_tool.py does not compile."""
    (src / "tools").mkdir()
    (src / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (src / "tools" / "x_search_tool.py").write_text("def x_search_tool(query:\n    return\n", encoding="utf-8")


@contextlib.contextmanager
def isolated_tools_import():
    """Let each test import its own `tools` package and leave sys.path/sys.modules as found."""
    saved_path = list(sys.path)
    saved = {k: v for k, v in sys.modules.items() if k == "tools" or k.startswith("tools.")}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for name in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            del sys.modules[name]
        sys.modules.update(saved)


class HermesProviderTests(unittest.TestCase):
    def test_real_hermes_tool_imports_from_src(self):
        src = hermes_checkout()
        if src is None:
            self.skipTest("no hermes-agent checkout (set HERMES_AGENT_SRC or clone it beside this repo)")
        if importlib.util.find_spec("requests") is None:
            self.skipTest("requests not installed")
        with env(HERMES_AGENT_SRC=str(src)), isolated_tools_import():
            self.assertEqual(x_provider.provider_name(), "hermes")
            tool = x_provider._hermes_tool()
            self.assertTrue(callable(tool))
            self.assertEqual(tool.__name__, "x_search_tool")
            self.assertEqual(Path(sys.modules[tool.__module__].__file__).resolve(),
                             (src / "tools" / "x_search_tool.py").resolve())
        # The tool is not called: its credential resolver may refresh an xAI OAuth token over the network.

    def test_missing_src_tree_is_unavailable_with_import_error(self):
        with tempfile.TemporaryDirectory() as empty, env(HERMES_AGENT_SRC=empty), isolated_tools_import():
            result = x_provider.search("q")
        self.assertFalse(result["available"])
        self.assertTrue(result["error"].startswith("x_search import failed:"), result["error"])
        self.assertEqual(result["error_type"], "hermes_import")
        self.assertTrue(HERMES_KEYS <= set(result))
        self.assertEqual(result["credential_source"], "hermes")

    def test_tool_with_a_syntax_error_is_an_import_failure(self):
        with tempfile.TemporaryDirectory() as src, isolated_tools_import():
            write_broken_hermes_tree(Path(src))
            with env(HERMES_AGENT_SRC=src):
                result = x_provider.search("q")
        self.assertFalse(result["available"])
        self.assertEqual(result["error_type"], "hermes_import")
        self.assertIn("SyntaxError", result["error"])

    def search_with_stub_tool(self, payload, **kwargs):
        """search() through the real HERMES_AGENT_SRC import path, against a throwaway source
        tree whose x_search_tool returns `payload` as JSON."""
        with tempfile.TemporaryDirectory() as src:
            (Path(src) / "tools").mkdir()
            (Path(src) / "tools" / "__init__.py").write_text("", encoding="utf-8")
            (Path(src) / "tools" / "x_search_tool.py").write_text(
                "import json\n\ndef x_search_tool(query, from_date='', to_date=''):\n"
                f"    return json.dumps({payload!r})\n",
                encoding="utf-8",
            )
            with env(HERMES_AGENT_SRC=src), isolated_tools_import():
                return x_provider.search("q", **kwargs)

    def test_hermes_failure_payload_keeps_its_error_and_gains_full_shape(self):
        # The failure JSON the Hermes tool returns when no xAI credential resolves.
        result = self.search_with_stub_tool({"success": False, "provider": "xai", "tool": "x_search",
                                             "error": "No xAI credentials available.", "error_type": "RuntimeError"})
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "No xAI credentials available.")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertTrue(HERMES_KEYS <= set(result))
        self.assertEqual(result["credential_source"], "hermes")
        self.assertEqual(result["credential_detail"], "")

    def test_subscription_answer_keeps_hermes_credential_as_detail(self):
        # Hermes's success shape for a SuperGrok login: an answer and no citations.
        result = self.search_with_stub_tool({
            "success": True, "provider": "xai", "credential_source": "xai-oauth", "tool": "x_search",
            "model": "grok-4.5", "query": "q", "answer": "unsourced answer", "citations": [],
            "inline_citations": [], "degraded": False, "degraded_reason": None,
        })
        self.assertTrue(result["available"])
        self.assertEqual(result["credential_source"], "hermes")
        self.assertEqual(result["credential_detail"], "xai-oauth")
        self.assertEqual(result["citations"], [])
        self.assertIsNone(result["error"])

    def test_hermes_citations_are_returned_untouched(self):
        citations = [f"https://x.com/placeholder_handle/status/{n}" for n in range(1, 13)]
        result = self.search_with_stub_tool({
            "success": True, "provider": "xai", "credential_source": "xai", "tool": "x_search",
            "model": "grok-4.5", "query": "q", "answer": "a", "citations": citations,
            "inline_citations": [], "degraded": False, "degraded_reason": None,
        }, max_results=1)
        self.assertEqual(result["citations"], citations)
        self.assertEqual(result["credential_detail"], "xai")


if __name__ == "__main__":
    unittest.main()
