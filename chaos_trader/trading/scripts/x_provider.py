#!/usr/bin/env python3
"""X search behind one seam: X_SEARCH_PROVIDER = hermes | xai | none.

Default when X_SEARCH_PROVIDER is unset: `hermes` if HERMES_AGENT_SRC is set, else `xai` if
XAI_API_KEY is set, else `none`. Both real providers reach the same upstream, xAI's Responses API
with the built-in `x_search` tool; `xai` calls it directly with XAI_API_KEY, `hermes` imports the
tool from a Hermes Agent source tree. `search()` never raises and always returns the dict shape the
Hermes tool returns, plus `available`, `error`, `error_type` and `credential_detail`.
`credential_source` says which path answered: "api_key" (xai), "hermes", or "none".
`credential_detail` keeps the credential the Hermes tool reported ("xai-oauth" for a subscription
login, "xai" for an API key); the xai provider reports "xai".

A SuperGrok / X Premium+ login (via Hermes) returns answers without citations; under this package's
rules a citation-free answer is no evidence. Set XAI_API_KEY for real posts (metered).

The xai provider's settings follow Hermes's `x_search` config: X_SEARCH_MODEL (default grok-4.5),
X_SEARCH_REASONING_EFFORT (sent only when set), X_SEARCH_TIMEOUT_SECONDS (default 180, minimum 30),
and X_SEARCH_RETRIES (default 2, at most 5) on 5xx, timeout, connection or HTTP protocol errors.
Retries are cut so that every attempt's timeout together stays within 500 s, inside the paper
tick's 540 s and the autopilot child's 620 s; a result says `retries_clamped: True` when that happened.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Callable

import no_redirect
from helius_common import load_env

PROVIDERS = ("hermes", "xai", "none")
NO_PROVIDER_NOTICE = "X search: no provider configured (set XAI_API_KEY or HERMES_AGENT_SRC)"
CREDENTIAL_SOURCES = {"hermes": "hermes", "xai": "api_key", "none": "none"}
# Endpoint and tool spec copied from the Hermes tool (tools/x_search_tool.py): DEFAULT_XAI_BASE_URL +
# "/responses" and {"type": "x_search", from_date, to_date}. Model, timeout and retries follow the
# Hermes x_search docs (grok-4.5 recommended, timeout_seconds 180 with a 30 s floor, retries 2).
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
DEFAULT_X_SEARCH_MODEL = "grok-4.5"
DEFAULT_X_SEARCH_TIMEOUT_SECONDS = 180
MIN_X_SEARCH_TIMEOUT_SECONDS = 30
DEFAULT_X_SEARCH_RETRIES = 2
MAX_X_SEARCH_RETRIES = 5
X_SEARCH_TOTAL_BUDGET_SECONDS = 500
XAI_KEY_MIN_LENGTH = 8
XAI_KEY_MAX_LENGTH = 512
BAD_KEY_ERROR = "XAI_API_KEY is malformed (non-ASCII, whitespace, or not 8 to 512 characters); fix it in CHAOS_HOME/.env"


def provider_name() -> str:
    load_env()  # CHAOS_HOME/.env, where the README says to set these; never replaces a process env value
    explicit = os.environ.get("X_SEARCH_PROVIDER", "").strip().lower()
    if explicit:
        return explicit if explicit in PROVIDERS else "none"
    if os.environ.get("HERMES_AGENT_SRC", "").strip():
        return "hermes"
    if os.environ.get("XAI_API_KEY", "").strip():
        return "xai"
    return "none"


def no_provider_notice() -> str:
    """The one stderr line printed when X is asked for and provider_name() is "none"."""
    explicit = os.environ.get("X_SEARCH_PROVIDER", "").strip()
    if explicit and explicit.lower() not in PROVIDERS:
        return f"X search: unknown provider '{explicit}' (use hermes, xai, or none)"
    configured = os.environ.get("XAI_API_KEY", "").strip() or os.environ.get("HERMES_AGENT_SRC", "").strip()
    if explicit.lower() == "none" and configured:
        return "X search: provider is set to none"
    return NO_PROVIDER_NOTICE


def UNAVAILABLE(error: str, *, provider: str = "none", query: str = "", error_type: str | None = None) -> dict[str, Any]:
    """A fresh "no X evidence" dict carrying every key of the Hermes shape, empty."""
    return {
        "available": False,
        "success": False,
        "provider": provider,
        "credential_source": CREDENTIAL_SOURCES.get(provider, "none"),
        "credential_detail": "",
        "tool": "x_search",
        "model": "",
        "query": query,
        "answer": "",
        "citations": [],
        "inline_citations": [],
        "degraded": False,
        "degraded_reason": None,
        "error": error,
        "error_type": error_type,
    }


def search(query: str, *, max_results: int | None = None, from_date: str = "", to_date: str = "") -> dict[str, Any]:
    """Search X through the configured provider.

    `from_date` / `to_date` are YYYY-MM-DD strings (either may be empty). They go into the x_search
    tool spec as the search window, and they are what lets `degraded` fire: a window was set and no
    citation came back, so the answer is the model's own knowledge. `max_results`, when given,
    caps each citation list on the xai path only; the hermes path returns the tool's citations as
    the tool gave them.
    """
    name = provider_name()
    query = query.strip()
    if name == "hermes":
        return _search_hermes(query, from_date, to_date)
    if name == "xai":
        return _search_xai(query, from_date, to_date, max_results)
    explicit = os.environ.get("X_SEARCH_PROVIDER", "").strip()
    if explicit and explicit.lower() not in PROVIDERS:
        return UNAVAILABLE(f"no provider (X_SEARCH_PROVIDER={explicit!r} is not one of {', '.join(PROVIDERS)})", query=query)
    return UNAVAILABLE("no provider", query=query)


def _hermes_tool() -> Callable[..., str]:
    src = os.environ.get("HERMES_AGENT_SRC", "").strip()
    if src and src not in sys.path:
        sys.path.insert(0, src)
    from tools.x_search_tool import x_search_tool  # type: ignore

    return x_search_tool


def _search_hermes(query: str, from_date: str, to_date: str) -> dict[str, Any]:
    try:
        tool = _hermes_tool()
    except Exception as exc:  # any import failure (ImportError, SyntaxError, ...) is before a request
        return UNAVAILABLE(f"x_search import failed: {type(exc).__name__}: {exc}", provider="hermes", query=query, error_type="hermes_import")
    try:
        payload = json.loads(tool(query=query, from_date=from_date, to_date=to_date))
    except Exception as exc:
        return UNAVAILABLE(f"x_search failed: {exc}", provider="hermes", query=query, error_type=type(exc).__name__)
    available = bool(payload.get("success"))
    result = {
        **UNAVAILABLE("", provider="hermes", query=query),
        **payload,
        "available": available,
        "credential_source": "hermes",
        "credential_detail": str(payload.get("credential_source") or ""),
    }
    result["error"] = None if available else payload.get("error") or "x_search failed"
    return result


def _x_search_model() -> str:
    return os.environ.get("X_SEARCH_MODEL", "").strip() or DEFAULT_X_SEARCH_MODEL


def _int_env(name: str, default: int, floor: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(floor, int(raw)) if raw else default
    except ValueError:
        return default


def _x_search_timeout_seconds() -> int:
    return _int_env("X_SEARCH_TIMEOUT_SECONDS", DEFAULT_X_SEARCH_TIMEOUT_SECONDS, MIN_X_SEARCH_TIMEOUT_SECONDS)


def _x_search_retries() -> int:
    return min(MAX_X_SEARCH_RETRIES, _int_env("X_SEARCH_RETRIES", DEFAULT_X_SEARCH_RETRIES, 0))


def _effective_retries(retries: int, timeout: int) -> int:
    """The most retries whose attempts, at `timeout` each, fit in X_SEARCH_TOTAL_BUDGET_SECONDS (never below 0)."""
    return max(0, min(retries, X_SEARCH_TOTAL_BUDGET_SECONDS // timeout - 1))


def _key_malformed(key: str) -> bool:
    """A key that cannot go into an HTTP header as-is: non-ASCII, unprintable, whitespace, or an odd length."""
    return (
        not XAI_KEY_MIN_LENGTH <= len(key) <= XAI_KEY_MAX_LENGTH
        or not key.isascii()
        or not key.isprintable()
        or any(ch.isspace() for ch in key)
    )


def _date_error(from_date: str, to_date: str) -> str | None:
    """Hermes's client-side date checks: YYYY-MM-DD, not inverted, from_date not in the future."""
    parsed: dict[str, date] = {}
    for field, value in (("from_date", from_date), ("to_date", to_date)):
        raw = value.strip()
        if raw:
            try:
                parsed[field] = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return f"{field} must be YYYY-MM-DD (got {raw!r})"
    start, end = parsed.get("from_date"), parsed.get("to_date")
    if start and end and start > end:
        return f"from_date ({start.isoformat()}) must be on or before to_date ({end.isoformat()})"
    today = datetime.now(timezone.utc).date()
    if start and start > today:
        return f"from_date ({start.isoformat()}) is in the future; X Search only indexes past posts (today UTC is {today.isoformat()})"
    return None


