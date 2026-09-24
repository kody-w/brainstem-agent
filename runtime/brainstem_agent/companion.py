"""The companion's owner-only browser surface: login, sessions, the request gate and the
hardening every response carries. Threat model and tests: ``contracts/companion.md``.

One daemon listener (127.0.0.1:<port>) serves the same documented routes to two kinds of
caller. The CLI and agents present the daemon's bearer token (unchanged). A browser tab
presents a **companion session**: an ``HttpOnly; SameSite=Strict`` cookie *and* the
session's CSRF secret in ``X-Brainstem-CSRF``. Cookies are shared by every port of
127.0.0.1 and every such port is same-site, so the cookie alone never authenticates; the
CSRF secret is handed out once, by the login exchange, to the page that exchanged the
one-time token, and a cross-origin page can neither read it nor send a custom header
(the preflight is never granted).

Login tokens are minted for ``brainstem-agent open`` (bearer only): 256 random bits, one
use, 120 s, held only as SHA-256 digests in memory; the URL carries them in the fragment.
Sessions live in memory only (a restart ends them): idle 1 h, absolute 12 h, at most 8.
The daemon remembers (as digests) the sessions it ended itself, so a refused tab learns
whether it was signed out here or the daemon restarted since it signed in.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import secrets
import threading
import time
from importlib import resources
from typing import Any, Callable, Mapping

__all__ = ["CSP", "CompanionAuth", "Refusal", "SECURITY_HEADERS", "STATIC", "json_bytes",
           "load_static", "sse_frame"]

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "object-src 'none'; require-trusted-types-for 'script'; trusted-types 'none'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
)
JSON_TYPE = "application/json; charset=utf-8"
SSE_TYPE = "text/event-stream; charset=utf-8"
# Static assets by URL: a fixed table (URLs never name files).
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/login": ("login.html", "text/html; charset=utf-8"),
    "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/ui/login.js": ("login.js", "text/javascript; charset=utf-8"),
    "/ui/app.css": ("app.css", "text/css; charset=utf-8"),
    "/ui/icon.svg": ("icon.svg", "image/svg+xml"),
}
LOGIN_SECONDS = 120.0
IDLE_SECONDS = 3600.0
ABSOLUTE_SECONDS = 12 * 3600.0
MAX_SESSIONS = 8
MAX_PENDING_LOGINS = 8
MAX_ENDED = 64  # sessions this daemon ended, remembered to tell "signed out" from "restarted"
FAILURE_WINDOW = 60.0
MAX_FAILURES = 20
CSRF_HEADER = "X-Brainstem-CSRF"
_ESCAPES = {ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"}


def json_bytes(document: Any) -> bytes:
    """JSON that is ASCII-only with ``<``, ``>`` and ``&`` escaped: never parseable as
    HTML, never able to break out of any context (U+2028/2029 are escaped too)."""
    return json.dumps(document, ensure_ascii=True).translate(_ESCAPES).encode("ascii")


def sse_frame(seq: int, document: Any) -> bytes:
    """One server-sent event: its sequence number and one line of escaped JSON."""
    return b"id: %d\ndata: " % seq + json_bytes(document) + b"\n\n"


def load_static() -> dict[str, tuple[bytes, str]]:
    """The companion's assets (package data under ``brainstem_agent/ui``), read once."""
    folder = resources.files("brainstem_agent") / "ui"
    return {url: ((folder / name).read_bytes(), kind) for url, (name, kind) in STATIC.items()}


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "replace")).digest()


