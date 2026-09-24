"""Hostile specs for the companion's HTTP surface (threat model: contracts/companion.md).

Written before the companion: each test attacks one threat (T1-T14, T17) against a real
daemon (scripted workers) with raw HTTP whose every header the attacker controls, and
proves the refusal and that nothing happened. Unit tier: no Grail process, no inference.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from acceptance_support import criteria, leaks, private_dir, run_cli, wait_until
from companion_support import HOSTILE, Harness, parse_sse

UI = Path(__file__).resolve().parents[1] / "brainstem_agent" / "ui"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "object-src 'none'; require-trusted-types-for 'script'; trusted-types 'none'")
FOREIGN_HOSTS = ["evil.example:{p}", "rebind.test:{p}", "127.0.0.1.evil.test:{p}",
                 "localhost.evil.test:{p}", "127.0.0.1:{q}", "127.0.0.1", "localhost",
                 "[::1]:{p}", "0.0.0.0:{p}", "127.1:{p}", "2130706433:{p}", "0x7f000001:{p}",
                 "127.0.0.1:{p} evil", "127.0.0.1:{p}.", "", "LOCALHOST.EVIL:{p}"]
PAGES = [("GET", "/"), ("GET", "/login"), ("GET", "/ui/app.js"), ("GET", "/ui/app.css"),
         ("GET", "/v1/status"), ("GET", "/v1/api"), ("GET", "/v1/sessions"),
         ("POST", "/v1/requests"), ("POST", "/v1/companion/session")]


def assert_hardened(test: unittest.TestCase, response, *, kind: str | None = None) -> None:
    """Every response, success or refusal, carries the full browser hardening."""
    test.assertEqual(response.header("Content-Security-Policy"), CSP)
    test.assertEqual(response.header("X-Frame-Options"), "DENY")
    test.assertEqual(response.header("X-Content-Type-Options"), "nosniff")
    test.assertEqual(response.header("Referrer-Policy"), "no-referrer")
    test.assertEqual(response.header("Cross-Origin-Opener-Policy"), "same-origin")
    test.assertEqual(response.header("Cross-Origin-Resource-Policy"), "same-origin")
    test.assertEqual(response.header("Cache-Control"), "no-store")
    for name, _value in response.header_list:
        test.assertFalse(name.lower().startswith("access-control-"), name)
    test.assertNotIn("Python", response.header("Server") or "")
    if kind is not None:
        test.assertEqual(response.header("Content-Type"), kind)


class CompanionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = private_dir(prefix="ba-companion-")
        cls.harness = Harness(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()
        from acceptance_support import remove_tree
        remove_tree(cls.root)

    def turns_started(self) -> int:
        return len(self.harness.cell.host.store.list_sessions(self.harness.cell.host.namespace))


class HostAllowlist(CompanionCase):
    @criteria("G4")
    def test_t1_dns_rebinding_hosts_are_refused_on_every_route(self):
        h = self.harness
        before = self.turns_started()
        session = h.login()
        for template in FOREIGN_HOSTS:
            host = template.format(p=h.port, q=h.port + 1)
            for method, path in PAGES:
                for headers in ({}, session.headers(method),
                                {"Authorization": "Bearer " + h.token}):
                    answer = h.raw(method, path, host=host, headers=headers,
                                   body={} if method == "POST" else None)
                    self.assertEqual(answer.status, 403, (host, method, path))
                    assert_hardened(self, answer, kind="application/json; charset=utf-8")
        for method, path in PAGES:  # no Host at all, and a duplicated Host
            for host in (None, [h.host_header, "evil.example"], [h.host_header] * 2):
                answer = h.raw(method, path, host=host, headers=session.headers(method),
                               body={} if method == "POST" else None)
                self.assertEqual(answer.status, 403, (host, method, path))
        self.assertEqual(self.turns_started(), before)

    @criteria("G4")
    def test_t1_localhost_name_is_only_for_the_cli(self):
        h = self.harness
        answer = h.bearer("GET", "/v1/status", host=f"localhost:{h.port}")
        self.assertEqual(answer.status, 200)  # the always-on daemon's CLI behaviour, unchanged
        session = h.login()
        for method, path in PAGES:
            answer = h.raw(method, path, host=f"localhost:{h.port}",
                           headers=session.headers(method), body={} if method == "POST" else None)
            self.assertEqual(answer.status, 403, path)


class CrossSiteRequests(CompanionCase):
    @criteria("G4")
    def test_t2_state_changes_need_the_exact_origin(self):
        h = self.harness
        before = self.turns_started()
        session = h.login()
        requests_before = h.cell.requests.count()
        body = {"message": "[[say \"should never run\"]]"}
        for origin in (None, "null", "https://evil.example", f"http://127.0.0.1:{h.port + 1}",
                       f"http://localhost:{h.port}", f"http://127.0.0.1:{h.port}/",
                       f"https://127.0.0.1:{h.port}", f"http://127.0.0.1:{h.port}.evil.test",
                       f"http://127.0.0.1"):
            answer = h.raw("POST", "/v1/requests", body=body,
                           headers=session.headers("POST", Origin=origin))
            self.assertEqual(answer.status, 403, origin)
            assert_hardened(self, answer)
        self.assertEqual(self.turns_started(), before)
        self.assertEqual(h.cell.requests.count(), requests_before)

    @criteria("G4")
    def test_t2_only_json_bodies_are_accepted(self):
        h = self.harness
        before = self.turns_started()
        session = h.login()
        for kind in (None, "text/plain", "text/plain;charset=UTF-8",
                     "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                     "application/json-evil", "text/json"):
            answer = h.raw("POST", "/v1/requests", body=b'{"message": "[[say \\"no\\"]]"}',
                           headers=session.headers("POST", **{"Content-Type": kind}))
            self.assertEqual(answer.status, 415, kind)
        form = h.raw("POST", "/v1/requests", body=b"message=%5B%5Bsay%20x%5D%5D",
                     headers=session.headers(
                         "POST", **{"Content-Type": "application/x-www-form-urlencoded"}))
        self.assertEqual(form.status, 415)
        self.assertEqual(self.turns_started(), before)

    @criteria("G4")
    def test_t2_fetch_metadata_from_other_sites_is_refused(self):
        h = self.harness
        before = self.turns_started()
        session = h.login()
        for extra in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                      {"Sec-Fetch-Site": "none"}, {"Sec-Fetch-Mode": "no-cors"},
                      {"Sec-Fetch-Mode": "navigate"}, {"Sec-Fetch-Dest": "script"},
                      {"Sec-Fetch-Dest": "iframe"}, {"Sec-Fetch-Mode": "websocket"}):
            for method, path in (("POST", "/v1/requests"), ("GET", "/v1/sessions"),
                                 ("GET", "/v1/status"), ("POST", "/v1/cancel")):
                answer = h.raw(method, path, headers=session.headers(method, **extra),
                               body={"message": "[[say \"no\"]]"} if method == "POST" else None)
                self.assertEqual(answer.status, 403, (extra, path))
        self.assertEqual(self.turns_started(), before)

    @criteria("G4")
    def test_t2_csrf_secret_is_required_and_exact(self):
        h = self.harness
        before = self.turns_started()
        session, other = h.login(), h.login()
        for csrf in (None, "", "x" * 43, other.csrf, session.csrf + "x", session.csrf.upper()):
            for method, path in (("POST", "/v1/requests"), ("GET", "/v1/sessions")):
                answer = h.raw(method, path, headers=session.headers(
                    method, **{"X-Brainstem-CSRF": csrf}),
                    body={"message": "[[say \"no\"]]"} if method == "POST" else None)
                self.assertEqual(answer.status, 403, (csrf and csrf[:4], path))
        self.assertEqual(self.turns_started(), before)
        # The control: the same request with the right secret works.
        ok = session.call("POST", "/v1/requests", {"message": "[[say \"allowed\"]]"})
        self.assertEqual(ok.status, 202, ok.body[:200])
        events = session.events(ok.json()["request_id"])
        self.assertEqual(events[-1]["event"], "request.finished")


class PortSharedCookies(CompanionCase):
    @criteria("G4")
    def test_t3_a_leaked_cookie_alone_reads_and_changes_nothing(self):
        h = self.harness
        before = self.turns_started()
        session = h.login()
        cookie_only = {"Cookie": session.cookie}
        # A server on another 127.0.0.1 port receives the cookie and replays it without a
        # browser; a page on another port sends it with its own Origin.
        for headers in (cookie_only, {**cookie_only, "Origin": f"http://127.0.0.1:{h.port + 7}"},
                        {**cookie_only, "Sec-Fetch-Site": "same-site"}):
            for path in ("/v1/sessions", "/v1/memory", "/v1/skills", "/v1/status",
                         "/v1/companion/session", "/v1/egress"):
                answer = h.raw("GET", path, headers=headers)
                self.assertEqual(answer.status, 403, path)
                self.assertNotIn(b"session_", answer.body)
            answer = h.raw("POST", "/v1/requests", body={"message": "[[say \"no\"]]"},
                           headers={**headers, "Content-Type": "application/json"})
            self.assertIn(answer.status, (403, 415))
        self.assertEqual(self.turns_started(), before)

    @criteria("G4")
    def test_t3_the_csrf_secret_is_given_once_and_never_again(self):
        h = self.harness
        session = h.login()
        for path in ("/v1/companion/session", "/v1/status", "/v1/api", "/v1/sessions"):
            answer = session.call("GET", path)
            self.assertEqual(answer.status, 200, path)
            self.assertNotIn(session.csrf.encode(), answer.body)
            self.assertNotIn(session.cookie.split("=", 1)[1].encode(), answer.body)
        self.assertTrue(session.call("GET", "/v1/companion/session").json()["signed_in"])


class CrossOriginReads(CompanionCase):
    @criteria("G4")
    def test_t4_preflights_are_never_granted(self):
        h = self.harness
        for path in ("/v1/requests", "/v1/sessions", "/v1/companion/session", "/"):
            answer = h.raw("OPTIONS", path, headers={
                "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-brainstem-csrf, content-type",
                "Access-Control-Request-Private-Network": "true"})
            self.assertEqual(answer.status, 405, path)
            assert_hardened(self, answer, kind="application/json; charset=utf-8")
        for method in ("PUT", "DELETE", "PATCH", "TRACE", "HEAD", "CONNECT", "PROPFIND"):
            answer = h.raw(method, "/v1/sessions", headers={"Cookie": "x=y"})
            self.assertEqual(answer.status, 405, method)
            if method != "HEAD":
                self.assertEqual(answer.json().get("error") is not None, True)

    @criteria("G4")
    def test_t4_data_is_an_escaped_json_object(self):
        h = self.harness
        session = h.login()
        started = session.call("POST", "/v1/requests",
                               {"message": HOSTILE + " [[say \"plain\"]]"})
        events = session.events(started.json()["request_id"])
        self.assertEqual(events[-1]["result"]["state"], "succeeded")
        for path in ("/v1/sessions", f"/v1/sessions/{events[-1]['result']['session_id']}"):
            answer = session.call("GET", path)
            self.assertEqual(answer.status, 200)
            assert_hardened(self, answer, kind="application/json; charset=utf-8")
            self.assertTrue(answer.body.startswith(b"{"))
            self.assertNotIn(b"<", answer.body)
            self.assertNotIn(b">", answer.body)
            self.assertIn(b"\\u003cscript\\u003e", answer.body)
            self.assertIn("<script>", json.dumps(answer.json()))  # decodes to the text itself


class LoginTokens(CompanionCase):
    @criteria("G2", "G4")
    def test_t5_the_login_link_is_one_time_fragment_only_and_short_lived(self):
        h = self.harness
        url = h.mint()
        self.assertRegex(url, rf"^http://127\.0\.0\.1:{h.port}/login#[A-Za-z0-9_-]{{43}}$")
        token = url.split("#", 1)[1]
        first = h.exchange(token)
        self.assertEqual(first.status, 200)
        assert_hardened(self, first, kind="application/json; charset=utf-8")
        self.assertEqual(h.exchange(token).status, 401)  # used
        late = h.mint().split("#", 1)[1]
        clock = h.cell.companion.clock
        try:
            now = clock()
            h.cell.companion.clock = lambda: now + 121
            self.assertEqual(h.exchange(late).status, 401)  # expired
        finally:
            h.cell.companion.clock = clock
        self.assertEqual(h.exchange("A" * 43).status, 401)

    @criteria("G4")
    def test_t5_tokens_in_query_strings_and_foreign_exchanges_are_ignored(self):
        h = self.harness
        token = h.mint().split("#", 1)[1]
        page = h.raw("GET", f"/login?token={token}&next=https://evil.example",
                     headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"})
        self.assertEqual(page.status, 200)
        self.assertIsNone(page.header("Set-Cookie"))
        self.assertIsNone(page.header("Location"))
        self.assertNotIn(token.encode(), page.body)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": None},
                        {"Sec-Fetch-Site": "cross-site"}, {"Content-Type": "text/plain"}):
            answer = h.exchange(token, headers=headers)
            self.assertIn(answer.status, (403, 415), headers)
            self.assertIsNone(answer.header("Set-Cookie"))
        self.assertEqual(h.exchange(token).status, 200)  # refused attempts did not burn it

    @criteria("G4")
    def test_t5_repeated_failures_are_rate_limited(self):
        root = private_dir(self)
        harness = Harness(root)
        try:
            good = harness.mint().split("#", 1)[1]
            codes = [harness.exchange(f"{index:043d}").status for index in range(20)]
            self.assertEqual(set(codes), {401})
            self.assertEqual(harness.exchange(good).status, 429)
            clock = harness.cell.companion.clock
            now = clock()
            harness.cell.companion.clock = lambda: now + 61
            self.assertEqual(harness.exchange(good).status, 200)
        finally:
            harness.stop()


class Sessions(CompanionCase):
    @criteria("G4")
    def test_t6_the_cookie_is_httponly_strict_and_per_port(self):
        h = self.harness
        token = h.mint().split("#", 1)[1]
        answer = h.exchange(token, headers={"Cookie": f"bsa_session_{h.port}=attacker-chosen"})
        cookie = answer.header("Set-Cookie")
        parts = [part.strip() for part in cookie.split(";")]
        name, value = parts[0].split("=", 1)
        self.assertEqual(name, f"bsa_session_{h.port}")
        self.assertNotEqual(value, "attacker-chosen")  # no fixation
        self.assertGreaterEqual(len(value), 43)
        self.assertNotEqual(value, token)
        self.assertNotEqual(value, answer.json()["csrf"])
        flags = {part.split("=")[0].lower(): part for part in parts[1:]}
        self.assertIn("httponly", flags)
        self.assertEqual(flags.get("samesite"), "SameSite=Strict")
        self.assertEqual(flags.get("path"), "Path=/")
        for absent in ("domain", "expires", "max-age"):
            self.assertNotIn(absent, flags)  # a session cookie for this host only

    @criteria("G4")
    def test_t6_logout_revoke_and_expiry_end_sessions(self):
        h = self.harness
        session = h.login()
        answer = session.call("POST", "/v1/companion/logout")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.header("Clear-Site-Data"), '"cache", "cookies", "storage"')
        self.assertIn("Max-Age=0", answer.header("Set-Cookie"))
        self.assertEqual(session.call("GET", "/v1/status").status, 401)
        one, two = h.login(), h.login()
        revoked = h.bearer("POST", "/v1/companion/revoke", {})
        self.assertEqual(revoked.status, 200)
        self.assertGreaterEqual(revoked.json()["revoked"], 2)
        self.assertEqual(one.call("GET", "/v1/status").status, 401)
        self.assertEqual(two.call("GET", "/v1/status").status, 401)
        idle = h.login()
        clock = h.cell.companion.clock
        try:
            now = clock()
            h.cell.companion.clock = lambda: now + 3601
            self.assertEqual(idle.call("GET", "/v1/status").status, 401)
        finally:
            h.cell.companion.clock = clock
        sessions = [h.login() for _ in range(9)]  # at most 8: the oldest is ended
        self.assertEqual(sessions[0].call("GET", "/v1/status").status, 401)
        self.assertEqual(sessions[-1].call("GET", "/v1/status").status, 200)

    @criteria("G4", "G5")
    def test_t6_a_daemon_restart_ends_every_session(self):
        root = private_dir(self)
        first = Harness(root)
        session = first.login()
        cookie, csrf = session.cookie, session.csrf
        first.stop()
        second = Harness(root, workspace=first.workspace)
        try:
            renamed = f"bsa_session_{second.port}=" + cookie.split("=", 1)[1]
            answer = second.raw("GET", "/v1/status", headers={
                "Cookie": renamed, "X-Brainstem-CSRF": csrf, "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(answer.status, 401)
            self.assertIn("sign in", answer.json()["error"].lower())
        finally:
            second.stop()


class Restarts(unittest.TestCase):
    """G5: an open page can tell "the daemon restarted" from "unreachable". The daemon comes
    back on its previous port when nothing else listens there (the tab keeps its origin),
    and tells a tab whose session it never started that it restarted; a session it ended
    itself (sign-out, revoke, expiry) is "signed out"."""

    @criteria("G5")
    def test_g5_a_restarted_daemon_comes_back_on_its_previous_port(self):
        root = private_dir(self)
        first = Harness(root)
        port = first.port
        first.stop()
        record = first.home / "run" / "port.json"
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        second = Harness(root, workspace=first.workspace)
        try:
            self.assertEqual(second.port, port)
        finally:
            second.stop()

    @criteria("G4", "G5")
    def test_the_companion_listener_binds_without_reverse_dns(self):
        """The companion is served by the daemon's own listener, a LoopbackHTTPServer: it
        binds (fresh and on its previous port) and serves pages without asking DNS about
        its address (the stdlib lookup stalls for tens of seconds on some hosts)."""
        from unittest import mock

        from brainstem_agent.broker import LoopbackHTTPServer

        def refuse(*_arguments):
            raise AssertionError("a reverse DNS lookup of the listener's own address")

        root = private_dir(self)
        with mock.patch("socket.getfqdn", refuse), mock.patch("socket.gethostbyaddr", refuse):
            for _start in ("fresh", "previous port"):
                harness = Harness(root)
                try:
                    self.assertIsInstance(harness.cell._server, LoopbackHTTPServer)
                    self.assertEqual(harness.cell._server.server_name, "127.0.0.1")
                    self.assertEqual(harness.raw("GET", "/").status, 200)
                    self.assertEqual(harness.login().call("GET", "/v1/status").status, 200)
                finally:
                    harness.stop()

    @criteria("G4", "G5")
    def test_g5_a_previous_port_someone_else_listens_on_is_left_alone(self):
        import socket

        from brainstem_agent import daemon

        root = private_dir(self)
        first = Harness(root)
        port = first.port
        first.stop()
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", port))
        squatter.listen(4)
        try:
            self.assertFalse(daemon.port_is_free(port))
            second = Harness(root, workspace=first.workspace)
            try:
                self.assertNotEqual(second.port, port)
                with socket.create_connection(("127.0.0.1", port), timeout=10):
                    accepted, _address = squatter.accept()  # the squatter still has its port
                    accepted.close()
            finally:
                second.stop()
        finally:
            squatter.close()
        self.assertTrue(daemon.port_is_free(port))

    @criteria("G4", "G5")
    def test_g5_a_tab_from_before_a_restart_is_told_the_daemon_restarted(self):
        from companion_support import Session

        root = private_dir(self)
        first = Harness(root)
        before = first.login()
        first.stop()
        second = Harness(root, workspace=first.workspace)
        try:
            self.assertEqual(second.port, first.port)  # the same origin: the tab can ask
            answer = Session(second, before.cookie, before.csrf).call("GET", "/v1/status")
            self.assertEqual((answer.status, answer.json()["reason"]), (401, "daemon-restarted"))
            self.assertIn("daemon restarted", answer.json()["error"])
            assert_hardened(self, answer)
            # A session this daemon ended itself is signed out, never "restarted".
            mine = second.login()
            self.assertEqual(mine.call("POST", "/v1/companion/logout").status, 200)
            again = mine.call("GET", "/v1/status")
            self.assertEqual((again.status, again.json()["reason"]), (401, "signed-out"))
            revoked = second.login()
            self.assertEqual(second.bearer("POST", "/v1/companion/revoke", {}).status, 200)
            self.assertEqual(revoked.call("GET", "/v1/status").json()["reason"], "signed-out")
            idle = second.login()
            clock = second.cell.companion.clock
            now = clock()
            second.cell.companion.clock = lambda: now + 3601
            try:
                self.assertEqual(idle.call("GET", "/v1/status").json()["reason"], "signed-out")
            finally:
                second.cell.companion.clock = clock
            bare = second.raw("GET", "/v1/status", headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual((bare.status, bare.json()["reason"]), (401, "not-signed-in"))
        finally:
            second.stop()


class Framing(CompanionCase):
    @criteria("G4")
    def test_t7_t9_t10_every_response_is_hardened(self):
        h = self.harness
        session = h.login()
        kinds = {"/": "text/html; charset=utf-8", "/login": "text/html; charset=utf-8",
                 "/ui/app.js": "text/javascript; charset=utf-8",
                 "/ui/login.js": "text/javascript; charset=utf-8",
                 "/ui/app.css": "text/css; charset=utf-8",
                 "/ui/icon.svg": "image/svg+xml"}
        for path, kind in kinds.items():
            answer = h.raw("GET", path, headers={"Sec-Fetch-Mode": "navigate"})
            self.assertEqual(answer.status, 200, path)
            assert_hardened(self, answer, kind=kind)
        json_kind = "application/json; charset=utf-8"
        for answer in (session.call("GET", "/v1/status"), h.raw("GET", "/v1/status"),
                       h.raw("GET", "/nope"), h.raw("GET", "/v1/nope", headers=session.headers()),
                       h.raw("DELETE", "/"), h.raw("GET", "/", host="evil.example"),
                       h.raw("POST", "/v1/requests", headers=session.headers(
                           "POST", **{"Content-Type": "text/plain"}), body=b"{}"),
                       h.raw("POST", "/v1/requests", headers=session.headers("POST"),
                             body=b"x" * ((1 << 20) + 1))):
            assert_hardened(self, answer, kind=json_kind)
            self.assertTrue(answer.body.startswith(b"{"), answer.body[:80])

    @criteria("G4")
    def test_t9_malformed_requests_get_json_not_the_stdlib_html_page(self):
        import socket

        h = self.harness
        for raw in (b"GARBAGE\r\n\r\n", b"GET /v1/status HTTP/9.9\r\nHost: x\r\n\r\n",
                    b"GET " + b"/" * 70000 + b" HTTP/1.1\r\n\r\n",
                    b"GET /<script>alert(1)</script> HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                    % h.port):
            with socket.create_connection(("127.0.0.1", h.port), timeout=10) as connection:
                connection.sendall(raw)
                data = b""
                while chunk := connection.recv(65536):
                    data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            self.assertIn(b"Content-Type: application/json; charset=utf-8", head)
            self.assertIn(b"X-Content-Type-Options: nosniff", head)
            self.assertNotIn(b"<html", body.lower())
            self.assertNotIn(b"<script>", body)


class Redirects(CompanionCase):
    @criteria("G4")
    def test_t11_no_redirects_and_no_paths_outside_the_asset_table(self):
        h = self.harness
        for path in ("/login?next=https://evil.example", "//evil.example", "/\\evil.example",
                     "/ui/../companion.py", "/ui/%2e%2e/companion.py", "/ui/..%2fcompanion.py",
                     "/ui/app.js/..", "/ui/", "/ui", "/etc/passwd", "/ui//etc/passwd",
                     "/ui/%2Fetc%2Fpasswd", "/ui/app.js%00.css", "/ui/APP.JS",
                     "/../daemon.py", "/v1/../ui/app.js", "/index.html", "/ui/index.html.bak"):
            answer = h.raw("GET", path, headers={"Sec-Fetch-Mode": "navigate"})
            self.assertFalse(300 <= answer.status < 400, path)
            self.assertIsNone(answer.header("Location"), path)
            if path.startswith("/login"):
                self.assertEqual(answer.status, 200)
            else:
                self.assertIn(answer.status, (401, 403, 404), path)
                self.assertNotIn(b"import ", answer.body)
        self.assertEqual(h.raw("GET", "/ui/app.js?v=1").status, 200)


class LocalProcesses(CompanionCase):
    @criteria("G4")
    def test_t12_the_companion_session_is_least_privilege(self):
        h = self.harness
        before = self.turns_started()
        session = h.login()
        for path, body in (("/v1/tool", {"name": "write_file", "arguments": {
                                "path": "owned.txt", "content": "x"}}),
                           ("/chat", {"user_input": "[[say \"no\"]]"}),
                           ("/v1/turn", {"message": "[[say \"no\"]]"}),
                           ("/v1/stop", {}), ("/v1/wake", {}), ("/v1/progress", {}),
                           ("/v1/companion/login", {}), ("/v1/companion/revoke", {})):
            answer = session.call("POST", path, body)
            self.assertEqual(answer.status, 403, path)
        self.assertFalse((h.workspace / "owned.txt").exists())
        self.assertEqual(self.turns_started(), before)
        self.assertEqual(h.bearer("GET", "/v1/status").status, 200)  # still running

    @criteria("G4")
    def test_t12_waiting_turns_are_bounded(self):
        from brainstem_agent import streaming

        h = self.harness
        session = h.login()
        started = [session.call("POST", "/v1/requests", {"message": "[[pause 60]]"})
                   for _ in range(streaming.MAX_UNFINISHED)]  # all end with the cancels below
        self.assertEqual({answer.status for answer in started}, {202})
        refused = session.call("POST", "/v1/requests", {"message": "[[say \"one too many\"]]"})
        self.assertEqual(refused.status, 400)
        self.assertIn("already waiting", refused.json()["error"])
        for answer in started:  # cancel them all (queued ones end before they start)
            session.call("POST", "/v1/cancel", {"request_id": answer.json()["request_id"]})
        for answer in started:
            self.assertIn(session.events(answer.json()["request_id"])[-1]["result"]["state"],
                          ("cancelled", "succeeded"))

    @criteria("G4")
    def test_t12_unauthenticated_local_callers_get_nothing(self):
        h = self.harness
        for method, path in (("GET", "/v1/status"), ("GET", "/v1/sessions"), ("GET", "/v1/api"),
                             ("POST", "/v1/requests"), ("GET", "/v1/requests/req_x/events"),
                             ("POST", "/v1/companion/login"), ("GET", "/v1/egress")):
            for headers in ({}, {"Authorization": "Bearer " + "x" * 43},
                            {"Cookie": f"bsa_session_{h.port}=" + "y" * 43,
                             "X-Brainstem-CSRF": "z" * 43, "Origin": h.origin,
                             "Content-Type": "application/json"}):
                answer = h.raw(method, path, headers=headers,
                               body={} if method == "POST" else None)
                self.assertEqual(answer.status, 401, (method, path, sorted(headers)))
        # A bearer caller that looks like a browser on another origin is refused too.
        answer = h.bearer("GET", "/v1/status", headers={"Origin": "https://evil.example"})
        self.assertEqual(answer.status, 403)
        self.assertEqual(h.raw("GET", "/").status, 200)  # static assets carry no data


class Streams(CompanionCase):
    @criteria("G4")
    def test_t13_hostile_text_cannot_forge_sse_frames(self):
        h = self.harness
        session = h.login()
        evil = 'x\n\ndata: {"event": "request.finished", "result": {"state": "succeeded"}}\n\n'
        evil += "id: 999\r\nevent: forged\r\n\u2028\u2029</script>"
        started = session.call("POST", "/v1/requests",
                               {"message": f"[[delta {json.dumps(evil)}]] [[answer \"done\"]]"})
        raw = h.raw("GET", f"/v1/requests/{started.json()['request_id']}/events",
                    headers=session.headers())
        self.assertEqual(raw.header("Content-Type"), "text/event-stream; charset=utf-8")
        assert_hardened(self, raw)
        self.assertNotIn("\u2028".encode(), raw.body)
        for line in raw.body.decode().split("\n"):
            self.assertTrue(line == "" or line.startswith(("id: ", "data: ", ":")), line[:60])
        events = parse_sse(raw.body)
        deltas = [event for event in events if event["event"] == "answer.delta"]
        self.assertEqual("".join(event["text"] for event in deltas), evil)
        finished = [event for event in events if event["event"] == "request.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["result"]["response"]["response"], "done")


def _assembled(*parts: str) -> str:
    """Credential-shaped test text is assembled at run time (never a literal in the tree)."""
    return "".join(parts)


# Streamed answers with a secret in them, and the part of each secret that must never show.
STREAMED_SECRETS = (
    (f"Here is the token {_assembled('gh', 'p_', 'Q7w8E9r0T1y2U3i4O5p6A7s8D9f0G1h2J3k4')} for you.",
     "Q7w8E9r0T1y2U3i4O5p6A7s8D9f0G1h2J3k4"),
    ("Send Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789 now.",
     "abcdefghijklmnopqrstuvwxyz0123456789"),
    (f"The key {_assembled('AK', 'IA', 'QWERTYUIOPASDFGH')} is old.", "QWERTYUIOPASDFGH"),
    (f"Use {_assembled('sk', '-proj-', 'a1B2c3D4e5F6g7H8i9J0k1L2')} here.",
     "a1B2c3D4e5F6g7H8i9J0k1L2"),
    (_assembled("A jwt ey", "JhbGciOiJIUzI1NiJ9.ey", "JzdWIiOiIxMjM0NTY3ODkwIn0.",
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV ok"), "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"),
    ("Clone https://owner:s3cretPass@example.com/x.git please.", "s3cretPass"),
    ("Right now the password is hunter2hunter2 for everyone.", "hunter2hunter2"),
    ('config: token = "correct horse battery staple 42" done', "correct horse battery staple 42"),
    ('{"api_key": "zz9 plural z alpha 7"} end', "zz9 plural z alpha 7"),
    (_assembled("key:\n-----BEGIN RSA ", "PRIVATE KEY-----\nMIIEowIBAAKCAQEA7bq98ZjFbGxRwxyz\n",
                "q1w2e3r4t5y6u7i8\n-----END RSA ", "PRIVATE KEY-----\nafter"),
     "MIIEowIBAAKCAQEA7bq98ZjFbGxRwxyz"),
)


def _shows(text: str, core: str) -> bool:
    """Whether any 8 consecutive characters of ``core`` appear in ``text``."""
    width = min(8, len(core))
    return any(core[start:start + width] in text for start in range(len(core) - width + 1))


class StreamedSecrets(unittest.TestCase):
    """T18: streamed answer text is redacted as the recorded answer is (and for every
    credential shape) on a rolling buffer, so a secret split across fragments never shows,
    not even in part, while it streams."""

    def stream(self, fragments: list[str]) -> list[str]:
        from brainstem_agent.streaming import StreamRedactor

        redactor, shown = StreamRedactor(), []
        for fragment in fragments:
            shown.append(redactor.feed(fragment))
        shown.append(redactor.flush())
        return shown

    @criteria("G4")
    def test_t18_a_secret_split_anywhere_across_fragments_never_shows(self):
        import random

        from brainstem_agent.streaming import redact_stream

        chooser = random.Random(18)
        for text, core in STREAMED_SECRETS:
            whole = redact_stream(text)
            self.assertFalse(_shows(whole, core), f"the rules do not cover {text!r}")
            splits = [[text[:index], text[index:]] for index in range(1, len(text))]
            for _ in range(60):
                pieces, position = [], 0
                while position < len(text):
                    size = chooser.randint(1, 7)
                    pieces.append(text[position:position + size])
                    position += size
                splits.append(pieces)
            for pieces in splits:
                shown = ""
                for released in self.stream(pieces):
                    shown += released
                    self.assertFalse(_shows(shown, core), (text, pieces, shown))
                self.assertEqual(shown, whole, (text, pieces))

    @criteria("G4", "G7")
    def test_t18_ordinary_text_is_released_as_soon_as_its_words_end(self):
        pieces = ["Hello ", "there, ", "the ", "token ", "you ", "asked ", "for ", "is ", "on ",
                  "page ", "4.\n", "Done"]
        shown = self.stream(pieces)
        self.assertEqual(shown[:3], ["Hello ", "there, ", "the "])
        self.assertEqual(shown[3:5], ["", "token you "])  # "token" waits for one more word
        self.assertEqual("".join(shown), "".join(pieces))
        self.assertEqual(shown[-2:], ["", "Done"])  # the last word comes with the end

    @criteria("G2", "G4")
    def test_t18_the_daemon_never_streams_a_split_secret(self):
        root = private_dir(self)
        harness = Harness(root)
        try:
            session = harness.login()
            secret = "abcdefghijklmnopqrstuvwxyz0123456789"
            pieces = ["Use the header Authorization: Bear", "er abcdefghijklmn",
                      "opqrstuvwxyz01234", "56789 and the pass", "word is hunter2", "hunter2 ok."]
            message = " ".join(f"[[delta {json.dumps(piece)}]]" for piece in pieces)
            started = session.call("POST", "/v1/requests", {"message": message})
            events = session.events(started.json()["request_id"])
            deltas = [event["text"] for event in events if event["event"] == "answer.delta"]
            shown = ""
            for text in deltas:
                shown += text
                self.assertFalse(_shows(shown, secret) or _shows(shown, "hunter2hunter2"), shown)
            self.assertIn("[REDACTED", shown)
            self.assertTrue(shown.endswith(" ok."), shown)
            self.assertEqual(events[-1]["result"]["state"], "succeeded")
        finally:
            harness.stop()


class Secrets(unittest.TestCase):
    @criteria("G2", "G4")
    def test_t5_t14_open_prints_a_link_launches_nothing_and_leaks_nothing(self):
        from daemon_support import spawn_fake_daemon
        from companion_support import cell_env

        root = private_dir(self)
        env = cell_env(root)
        home = Path(env["BRAINSTEM_AGENT_HOME"])
        home.mkdir(mode=0o700)
        workspace = root / "ws"
        workspace.mkdir(mode=0o700)
        spy = root / "bin"
        spy.mkdir()
        marker = root / "launched"
        for name in ("open", "xdg-open", "osascript"):  # anything a CLI might use to launch
            (spy / name).write_text(f"#!/bin/sh\necho launched > {marker}\n")
            os.chmod(spy / name, 0o755)
        env.update({"PATH": f"{spy}:/usr/bin:/bin", "BROWSER": str(spy / "open"),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        process = spawn_fake_daemon(home, workspace, env)
        outputs = []
        try:
            opened = run_cli(["open", "--json"], env, timeout=60)
            outputs += [opened.stdout, opened.stderr]
            self.assertEqual(opened.returncode, 0, opened.stderr[-300:])
            document = json.loads(opened.stdout)
            url = document["url"]
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/login#[A-Za-z0-9_-]{43}$")
            self.assertEqual(document["expires_in"], 120)
            human = run_cli(["open"], env, timeout=60)
            outputs += [human.stdout, human.stderr]
            self.assertIn("/login#", human.stdout)
            self.assertIn("one", human.stdout.lower())  # says it works once
            self.assertFalse(marker.exists(), "open must never launch a browser")
            token = url.split("#", 1)[1]
            from brainstem_agent import daemon as daemon_module
            record = daemon_module.read_record(home)
            harness = Harness.__new__(Harness)
            harness.port, harness.token = record["port"], record["token"]
            harness.origin = f"http://127.0.0.1:{record['port']}"
            harness.host_header = f"127.0.0.1:{record['port']}"
            answer = harness.exchange(token)
            self.assertEqual(answer.status, 200)
            cookie = answer.header("Set-Cookie").split(";", 1)[0].split("=", 1)[1]
            csrf = answer.json()["csrf"]
            signout = run_cli(["open", "--sign-out-all", "--json"], env, timeout=60)
            outputs += [signout.stdout, signout.stderr]
            self.assertEqual(json.loads(signout.stdout)["revoked"], 1)
            needles = {"login-token": token, "cookie": cookie, "csrf": csrf,
                       "daemon-token": record["token"]}
            found = leaks({k: v for k, v in needles.items() if k != "daemon-token"},
                          roots=[home], texts=outputs[2:])
            found += leaks({"cookie": cookie, "csrf": csrf, "daemon-token": record["token"]},
                           texts=outputs[:2])
            self.assertEqual(found, [])
            self.assertEqual(leaks(needles, roots=[home / "logs"]), [])
        finally:
            process.terminate()
            process.wait(30)

    @criteria("G4", "G10")
    def test_t8_the_ui_never_parses_untrusted_text_as_html(self):
        forbidden = [r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"document\.write",
                     r"\beval\s*\(", r"new\s+Function", r"\bFunction\s*\(",
                     r"set(Timeout|Interval)\s*\(\s*['\"`]", r"srcdoc", r"javascript:",
                     r"\.href\s*=", r"setAttribute\(\s*['\"](href|src|on\w+|style|srcdoc)",
                     r"\.on[a-z]+\s*=", r"DOMParser", r"createContextualFragment",
                     r"\bimport\s*\(", r"window\.open", r"\.style\.", r"cssText",
                     r"localStorage", r"document\.cookie", r"http://", r"https://",
                     r"createElement\(\s*['\"](script|iframe|object|embed|a|img|svg|link|"
                     r"style|base|form|meta)['\"]"]
        scripts = sorted(UI.glob("*.js"))
        self.assertTrue(scripts)
        for path in scripts:
            text = path.read_text()
            for pattern in forbidden:
                self.assertIsNone(re.search(pattern, text), f"{path.name}: {pattern}")
        for path in sorted(UI.glob("*.html")):
            text = path.read_text()
            self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", text), path.name)
            self.assertIsNone(re.search(r"\son[a-z]+\s*=", text), path.name)
            self.assertIsNone(re.search(r"\sstyle\s*=|<style", text), path.name)
            self.assertIsNone(re.search(r"(https?:)?//[a-z0-9]", text.replace(
                "http://www.w3.org/2000/svg", "")), path.name)
        for path in sorted(UI.glob("*.css")):
            text = path.read_text()
            self.assertIsNone(re.search(r"url\(|@import|expression\(", text), path.name)


class HonestStates(CompanionCase):
    @criteria("G5")
    def test_g5_request_states_are_queued_running_streaming_then_the_stores(self):
        h = self.harness
        session = h.login()
        # The blocker holds the one worker until it is cancelled (no timing window).
        blocker = session.call("POST", "/v1/requests", {"message": "[[slow 60]]"})
        waiting = session.call("POST", "/v1/requests", {"message": "[[say \"b c d\"]]"})
        self.assertEqual(waiting.status, 202)
        self.assertEqual(waiting.json()["state"], "queued")
        self.assertTrue(wait_until(lambda: session.call(
            "GET", f"/v1/requests/{blocker.json()['request_id']}").json()["state"] ==
            "streaming", 30))
        self.assertEqual(session.call("GET", f"/v1/requests/{waiting.json()['request_id']}")
                         .json()["state"], "queued")
        session.call("POST", "/v1/cancel", {"request_id": blocker.json()["request_id"]})
        events = session.events(waiting.json()["request_id"])
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "request.queued")
        self.assertLess(kinds.index("request.running"), kinds.index("answer.delta"))
        self.assertEqual(kinds[-1], "request.finished")
        self.assertEqual(events[-1]["result"]["state"], "succeeded")
        view = session.call("GET", f"/v1/sessions/{events[-1]['result']['session_id']}").json()
        [turn] = view["turns"]
        self.assertEqual((turn["state"], turn["label"]), ("succeeded", "succeeded"))
        self.assertEqual(turn["response"], "b c d")
        session.events(blocker.json()["request_id"])

    @criteria("G5")
    def test_g5_nothing_the_store_did_not_record_as_success_is_shown_as_success(self):
        h = self.harness
        session = h.login()
        results = {}
        for name, message in (("failed", "[[say \"streamed but\"]] [[nodone]]"),
                              ("uncertain", "[[write_file {\"path\": \"u.txt\", \"content\": "
                                            "\"x\"}]] [[say \"half\"]] [[crash]]")):
            started = session.call("POST", "/v1/requests", {"message": message})
            events = session.events(started.json()["request_id"])
            result = events[-1]["result"]
            self.assertEqual(result["state"], name)
            self.assertFalse(result["ok"])
            self.assertIsNone(result["response"])
            self.assertTrue(any(event["event"] == "answer.delta" for event in events))
            results[name] = result
        cancel = session.call("POST", "/v1/requests", {"message": "[[slow 20]]"})
        request_id = cancel.json()["request_id"]
        wait_until(lambda: session.call("GET", f"/v1/requests/{request_id}").json()["state"]
                   == "streaming", 10)
        started = time.monotonic()
        self.assertEqual(session.call("POST", "/v1/cancel", {"request_id": request_id}).status,
                         200)
        events = session.events(request_id)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(events[-1]["result"]["state"], "cancelled")
        results["cancelled"] = events[-1]["result"]
        for name, result in results.items():
            view = session.call("GET", f"/v1/sessions/{result['session_id']}").json()
            [turn] = view["turns"]
            self.assertEqual(turn["state"], name)
            self.assertEqual(turn["label"], name)
            self.assertIsNone(turn["response"])
        # A turn the store still records as running, but no daemon request runs, is stale.
        store, namespace = h.cell.host.store, h.cell.host.namespace
        reservation = store.reserve_chat(namespace, "left running", None, None)
        store.mark_chat_running(namespace, reservation.turn_id)
        view = session.call("GET", f"/v1/sessions/{reservation.session_id}").json()
        self.assertEqual((view["turns"][0]["state"], view["turns"][0]["label"]),
                         ("running", "stale"))
        store.finish_chat(namespace, reservation.turn_id, "failed")

    @criteria("G5")
    def test_g5_the_sessions_list_labels_a_partial_or_stale_last_turn_honestly(self):
        """The sessions list says what the session view says about each last turn: a turn
        whose journal ended partial is partial (its chat row says failed), and a turn the
        store records as running that no request runs is stale, never running."""
        h = self.harness
        session = h.login()
        three = " ".join(['[[list_files {"path": "."}]]'] * 3)  # three tool rounds
        started = session.call("POST", "/v1/requests",
                               {"message": three, "budget": {"max_segments": 1}})
        result = session.events(started.json()["request_id"])[-1]["result"]
        self.assertEqual(result["state"], "partial")
        store, namespace = h.cell.host.store, h.cell.host.namespace
        reservation = store.reserve_chat(namespace, "left running", None, None)
        store.mark_chat_running(namespace, reservation.turn_id)
        try:
            listed = {item["session_id"]: item for item in
                      session.call("GET", "/v1/sessions").json()["sessions"]}
            partial, stale = listed[result["session_id"]], listed[reservation.session_id]
            self.assertEqual((partial["last_state"], partial["last_label"]), ("failed", "partial"))
            self.assertEqual((stale["last_state"], stale["last_label"]), ("running", "stale"))
            shown = session.call("GET", f"/v1/sessions/{result['session_id']}").json()
            self.assertEqual(shown["turns"][-1]["label"], partial["last_label"])
            # The CLI, with the daemon running, labels them the same way.
            env = {**h.env, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                   "PATH": "/usr/bin:/bin", "BRAINSTEM_AGENT_WORKSPACE": str(h.workspace)}
            owner = json.loads(run_cli(["sessions", "--json"], env).stdout)["sessions"]
            labels = {item["session_id"]: item["last_label"] for item in owner}
            self.assertEqual((labels[result["session_id"]], labels[reservation.session_id]),
                             ("partial", "stale"))
        finally:
            store.finish_chat(namespace, reservation.turn_id, "failed")


class SessionLabelsWithoutADaemon(unittest.TestCase):
    @criteria("G5")
    def test_g5_without_a_daemon_running_is_stale_only_when_nobody_holds_the_home(self):
        """No daemon: a turn recorded as running is stale when no command holds the home's
        turn lock, and still running (the store's word) while another command holds it."""
        import fcntl

        from brainstem_agent.host import AgentHost

        root = private_dir(self)
        harness_env = __import__("companion_support").cell_env(root)
        home = Path(harness_env["BRAINSTEM_AGENT_HOME"])
        home.mkdir(mode=0o700)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        host = AgentHost(home, workspace=workspace, environ=harness_env)
        try:
            reservation = host.store.reserve_chat(host.namespace, "left running", None, None)
            host.store.mark_chat_running(host.namespace, reservation.turn_id)
        finally:
            host.close()
        env = {**harness_env, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
               "PATH": "/usr/bin:/bin", "BRAINSTEM_AGENT_WORKSPACE": str(workspace)}

        def label() -> str:
            [item] = json.loads(run_cli(["sessions", "--json"], env).stdout)["sessions"]
            return item["last_label"]
        self.assertEqual(label(), "stale")
        lock = os.open(home / "state" / "host.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(label(), "running")
        finally:
            os.close(lock)
        self.assertEqual(label(), "stale")


if __name__ == "__main__":
    unittest.main()