def _transport_error(exc: Exception, timeout: int) -> str:
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
        return f"xAI x_search timed out after {timeout} seconds"
    if isinstance(exc, http.client.HTTPException):
        return f"xAI request failed: {type(exc).__name__}: {exc!r}"
    return f"xAI request failed: {getattr(exc, 'reason', exc)}"


def _search_xai(query: str, from_date: str, to_date: str, max_results: int | None) -> dict[str, Any]:
    if not query:
        return UNAVAILABLE("query is required for x_search", provider="xai", query=query, error_type="empty_query")
    date_error = _date_error(from_date, to_date)
    if date_error:
        return UNAVAILABLE(date_error, provider="xai", query=query, error_type="bad_dates")
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        return UNAVAILABLE("XAI_API_KEY is not set", provider="xai", query=query)
    if _key_malformed(key):
        return UNAVAILABLE(BAD_KEY_ERROR, provider="xai", query=query, error_type="bad_key")
    tool_def: dict[str, Any] = {"type": "x_search"}
    if from_date.strip():
        tool_def["from_date"] = from_date.strip()
    if to_date.strip():
        tool_def["to_date"] = to_date.strip()
    model = _x_search_model()
    body: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": query}],
        "tools": [tool_def],
        "store": False,
    }
    effort = os.environ.get("X_SEARCH_REASONING_EFFORT", "").strip()
    if effort:
        body["reasoning"] = {"effort": effort}
    timeout = _x_search_timeout_seconds()
    asked_retries = _x_search_retries()
    retries = _effective_retries(asked_retries, timeout)
    clamp = {"retries_clamped": True} if retries < asked_retries else {}

    def fail(message: str, exc: BaseException) -> dict[str, Any]:
        result = UNAVAILABLE(message.replace(key, "<XAI_API_KEY_REDACTED>"), provider="xai", query=query, error_type=type(exc).__name__)
        return {**result, **clamp}

    # Hermes's retry rule: 5xx, timeouts and connection errors are retried with a 1.5 s x attempt
    # backoff capped at 5 s; any other HTTP status fails at once. A broken HTTP exchange
    # (IncompleteRead, BadStatusLine, LineTooLong) counts as a connection error.
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                XAI_RESPONSES_URL,
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "chaos-trader"},
            )
            # Stricter than Hermes (requests follows a redirect, dropping the header only on a host
            # change): no redirect is followed, so the key never goes to another host or plain http.
            with no_redirect.open_no_redirect(request, timeout=timeout) as response:
                raw = response.read()
            break
        except ValueError as exc:
            # Header encoding (UnicodeEncodeError is a ValueError) fails before anything is sent.
            # The message can quote the header, so none of it is kept.
            result = UNAVAILABLE(f"xAI request could not be built ({type(exc).__name__})", provider="xai", query=query, error_type="bad_request")
            return {**result, **clamp}
        except urllib.error.HTTPError as exc:
            try:
                if 300 <= exc.code < 400:
                    return fail(f"xAI unavailable: redirect refused (HTTP {exc.code})", exc)
                if exc.code < 500 or attempt >= retries:
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:300]
                    except (OSError, http.client.HTTPException):
                        detail = ""
                    return fail(f"xAI HTTP {exc.code}: {detail}".rstrip(": "), exc)
            finally:
                exc.close()  # release the response so no ResourceWarning is emitted
        except (OSError, http.client.HTTPException) as exc:  # URLError, TimeoutError, connection and protocol errors
            if attempt >= retries:
                return fail(_transport_error(exc, timeout), exc)
        time.sleep(min(5.0, 1.5 * (attempt + 1)))

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError(f"top level is {type(data).__name__}, not an object")
        answer = _extract_response_text(data)
        inline_citations = _extract_inline_citations(data)
    except (ValueError, TypeError, AttributeError) as exc:
        return fail(f"malformed xAI response: {exc}", exc)

    raw_citations = data.get("citations")
    citations_malformed = raw_citations is not None and not isinstance(raw_citations, list)
    citations = [] if citations_malformed else list(raw_citations or [])

    # Hermes's degraded rule: a narrowing filter was active and neither citation channel has
    # anything, so the answer came from the model's own knowledge, not the X index. A citations
    # field that is not a list is treated as no citations and always degrades.
    active_filters = [name for name, value in (("from_date", from_date), ("to_date", to_date)) if value.strip()]
    if citations_malformed:
        degraded_reason: str | None = f"malformed citations field from xAI ({type(raw_citations).__name__}, not a list)"
    elif active_filters and not citations and not inline_citations:
        degraded_reason = f"no citations returned despite filters: {', '.join(active_filters)}"
    else:
        degraded_reason = None
    if max_results is not None:
        citations, inline_citations = citations[:max_results], inline_citations[:max_results]
    return {
        "available": True,
        "success": True,
        "provider": "xai",
        "credential_source": "api_key",
        "credential_detail": "xai",
        "tool": "x_search",
        "model": model,
        "query": query,
        "answer": answer,
        "citations": citations,
        "inline_citations": inline_citations,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
        "error": None,
        "error_type": None,
        **clamp,
    }


# The two extractors below follow the Hermes tool's _extract_response_text / _extract_inline_citations.
def _extract_response_text(payload: dict[str, Any]) -> str:
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"}:
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_inline_citations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            for annotation in content.get("annotations", []) or []:
                if annotation.get("type") != "url_citation":
                    continue
                citations.append({
                    "url": annotation.get("url", ""),
                    "title": annotation.get("title", ""),
                    "start_index": annotation.get("start_index"),
                    "end_index": annotation.get("end_index"),
                })
    return citations