class Refusal(Exception):
    """A refused request: HTTP status, the message shown, the counter it adds to, and for a
    refused sign-in why (``signed_out``: not-signed-in, signed-out or daemon-restarted)."""

    def __init__(self, status: int, message: str, reason: str, *,
                 signed_out: str | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.reason = status, message, reason
        self.signed_out = signed_out


class CompanionAuth:
    """Login tokens, sessions, refusal counters (thread-safe; memory only)."""

    def __init__(self, port_getter: Callable[[], int], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._port = port_getter
        self.clock = clock
        self._lock = threading.Lock()
        self._logins: dict[bytes, float] = {}
        self._sessions: collections.OrderedDict[bytes, dict] = collections.OrderedDict()
        self._ended: collections.OrderedDict[bytes, str] = collections.OrderedDict()
        self._failures: collections.deque = collections.deque()
        self.refused: collections.Counter = collections.Counter()

    # -- names ------------------------------------------------------------------------
    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._port()}"

    @property
    def cookie_name(self) -> str:
        return f"bsa_session_{self._port()}"

    # -- login tokens -----------------------------------------------------------------
    def mint(self) -> dict:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._logins = {key: until for key, until in self._logins.items() if until > now}
            while len(self._logins) >= MAX_PENDING_LOGINS:
                self._logins.pop(next(iter(self._logins)))
            self._logins[_digest(token)] = now + LOGIN_SECONDS
        return {"url": f"{self.origin}/login#{token}", "expires_in": int(LOGIN_SECONDS),
                "expires_at": time.time() + LOGIN_SECONDS}

    def exchange(self, token: Any) -> tuple[str, str]:
        """One-time token -> (session id, CSRF secret). Raises ``Refusal``."""
        now = self.clock()
        with self._lock:
            while self._failures and self._failures[0] <= now - FAILURE_WINDOW:
                self._failures.popleft()
            if len(self._failures) >= MAX_FAILURES:
                self.refused["login-rate"] += 1
                raise Refusal(429, "Too many failed sign-ins; wait a minute and run "
                                   "brainstem-agent open again.", "login-rate")
            found = None
            if isinstance(token, str) and 0 < len(token) <= 128:
                wanted = _digest(token)
                for key in list(self._logins):
                    if hmac.compare_digest(key, wanted):
                        found = key
            until = self._logins.pop(found, None) if found is not None else None
            if until is None or until <= now:
                self._failures.append(now)
                self.refused["login"] += 1
                raise Refusal(401, "This sign-in link was already used or has expired. Run "
                                   "brainstem-agent open for a new one.", "login")
            session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[_digest(session_id)] = {"csrf": _digest(csrf), "created": now,
                                                   "seen": now}
            while len(self._sessions) > MAX_SESSIONS:
                self._forget(self._sessions.popitem(last=False)[0],
                             f"was replaced by newer sign-ins (at most {MAX_SESSIONS})")
        return session_id, csrf

    def _forget(self, key: bytes, why: str) -> None:
        """Remember that this daemon ended a session, and why (the lock is held)."""
        self._ended[key] = why
        self._ended.move_to_end(key)
        while len(self._ended) > MAX_ENDED:
            self._ended.popitem(last=False)

    def ended(self, cookie_value: str | None) -> str | None:
        """Why this daemon ended the session a cookie names, or None when it never started
        that session (it restarted since, or the cookie is not one of its own)."""
        if not cookie_value or len(cookie_value) > 128:
            return None
        wanted = _digest(cookie_value)
        with self._lock:
            for key, why in self._ended.items():
                if hmac.compare_digest(key, wanted):
                    return why
        return None

    # -- sessions ---------------------------------------------------------------------
    def session(self, cookie_value: str | None) -> tuple[bytes, dict] | None:
        """The live session a cookie names (expired ones are ended here), else None."""
        if not cookie_value or len(cookie_value) > 128:
            return None
        wanted, now = _digest(cookie_value), self.clock()
        with self._lock:
            for key in list(self._sessions):
                entry = self._sessions[key]
                if now - entry["seen"] > IDLE_SECONDS or now - entry["created"] > ABSOLUTE_SECONDS:
                    self._sessions.pop(key)
                    self._forget(key, "expired (idle for an hour, or signed in 12 hours ago)")
                    continue
                if hmac.compare_digest(key, wanted):
                    return key, entry
        return None

    def check_csrf(self, entry: dict, given: str | None) -> bool:
        return bool(given) and len(given) <= 128 and hmac.compare_digest(entry["csrf"],
                                                                         _digest(given))

    def touch(self, key: bytes) -> None:
        with self._lock:
            entry = self._sessions.get(key)
            if entry is not None:
                entry["seen"] = self.clock()
                self._sessions.move_to_end(key)

    def end(self, key: bytes) -> None:
        with self._lock:
            if self._sessions.pop(key, None) is not None:
                self._forget(key, "was signed out")

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            for key in self._sessions:
                self._forget(key, "was ended by brainstem-agent open --sign-out-all")
            self._sessions.clear()
            self._logins.clear()
        return count

    def describe(self) -> dict:
        with self._lock:
            return {"sessions": len(self._sessions), "pending_logins": len(self._logins),
                    "refused": {"with_cookie": 0, **dict(self.refused)}}

    # -- cookies ----------------------------------------------------------------------
    def set_cookie(self, session_id: str) -> str:
        return f"{self.cookie_name}={session_id}; HttpOnly; SameSite=Strict; Path=/"

    def clear_cookie(self) -> str:
        return f"{self.cookie_name}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"

    def cookie_value(self, header: str | None) -> str | None:
        """Our cookie from a Cookie header (the first with our exact name)."""
        for part in (header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.cookie_name:
                return value
        return None


def vet_browser(headers: Mapping[str, str], method: str, origin: str) -> None:
    """Fetch metadata, Origin and content type of a request that uses (or tries to use)
    the companion session. Raises ``Refusal`` (403/415)."""
    site, mode, dest = (headers.get("Sec-Fetch-Site"), headers.get("Sec-Fetch-Mode"),
                        headers.get("Sec-Fetch-Dest"))
    if site is not None and site != "same-origin":
        raise Refusal(403, "Only the companion's own pages may call this.", "fetch-metadata")
    if mode is not None and mode not in ("cors", "same-origin"):
        raise Refusal(403, "Only the companion's own pages may call this.", "fetch-metadata")
    if dest is not None and dest != "empty":
        raise Refusal(403, "Only the companion's own pages may call this.", "fetch-metadata")
    given = headers.get("Origin")
    if (method != "GET" and given is None) or (given is not None and given != origin):
        raise Refusal(403, "Cross-origin requests are refused.", "origin")
    if method != "GET":
        kind = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind != "application/json":
            raise Refusal(415, "Request bodies must be application/json.", "content-type")
