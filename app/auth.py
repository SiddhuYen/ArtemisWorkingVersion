"""Shared-token access control and per-client rate limiting.

Artemis has no user accounts, and adding them would be the wrong shape for what
this is: one private tool shared by a small team. What it needs instead is a
gate, because every build spends real money — search-provider quota and
Anthropic tokens — so an open URL is a bill anyone can run up.

So: ONE token, held by the operator, presented either as
`Authorization: Bearer <token>` (API clients, curl, scripts) or exchanged once
for a session cookie (browsers, via POST /login). Set
`config.ACCESS_TOKEN` to switch it on; leave it unset and the app stays wide
open, which is what a local checkout wants.

Rate limiting is separate from, and independent of, authentication. Holding the
token does not make a caller trusted enough to run unbounded builds — the token
is shared, a browser tab can loop, and a bug in the UI can hammer an endpoint.
The limiter therefore applies whether or not auth is enabled.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import config

_SESSION_SALT = b"artemis-session-v1"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Operator:
    """A resolved identity. Never built from request-supplied data.

    `id` is what every owner-scoped query filters on. `name` is a display
    label, and is also what decides whether a /connect route starting from
    this person may use their own imported contacts — it comes from
    configuration, not from the request, which is the whole point.
    """
    id: str
    name: str


def enabled() -> bool:
    """True when at least one operator token is configured and the gate is live."""
    return bool(config.OPERATORS)


def session_value(token: str) -> str:
    """The cookie value a logged-in browser carries, for one operator's token.

    Deliberately NOT the token: a cookie is stored on disk by the browser and
    is easy to leak through a screenshot, a shared profile, or an extension.
    This is an HMAC of a fixed salt under the token, so it authenticates the
    same secret without ever putting the secret itself where it can be copied
    back out and used against the API.

    Because each operator has a distinct token, this is distinct per operator
    and therefore carries identity. Previously it took no argument and every
    logged-in user got the same value, which is why identity had to come from
    a caller-supplied string.
    """
    return hmac.new(token.encode("utf-8"), _SESSION_SALT, hashlib.sha256).hexdigest()


def _bearer(header: Optional[str]) -> str:
    if not header:
        return ""
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def operator_for_token(candidate: str) -> Optional[Operator]:
    """The operator whose token this is, or None.

    Compares against every configured token in constant time and without early
    exit, so neither the match nor its position leaks through timing. A timing
    side channel on a shared secret is exactly what a public URL invites.
    """
    if not candidate or not enabled():
        return None
    found: Optional[Operator] = None
    for oid, name, token in config.OPERATORS:
        if hmac.compare_digest(candidate, token) and found is None:
            found = Operator(id=oid, name=name)
    return found


def operator_for_cookie(cookie: str) -> Optional[Operator]:
    """The operator this session cookie belongs to, or None."""
    if not cookie or not enabled():
        return None
    found: Optional[Operator] = None
    for oid, name, token in config.OPERATORS:
        if hmac.compare_digest(cookie, session_value(token)) and found is None:
            found = Operator(id=oid, name=name)
    return found


def resolve_operator(headers, cookies) -> Optional[Operator]:
    """The authenticated identity for this request, or None.

    This is the ONLY source of owner identity in the application. Nothing in
    the request body, query string, or a non-auth header may contribute.
    """
    if not enabled():
        # Open mode (local checkout, no token configured). Identity is a fixed
        # constant rather than absent, so owner-scoped queries still filter on
        # something the caller cannot choose.
        return Operator(id=config.ANONYMOUS_OPERATOR_ID,
                        name=config.ANONYMOUS_OPERATOR_ID)
    op = operator_for_token(_bearer(headers.get("authorization")))
    if op is None:
        # Some clients find a bare header easier than an Authorization one.
        op = operator_for_token((headers.get("x-artemis-token") or "").strip())
    if op is None:
        op = operator_for_cookie(cookies.get(config.SESSION_COOKIE) or "")
    return op


def request_authenticated(headers, cookies) -> bool:
    """True when this request carries a recognised token or session cookie."""
    return resolve_operator(headers, cookies) is not None


def client_key(client_host: str, headers) -> str:
    """Identify the caller for rate limiting.

    Behind a managed host the socket peer is always the platform's proxy, so
    every user would share one bucket. X-Forwarded-For fixes that, but only the
    RIGHTMOST entry is trustworthy: the proxy APPENDS the address that actually
    connected to it, while anything further left was supplied by the caller and
    can be forged. Hence rightmost, and hence the TRUST_PROXY_HEADERS flag —
    exposed directly, this header is attacker-controlled and using it would let
    one client mint unlimited identities.
    """
    if config.TRUST_PROXY_HEADERS:
        xff = headers.get("x-forwarded-for") or ""
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return client_host or "unknown"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
class RateLimiter:
    """Token bucket per client key.

    Chosen over a fixed window because the usage is bursty and legitimate: a
    user runs three searches back to back, then nothing for an hour. A fixed
    window would reject the third and then forgive everything at an arbitrary
    boundary. A bucket lets the burst through and throttles only sustained use.

    In-memory and per-process, which matches the deployment (one worker, one
    box). It is a cost guardrail, not a security boundary — a restart forgives
    outstanding debt, and that is an accepted trade for having no dependency.
    """

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = max(1, int(limit))
        self.window_s = max(1.0, float(window_s))
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = threading.Lock()

    @property
    def _rate(self) -> float:
        return self.limit / self.window_s

    def check(self, key: str) -> Tuple[bool, float]:
        """Spend one token. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            tokens, last = self._buckets.get(key, (float(self.limit), now))
            tokens = min(float(self.limit), tokens + (now - last) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, max(1.0, (1.0 - tokens) / self._rate)

    def _evict(self, now: float) -> None:
        """Drop fully-refilled buckets so the dict can't grow without bound.

        A caller at full tokens is indistinguishable from one that has never
        been seen, so forgetting them changes no decision — it just stops a
        long-running process from accumulating an entry per distinct IP.
        """
        if len(self._buckets) < 4096:
            return
        full_after = self.window_s
        self._buckets = {
            k: v for k, v in self._buckets.items()
            if not (v[0] >= self.limit or (now - v[1]) > full_after)
        }

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


build_limiter = RateLimiter(config.BUILD_RATE_LIMIT, config.BUILD_RATE_WINDOW_S)
login_limiter = RateLimiter(config.LOGIN_RATE_LIMIT, config.LOGIN_RATE_WINDOW_S)


def status() -> dict:
    """Auth configuration for /status. Never exposes the token."""
    return {
        "required": enabled(),
        "builds_per_window": config.BUILD_RATE_LIMIT,
        "window_seconds": int(config.BUILD_RATE_WINDOW_S),
    }
