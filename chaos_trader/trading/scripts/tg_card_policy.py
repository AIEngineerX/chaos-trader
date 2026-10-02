#!/usr/bin/env python3
"""Deterministic Telegram card/button policy for Chaos read-only UX.

No trading, wallet, signing, hidden-link, deep-link, or external-bot affordances.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from chaos_home import chaos_home  # noqa: E402
PROFILE_HOME = chaos_home()
ALLOWED_ARTIFACT_ROOTS = (
    PROFILE_HOME / "trading" / "research",
    PROFILE_HOME / "trading" / "reports",
    PROFILE_HOME / "trading" / "watchlists",
    PROFILE_HOME / "trading" / "owner",
)
ALLOWED_ARTIFACT_SUFFIXES = {".csv", ".json", ".txt"}
ALLOWED_URL_HOSTS = {"dexscreener.com", "solscan.io"}
URL_SHORTENER_HOSTS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "cutt.ly", "rebrand.ly", "shorturl.at", "tiny.cc", "lnkd.in", "trib.al",
}
EXECUTION_LABEL_RE = re.compile(
    r"\b(buy|sell|swap|snipe|copy\s*trade|copytrade|connect|verify|claim|"
    r"import\s*wallet|export\s*key|sign|approve|airdrop|bridge|withdraw|deposit)\b",
    re.IGNORECASE,
)
PRIVATE_PAYLOAD_RE = re.compile(
    r"(cluster|owner|watchlist|wallets=|wallet_list|report_path|label=|note=|"
    r"session|token=|api[_-]?key|secret|auth)",
    re.IGNORECASE,
)

Decision = Literal["allow", "downgrade_to_plain_text", "requires_approval", "block"]


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    button: dict[str, Any] | None = None
    plain_text: str | None = None


def _one_action(button: dict[str, Any]) -> list[str]:
    return [k for k in ("url", "copy_text", "callback", "artifact_path") if button.get(k) not in (None, "", [], {})]


def opaque_callback_id(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts if p not in (None, "", [], {})) or os.urandom(16).hex()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _is_local_or_private_host(hostname: str) -> bool:
    host = hostname.strip().strip("[]").lower()
    if host in {"localhost", "local", "broadcasthost"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    except ValueError:
        pass
    try:
        for _family, _typ, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return True
    except OSError:
        return False
    return False


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(str(url).strip())
    scheme = parsed.scheme.lower()
    if scheme != "https":
        raise ValueError("only https URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("userinfo in URL is not allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL host is required")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("non-ascii/confusable host is not allowed") from exc
    if host in URL_SHORTENER_HOSTS:
        raise ValueError("URL shorteners are not allowed")
    if host not in ALLOWED_URL_HOSTS:
        raise ValueError(f"host {host!r} is not allowlisted")
    if _is_local_or_private_host(host):
        raise ValueError("local/private host is not allowed")
    payload = unquote(urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment)))
    if parsed.query or parsed.fragment:
        raise ValueError("chart/explorer URLs must not include query strings or fragments")
    if host == "dexscreener.com" and not re.fullmatch(r"/solana/[1-9A-HJ-NP-Za-km-z]{3,100}/?", parsed.path or ""):
        raise ValueError("DEXScreener URL path is not an allowlisted token chart")
    if host == "solscan.io" and not re.fullmatch(r"/token/[1-9A-HJ-NP-Za-km-z]{3,100}/?", parsed.path or ""):
        raise ValueError("Solscan URL path is not an allowlisted token explorer path")
    if PRIVATE_PAYLOAD_RE.search(payload):
        raise ValueError("external URL contains private/research payload markers")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _validate_label(label: str) -> str | None:
    text = str(label or "").strip()
    if not text:
        return "button label is required"
    if EXECUTION_LABEL_RE.search(text):
        return "execution-looking button labels are prohibited"
    return None


def _artifact_allowed(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if p.suffix.lower() not in ALLOWED_ARTIFACT_SUFFIXES:
        raise ValueError("artifact type must be CSV, JSON, or TXT")
    if not any(p == root.resolve() or root.resolve() in p.parents for root in ALLOWED_ARTIFACT_ROOTS):
        raise ValueError("artifact path is outside approved Chaos trading directories")
    return p


def validate_button_policy(button: dict[str, Any]) -> PolicyDecision:
    candidate = dict(button)
    label = str(candidate.get("text") or "").strip()
    label_error = _validate_label(label)
    if label_error:
        return PolicyDecision("block", label_error, plain_text=label or None)
    actions = _one_action(candidate)
    if len(actions) != 1:
        return PolicyDecision("block", "exactly one button action field is required", plain_text=label)
    action = actions[0]
    try:
        if action == "copy_text":
            text = str(candidate.get("copy_text") or "")
            if not (1 <= len(text) <= 256):
                return PolicyDecision("downgrade_to_plain_text", "copy_text must be 1-256 characters", plain_text=text or label)
            if not label.lower().startswith("copy"):
                return PolicyDecision("block", "copy_text labels must start with Copy", plain_text=label)
            return PolicyDecision("allow", "copy button allowed", button=candidate)
        if action == "url":
            canonical = canonicalize_url(str(candidate["url"]))
            host = urlsplit(canonical).hostname or ""
            if f": {host}" not in label and host not in label:
                return PolicyDecision("block", "URL button label must disclose normalized host", plain_text=f"{label}: {canonical}")
            if not label.lower().startswith("open "):
                return PolicyDecision("block", "URL button labels must start with Open", plain_text=f"{label}: {canonical}")
            candidate["url"] = canonical
            return PolicyDecision("allow", "read-only allowlisted URL", button=candidate)
        if action == "callback":
            cb = dict(candidate.get("callback") or {})
            if cb.get("action") not in {"show_risk", "show_wallets", "show_full_wallets"}:
                return PolicyDecision("block", "callback action is not read-only allowlisted", plain_text=label)
            opaque = str(cb.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,48}", opaque):
                return PolicyDecision("block", "callback id must be opaque short token", plain_text=label)
            candidate["callback"] = cb
            return PolicyDecision("allow", "read-only callback allowed", button=candidate)
        if action == "artifact_path":
            p = _artifact_allowed(str(candidate["artifact_path"]))
            candidate["artifact_path"] = str(p)
            return PolicyDecision("allow", "local artifact allowed", button=candidate)
    except ValueError as exc:
        return PolicyDecision("block", str(exc), plain_text=label)
    return PolicyDecision("block", "unsupported button action", plain_text=label)


def validate_button_rows(rows: list[list[dict[str, Any]]]) -> tuple[list[list[dict[str, Any]]], list[PolicyDecision]]:
    allowed: list[list[dict[str, Any]]] = []
    decisions: list[PolicyDecision] = []
    for row in rows:
        out_row = []
        for button in row:
            decision = validate_button_policy(button)
            decisions.append(decision)
            if decision.decision == "allow" and decision.button:
                out_row.append(decision.button)
        if out_row:
            allowed.append(out_row)
    return allowed, decisions
