"""The daemon's HTTP surface: one gate in front of every documented route (``api.py``).

Order of checks for every request (threat model: ``contracts/companion.md``):

1. exactly one ``Host`` header naming this listener (``127.0.0.1:<port>``; ``localhost:<port>``
   for bearer callers only), else 403: DNS rebinding never reaches a route;
2. static assets (a fixed table, GET only) need nothing else; they carry no data;
3. a bearer caller must look like a CLI (no foreign ``Origin`` or cross-site fetch
   metadata) and present the daemon's token;
4. a caller with no credential at all gets 401; a companion session must pass the browser
   checks (fetch metadata same-origin, exact ``Origin`` on every state change, JSON
   bodies), name a live session by cookie, present that session's CSRF secret, and call a
   route marked ``companion``.

Every reply, refusal and stdlib error included, is JSON (or the asset, or an event stream)
with the full hardening headers; no reply ever carries CORS grants or a redirect.
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping

from . import api, release, views
from .companion import (CSRF_HEADER, JSON_TYPE, SECURITY_HEADERS, SSE_TYPE, STATIC,
                        CompanionAuth, Refusal, json_bytes, load_static, sse_frame, vet_browser)
from .host import HostError
from .state import NotFoundError, StateError

__all__ = ["Surface"]

MAX_BODY = 1 << 20
MAX_DRAIN = 8 << 20
HEARTBEAT_SECONDS = 10.0
_ERRORS = {400: "Bad request.", 404: "Unknown route.", 405: "Method not allowed.",
           408: "Request timeout.", 411: "Length required.", 413: "Request body is too large.",
           414: "Request line is too long.", 431: "Request headers are too large.",
           501: "Not implemented.", 505: "HTTP version not supported."}


@dataclass
class Access:
    kind: str  # bearer | companion | public
    session: bytes | None = None


class Surface:
    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self.auth = CompanionAuth(lambda: daemon._server.server_address[1])
        self.static = load_static()

    # -- replies ------------------------------------------------------------------------
    @staticmethod
    def _drain(handler: BaseHTTPRequestHandler) -> None:
        """Read (and drop) an unread request body so closing never resets the reply."""
        if getattr(handler, "_body_read", True):
            return
        handler._body_read = True
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except (ValueError, AttributeError):
            return
        if 0 < length <= MAX_DRAIN:
            while length > 0:
                chunk = handler.rfile.read(min(length, 65536))
                if not chunk:
                    break
                length -= len(chunk)

    def send(self, handler: BaseHTTPRequestHandler, status: int, data: bytes, kind: str, *,
             extra: tuple = (), head: bool = False) -> None:
        self._drain(handler)
        handler.send_response(status)
        handler.send_header("Content-Type", kind)
        handler.send_header("Content-Length", str(len(data)))
        for name, value in (*SECURITY_HEADERS, *extra):
            handler.send_header(name, value)
        handler.send_header("Connection", "close")
        handler.end_headers()
        if not head:
            handler.wfile.write(data)
        handler.close_connection = True

    def reply(self, handler, status: int, document: Mapping[str, Any], *, extra: tuple = (),
              head: bool = False) -> None:
        self.send(handler, status, json_bytes(dict(document)), JSON_TYPE, extra=extra, head=head)

    def error(self, handler, status: int, text: str | None = None, *,
              signed_out: str | None = None, **options) -> None:
        document = {"error": text or _ERRORS.get(status, "Request refused.")}
        if signed_out is not None:  # why a tab is not signed in (the page says so)
            document["reason"] = signed_out
        self.reply(handler, status, document, **options)

    # -- the handler class --------------------------------------------------------------
    def handler_class(self) -> type[BaseHTTPRequestHandler]:
        surface = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 30.0
            _body_read = True

            def log_message(self, *_args) -> None:
                return  # nothing about requests is ever logged (tokens, cookies, paths)

            def version_string(self) -> str:
                return "Brainstem-Agent"

            def __getattr__(self, name: str):
                if name.startswith("do_"):
                    return lambda: surface.error(self, 405, head=name == "do_HEAD")
                raise AttributeError(name)

            def send_error(self, code, message=None, explain=None) -> None:
                # Replaces the stdlib HTML error page (bad request lines, 414, 431, 505):
                # JSON, hardened, never echoing the request.
                if getattr(self, "request_version", "HTTP/0.9") in ("HTTP/0.9", ""):
                    self.request_version = "HTTP/1.0"
                try:
                    self.connection.settimeout(0.2)
                    for _ in range(64):
                        if not self.connection.recv(65536):
                            break
                except OSError:
                    pass
                surface.error(self, int(code))

            def do_GET(self) -> None:
                surface.dispatch(self, "GET")

            def do_POST(self) -> None:
                self._body_read = False
                surface.dispatch(self, "POST")

        return Handler

    # -- the gate -------------------------------------------------------------------------
    def dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        port = self.daemon._server.server_address[1]
        hosts = handler.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.auth.refused["host"] += 1
            return self.error(handler, 403, "This surface only answers on its loopback address.")
        companion_host = hosts[0] == f"127.0.0.1:{port}"
        target = handler.path
        if not target.startswith("/") or target.startswith("//"):
            return self.error(handler, 404)
        split = urllib.parse.urlsplit(target)
        path, query = split.path, urllib.parse.parse_qs(split.query, keep_blank_values=True)
        if path in STATIC:
            if not companion_host:
                self.auth.refused["host"] += 1
                return self.error(handler, 403, "Open the companion at its 127.0.0.1 address.")
            data, kind = self.static[path]
            return self.send(handler, 200, data, kind)
        route, params, known = api.match(method, path)
        if route is None:
            return self.error(handler, 405 if known else 404)
        try:
            access = self.authenticate(handler, route, method, companion_host)
        except Refusal as refusal:
            self.auth.refused[refusal.reason] += 1
            if getattr(refusal, "cookie", False):
                self.auth.refused["with_cookie"] += 1
            return self.error(handler, refusal.status, refusal.message,
                              signed_out=refusal.signed_out)
        body: dict = {}
        if method == "POST":
            body = self.read_body(handler)
            if body is None:
                return None
        try:
            return self.route(handler, route, access, params, query, body)
        except Refusal as refusal:  # the login exchange: used, expired or rate-limited
            return self.error(handler, refusal.status, refusal.message)
        except NotFoundError as problem:
            return self.error(handler, 404, _redact(str(problem)))
        except (ValueError, TypeError, HostError, KeyError) as problem:
            return self.error(handler, 400, _redact(str(problem)))
        except StateError as problem:
            status = 503 if route.name in ("turn", "chat", "tool") else 400
            return self.error(handler, status, _redact(f"The cell's store refused: {problem}"))
        except BrokenPipeError:
            return None

    def authenticate(self, handler, route: api.Route, method: str, companion_host: bool) -> Access:
        headers, auth = handler.headers, self.auth
        given = headers.get("Authorization")
        if given is not None:
            origin, site = headers.get("Origin"), headers.get("Sec-Fetch-Site")
            if (origin is not None and origin != auth.origin) or site not in (
                    None, "same-origin", "none"):
                raise Refusal(403, "Browsers cannot use the owner's token.", "browser-bearer")
            token = self.daemon._token
            if not (given.startswith("Bearer ") and hmac.compare_digest(
                    given[7:].encode(), token.encode())):
                raise Refusal(401, "Unauthorized: the daemon only answers its owner.",
                              "unauthenticated")
            return Access("bearer")
        if route.name == "companion.session" and method == "POST":
            if not companion_host:
                raise Refusal(403, "Sign in at the 127.0.0.1 address.", "host")
            vet_browser(headers, method, auth.origin)
            return Access("public")
        cookie = auth.cookie_value(headers.get("Cookie"))
        if cookie is None:
            raise Refusal(401, "Unauthorized: sign in with a link from brainstem-agent open.",
                          "unauthenticated", signed_out="not-signed-in")
        live = auth.session(cookie)
        try:
            if not companion_host:
                raise Refusal(403, "Open the companion at its 127.0.0.1 address.", "host")
            vet_browser(headers, method, auth.origin)
            if live is None:
                why = auth.ended(cookie)
                if why is None:  # a session this daemon never started: it restarted since
                    raise Refusal(401, "Signed out: the daemon restarted since this tab signed "
                                       "in, which ends every companion session. Run "
                                       "brainstem-agent open to sign in again.",
                                  "unauthenticated", signed_out="daemon-restarted")
                raise Refusal(401, f"Signed out: this companion session {why}. Run "
                                   "brainstem-agent open to sign in again.",
                              "unauthenticated", signed_out="signed-out")
            key, entry = live
            if not auth.check_csrf(entry, headers.get(CSRF_HEADER)):
                raise Refusal(403, "Missing or wrong CSRF secret.", "csrf")
            if not route.companion:
                raise Refusal(403, "The companion may not call this route; use the CLI.",
                              "not-allowed")
        except Refusal as refusal:
            refusal.cookie = live is not None
            raise
        auth.touch(key)
        return Access("companion", key)

    def read_body(self, handler) -> dict | None:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            handler._body_read = True
            self.error(handler, 400, "Bad Content-Length.")
            return None
        if length > MAX_BODY:
            self.error(handler, 413)  # drains up to MAX_DRAIN first
            return None
        raw = handler.rfile.read(length) if length else b""
        handler._body_read = True
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self.error(handler, 400, "The body must be a JSON object.")
            return None
        return body

    # -- routes -------------------------------------------------------------------------
    def route(self, handler, route: api.Route, access: Access, params: dict, query: dict,
              body: dict) -> None:
        daemon, host = self.daemon, self.daemon.host
        name = route.name
        first = lambda key, default=None: (query.get(key) or [default])[0]  # noqa: E731
        if name == "api":
            return self.reply(handler, 200, api.describe())
        if name == "status":
            return self.reply(handler, 200, daemon.status())
        if name == "health":
            return self.reply(handler, 200, daemon.health())
        if name == "version":
            return self.reply(handler, 200, release.version_info(daemon.home, verify=False))
        if name == "drain":
            return self.reply(handler, 200, daemon.drain(body))
        if name == "turn":
            return self.reply(handler, 200, daemon.turn(body))
        if name == "chat":  # RAPP/1: exactly response, agent_logs, session_id
            result = daemon.turn({"message": body.get("user_input"),
                                  "session_id": body.get("session_id")})
            if result["ok"]:
                return self.reply(handler, 200, result["response"])
            draining = (result.get("evidence") or {}).get("refused") == "draining"
            return self.reply(handler, 503 if draining else 502 if result["state"] != "cancelled"
                              else 409, {"error": result["error"], "state": result["state"]})
        if name == "tool":
            return self.reply(handler, 200, daemon.tool(body))
        if name == "cancel":
            if body.get("active") is True:  # the `cancel` command, the REPL's /stop
                return self.reply(handler, 200, host.cancel())
            event = daemon._requests.get(body.get("request_id"))
            if event is not None:
                event.set()
            return self.reply(handler, 200, {"cancelled": event is not None})
        if name == "progress":
            return self.reply(handler, 200, daemon.progress(body))
        if name == "wake":
            daemon.scheduler.wake()
            return self.reply(handler, 200, {"ok": True})
        if name == "stop":
            daemon.stop()  # from here on no worker starts; report the ones that exist
            worker = host.worker_status()
            return self.reply(handler, 200, {"ok": True, "stopping": True, "pid": os.getpid(),
                                             "workers": [] if worker is None else [worker]})
        if name == "requests.start":
            if access.kind == "companion":  # the daemon's own workspace only
                body = {key: value for key, value in body.items() if key != "workspace"}
            return self.reply(handler, 202, daemon.start_request(body))
        if name in ("requests.show", "requests.events"):
            request = daemon.requests.get(params["request_id"])
            if request is None:
                return self.error(handler, 404, "No such request (finished long ago, or the "
                                                "daemon restarted).")
            if name == "requests.show":
                return self.reply(handler, 200, request.describe())
            return self.stream(handler, request, int(first("after", "0") or 0))
        if name in ("sessions.list", "sessions.show"):
            active = {(host.active_turn or {}).get("turn_id")} - {None}  # the daemon's own
            if name == "sessions.list":
                return self.reply(handler, 200, views.sessions(host, active=active))
            return self.reply(handler, 200, views.session(host, params["session_id"],
                                                          active=active))
        if name == "journal.show":
            return self.reply(handler, 200, views.journal(host, params["turn_id"]))
        if name == "receipts.list":
            return self.reply(handler, 200, views.receipts(host, first("turn")))
        if name == "schedules.list":
            return self.reply(handler, 200, views.schedules(host, first("all") == "1"))
        if name == "schedules.change":
            return self.reply(handler, 200, views.schedule_change(
                host, params["schedule_id"], params["action"]))
        if name == "inbox.list":
            return self.reply(handler, 200, views.inbox(host, int(first("limit", "20"))))
        if name == "skills.list":
            return self.reply(handler, 200, views.skills(host, first("offered") == "1"))
        if name == "skills.show":
            version = first("version")
            return self.reply(handler, 200, views.skill(host, params["name"],
                                                        int(version) if version else None))
        if name == "skills.review":
            version = body.get("version")
            if version is not None and not isinstance(version, int):
                raise ValueError("version must be an integer")
            return self.reply(handler, 200, views.skill_review(host, params["name"],
                                                               params["action"], version))
        if name == "memory.list":
            return self.reply(handler, 200, views.memory(host, first("scope", "all"),
                                                         first("search")))
        if name == "memory.edit":
            return self.reply(handler, 200, views.memory_edit(
                host, body.get("scope"), body.get("fact_id"), body.get("text")))
        if name == "memory.forget":
            return self.reply(handler, 200, views.memory_forget(host, body.get("scope"),
                                                                body.get("fact_id")))
        if name == "tools.list":
            return self.reply(handler, 200, views.tools(host))
        if name == "mcp.list":
            return self.reply(handler, 200, views.mcp(host))
        if name == "egress.list":
            return self.reply(handler, 200, views.egress(host, int(first("limit", "100"))))
        if name == "companion.login":
            return self.reply(handler, 200, self.auth.mint())
        if name == "companion.revoke":
            return self.reply(handler, 200, {"ok": True, "revoked": self.auth.revoke_all()})
        if name == "companion.session":
            session_id, csrf = self.auth.exchange(body.get("token"))
            return self.reply(handler, 200, {"ok": True, "csrf": csrf, "idle_seconds": 3600},
                              extra=(("Set-Cookie", self.auth.set_cookie(session_id)),))
        if name == "companion.whoami":
            return self.reply(handler, 200, {"signed_in": access.kind == "companion",
                                             "product": "Brainstem Agent",
                                             "origin": self.auth.origin})
        if name == "companion.logout":
            if access.session is not None:
                self.auth.end(access.session)
            return self.reply(handler, 200, {"ok": True, "signed_out": True}, extra=(
                ("Set-Cookie", self.auth.clear_cookie()),
                ("Clear-Site-Data", '"cache", "cookies", "storage"')))
        return self.error(handler, 404)

    def stream(self, handler, request, after: int) -> None:
        """``text/event-stream`` of one request from ``after`` until it has finished."""
        handler.send_response(200)
        handler.send_header("Content-Type", SSE_TYPE)
        for name, value in SECURITY_HEADERS:
            handler.send_header(name, value)
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.close_connection = True
        try:
            while True:
                events, done = request.after(after, HEARTBEAT_SECONDS)
                if events:
                    handler.wfile.write(b"".join(sse_frame(seq, event) for seq, event in events))
                    after = events[-1][0]
                else:
                    handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                if done or self.daemon.stopping.is_set():
                    return
        except OSError:
            return  # the surface went away; the turn itself carries on


def _redact(text: str) -> str:
    from .daemon import _redacted

    return _redacted(text)
