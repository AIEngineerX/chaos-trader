"""Test-only stand-in for urllib.request.urlopen and no_redirect.open_no_redirect, the HTTP boundary
used by test_x_integration.

The test patches it in-process and loads it into child processes through a generated
sitecustomize.py, so the real analyzer, its helper scripts and the paper autopilot all run with
it. DexScreener token URLs get one canned pair; xAI's /responses gets the scenario named in the
JSON config file; RPC_URL answers getSignaturesForAddress with no signatures and every other RPC
method with a JSON-RPC error; every other URL is refused, so nothing reaches the network. Every URL
asked for is appended to the config's log file.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

FIXTURES = Path(__file__).resolve().parent
DEX_PREFIX = "https://api.dexscreener.com/"
XAI_URL = "https://api.x.ai/v1/responses"
# A local host, so a caller using it must also set CHAOS_ALLOW_PRIVATE_RPC=1; nothing listens there.
RPC_URL = "https://127.0.0.1:8899/"
# Names a catalyst (official creator, dev stream) and a risk word (scam), so the keyword
# classifiers have something to find when the answer counts as evidence.
CATALYST_TEXT = "The official creator account announced a dev stream for this token. Some replies call it a scam."
SCENARIOS = ("fixture", "catalyst_cited", "catalyst_uncited", "http429", "http503")


def dex_pair(mint: str) -> dict[str, Any]:
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "url": f"https://dexscreener.com/solana/{mint}",
        "pairAddress": "TestPair1111111111111111111111111111111111",
        "baseToken": {"address": mint, "name": "Test Token", "symbol": "TEST"},
        "priceUsd": "0.001",
        "txns": {"m5": {"buys": 3, "sells": 2}, "h1": {"buys": 40, "sells": 30}, "h6": {"buys": 200, "sells": 150}, "h24": {"buys": 600, "sells": 500}},
        "volume": {"m5": 500, "h1": 6000, "h6": 30000, "h24": 90000},
        "priceChange": {"m5": 1, "h1": 5, "h6": 8, "h24": 12},
        "liquidity": {"usd": 30000, "base": 15000000, "quote": 100},
        "fdv": 180000,
        "marketCap": 180000,
        "pairCreatedAt": 1790000000000,
    }


def xai_body(scenario: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / "xai_x_search_response.json").read_text(encoding="utf-8"))
    if scenario == "fixture":
        return data
    message = next(item for item in data["output"] if item.get("type") == "message")
    message["content"][0]["text"] = CATALYST_TEXT
    if scenario == "catalyst_uncited":
        message["content"][0]["annotations"] = []
        data["citations"] = []
    return data


def make_urlopen(config_path: str | Path) -> Callable[..., Any]:
    config_file = Path(config_path)

    def urlopen(request: Any, *args: Any, **kwargs: Any) -> io.BytesIO:
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        config = json.loads(config_file.read_text(encoding="utf-8"))
        with open(config["log"], "a", encoding="utf-8") as log:
            log.write(url + "\n")
        if url.startswith(DEX_PREFIX):
            pair = dex_pair(url.rstrip("/").rsplit("/", 1)[-1])
            body: Any = {"pairs": [pair]} if "/latest/dex/tokens/" in url else [pair]
            return io.BytesIO(json.dumps(body).encode("utf-8"))
        if url == XAI_URL:
            scenario = config["xai"]
            if scenario == "http429":
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, io.BytesIO(b'{"error": "rate limited"}'))
            if scenario == "http503":
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, io.BytesIO(b'{"error": "overloaded"}'))
            return io.BytesIO(json.dumps(xai_body(scenario)).encode("utf-8"))
        if url == RPC_URL:
            call = json.loads(request.data)
            if call.get("method") == "getSignaturesForAddress":
                reply: dict[str, Any] = {"jsonrpc": "2.0", "id": call.get("id"), "result": []}
            else:
                reply = {"jsonrpc": "2.0", "id": call.get("id"), "error": {"code": -32601, "message": "not served by the test fake"}}
            return io.BytesIO(json.dumps(reply).encode("utf-8"))
        raise urllib.error.URLError(f"network disabled in test: {url}")

    return urlopen


def install(config_path: str | Path) -> None:
    fake = make_urlopen(config_path)
    urllib.request.urlopen = fake
    # The xAI call goes through no_redirect, a script module; import it once so every later import
    # of it in this process gets the module with the fake in place.
    scripts = str(FIXTURES.parent)
    sys.path.insert(0, scripts)
    try:
        import no_redirect
    finally:
        sys.path.remove(scripts)
    no_redirect.open_no_redirect = fake
