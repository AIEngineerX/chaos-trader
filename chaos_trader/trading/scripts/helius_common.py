#!/usr/bin/env python3
"""Shared read-only Helius helpers for Chaos.

No transaction sending. No key printing. Loads Chaos-local .env first.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
ENV_PATH = PROFILE_HOME / ".env"
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,88}$")
SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,100}$")
ALLOWED_RPC_METHODS = {
    "getAsset",
    "getBalance",
    "getMultipleAccounts",
    "getSignaturesForAddress",
    "getTokenAccounts",
    "getTokenAccountsByOwner",
    "getTokenLargestAccounts",
    "getTokenSupply",
    "getTransaction",
    "getTransactionsForAddress",
}


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _rpc_env_label() -> str:
    return "SOLANA_RPC_URL"


def _parsed_endpoint(endpoint: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(endpoint)
    if not parsed.scheme or not parsed.hostname:
        raise SystemExit(f"Rejected {_rpc_env_label()}: invalid RPC URL")
    if parsed.username or parsed.password:
        raise SystemExit(f"Rejected {_rpc_env_label()}: RPC URL userinfo is not allowed")
    return parsed


def _is_helius_mainnet_endpoint(endpoint: str) -> bool:
    parsed = _parsed_endpoint(endpoint)
    return parsed.scheme == "https" and (parsed.hostname or "").lower().rstrip(".") == "mainnet.helius-rpc.com"


def _host_is_private_or_reserved(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast)
    except ValueError:
        pass
    try:
        for _family, _typ, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast:
                return True
    except OSError:
        return True
    return False


def _validate_custom_rpc_endpoint(endpoint: str) -> str:
    parsed = _parsed_endpoint(endpoint)
    host = (parsed.hostname or "").lower().rstrip(".")
    allow_private = _flag_enabled("CHAOS_ALLOW_PRIVATE_RPC")
    if "helius-rpc.com" in host and host != "mainnet.helius-rpc.com":
        raise SystemExit(f"Rejected {_rpc_env_label()}: RPC host is not allowlisted")
    if parsed.scheme != "https":
        raise SystemExit(f"Rejected {_rpc_env_label()}: custom RPC must use https")
    if _host_is_private_or_reserved(host) and not allow_private:
        raise SystemExit(f"Rejected {_rpc_env_label()}: private/local RPC requires CHAOS_ALLOW_PRIVATE_RPC=1")
    return endpoint


WALLET_API_BASE = "https://api.helius.xyz"


def rpc_endpoint() -> str:
    """Resolve the JSON-RPC endpoint. SOLANA_RPC_URL wins; HELIUS_API_KEY alone builds the Helius URL."""
    load_env()
    explicit = os.getenv("SOLANA_RPC_URL", "").strip()
    if explicit and "YOUR_KEY" not in explicit:
        if _is_helius_mainnet_endpoint(explicit):
            return explicit
        return _validate_custom_rpc_endpoint(explicit)
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if key and "YOUR_KEY" not in key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit(
        "No RPC configured. Set SOLANA_RPC_URL to an https JSON-RPC endpoint, or set HELIUS_API_KEY. "
        "Both live in CHAOS_HOME/.env; `chaos onboard` writes them."
    )


def is_helius_endpoint() -> bool:
    """True when the configured RPC is Helius mainnet, so Helius-only methods and the Wallet API apply."""
    return _is_helius_mainnet_endpoint(rpc_endpoint())


def helius_endpoint() -> str:
    """Old name. Kept for one release so scripts written against it keep working."""
    return rpc_endpoint()


def wallet_api_key() -> str:
    """The Helius key, needed for the wallet lane: wallet discovery (`smart_wallet_tracker.py`, `wallets --discover`) and deep wallet reads (`wallet_deep.py`)."""
    load_env()
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if not key or "YOUR_KEY" in key:
        raise SystemExit(
            "HELIUS_API_KEY is not set. It is needed for the wallet lane: wallet discovery (`smart_wallet_tracker.py`, `wallets --discover`) "
            "and deep wallet reads (`wallet_deep.py`). Token reads and the paper loop work without it. "
            "Set it in CHAOS_HOME/.env; `chaos onboard` writes it."
        )
    return key


def method_not_served(status: int | None, headers: Any) -> bool:
    """A 429 whose method limit is 0: the RPC never serves this method, so a retry cannot help.

    The public mainnet RPC answers getTokenLargestAccounts this way."""
    return status == 429 and headers is not None and (headers.get("x-ratelimit-method-limit") or "").strip() == "0"


def rpc_request(method: str, params: list[Any] | dict[str, Any] | None = None, *, timeout: int = 30, retries: int = 3) -> Any:
    if method not in ALLOWED_RPC_METHODS:
        raise SystemExit(json.dumps({"ok": False, "method": method, "error": "RPC method is not read-only allowlisted"}, indent=2))
    endpoint = rpc_endpoint()
    body = json.dumps({"jsonrpc": "2.0", "id": "chaos", "method": method, "params": params or []}).encode()
    req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if "error" in payload:
                raise SystemExit(json.dumps({"ok": False, "method": method, "error": payload["error"]}, indent=2))
            return payload.get("result")
        except urllib.error.HTTPError as exc:
            last_error = exc
            # The error carries the open response; close it on both paths so no socket is left to the collector.
            try:
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries or method_not_served(exc.code, exc.headers):
                    try:
                        details = exc.read().decode("utf-8")[:1000]
                    except Exception:
                        details = str(exc)
                    failure = SystemExit(json.dumps({"ok": False, "method": method, "http_status": exc.code, "error": details}, indent=2))
                    failure.headers = exc.headers  # so a caller can read rate-limit headers such as x-ratelimit-method-limit
                    raise failure
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt >= retries:
                raise SystemExit(json.dumps({"ok": False, "method": method, "network_error": str(exc)}, indent=2))
        time.sleep(2 * (2**attempt))  # public RPC rate windows are about 10 s: waits of 2, 4, 8 s
    raise SystemExit(str(last_error or "unknown Helius error"))


def require_address(value: str, label: str = "address") -> str:
    if not BASE58_RE.match(value):
        raise SystemExit(json.dumps({"ok": False, "error": f"Invalid Solana {label} shape", "value_length": len(value)}, indent=2))
    return value


def require_signature(value: str) -> str:
    if not SIG_RE.match(value):
        raise SystemExit(json.dumps({"ok": False, "error": "Invalid Solana transaction signature shape", "value_length": len(value)}, indent=2))
    return value


def safe_print(obj: Any) -> None:
    """Print JSON without leaking the Helius or xAI API key if one appears inside the payload."""
    text = json.dumps(obj, indent=2, sort_keys=False, default=str)
    for name in ("HELIUS_API_KEY", "XAI_API_KEY"):
        key = os.getenv(name, "").strip()
        if key:
            text = text.replace(key, f"<{name}_REDACTED>")
    print(text)


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--raw", action="store_true", help="Return raw summarized JSON envelope instead of Markdown.")
    return parser
