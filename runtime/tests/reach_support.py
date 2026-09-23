"""Support for the reaching-cell specs (E1-E12).

``WebFixture`` is a loopback HTTP server that the web organ reaches through injected name
resolution: every test host name resolves to the address the test chooses (public-looking by
default) and the pinned connect is redirected here, so the organ's real checks, redirects,
budgets and parsing run without any outbound request. ``notes_server`` is the configuration
of the stdlib MCP fixture (``mcp_fixture.py``) inside its default Seatbelt profile.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from acceptance_support import kill_quietly, private_dir, write_token_file
from brainstem_agent.broker import LoopbackHTTPServer
from brainstem_agent.host import AgentHost
from brainstem_agent.organs.scripts import standard_library
from brainstem_agent.paths import holds
from longturn_support import factory_for

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "mcp_fixture.py"
PAGE = ("<html><head><title>Brainstem Agent</title><style>body{color:red}</style></head><body>"
        "<nav>Home</nav><h1>Brainstem Agent</h1><p>Brainstem Agent is a cell that captures "
        "Grail.</p><p>It reads the web and plugs in MCP tools.</p><script>var x = 1;</script>"
        "</body></html>")


class WebFixture:
    """Routes: path -> (status, headers, body) or a callable(handler) that answers itself."""

    def __init__(self, test: unittest.TestCase, routes: dict | None = None) -> None:
        self.routes = {"/page": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE),
                       **(routes or {})}
        self.requests: list[dict] = []
        self.connected: list[str] = []
        self.resolved: list[str] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                fixture.requests.append({"path": self.path, "headers": dict(self.headers)})
                route = fixture.routes.get(self.path.split("?", 1)[0])
                if callable(route):
                    return route(self)
                status, headers, body = route or (404, {"Content-Type": "text/plain"}, "missing")
                data = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *_args: None  # a client that stopped reading
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    def attach(self, organ, answers: dict | None = None) -> None:
        """Resolve names through ``answers`` (default: a public address) and connect here."""
        answers = answers or {}

        def resolve(host, port):
            self.resolved.append(host)
            value = answers.get(host, ["93.184.216.34"])
            return value(host) if callable(value) else list(value)

        def connect(address, timeout):
            self.connected.append(address[0])
            return socket.create_connection(("127.0.0.1", self.port), timeout)

        organ.resolve, organ.connect = resolve, connect


def interpreter_readable() -> list[str]:
    """This interpreter's standard library when it is installed where the MCP sandbox denies
    reads (a pyenv or uv Python under the home), listed the way an owner would list it."""
    denied = ("/Users", "/Volumes", "/private/var/folders", "/private/tmp", Path.home())
    return [str(path) for path in standard_library(sys.executable)
            if any(holds(area, path) for area in denied)]


def notes_server(store: Path, **extra) -> dict:
    """The stdio MCP fixture as an owner would configure it (its default sandbox, plus reading
    its own script and interpreter and writing its store)."""
    config = {"command": sys.executable,
              "args": ["-I", str(FIXTURE), "--store", str(store / "notes.json")],
              "sandbox": {"readable": [str(HERE), *interpreter_readable()],
                          "writable": [str(store)]}}
    for key, value in extra.items():
        if key == "sandbox":
            config["sandbox"] = {**config["sandbox"], **value}
        elif key == "fixture_args":
            config["args"] = config["args"] + list(value)
        else:
            config[key] = value
    return config


def http_server(test: unittest.TestCase, *arguments: str, port: int = 0):
    """Start the HTTP MCP fixture; returns (process, port). Stopped at cleanup."""
    process = subprocess.Popen([sys.executable, "-I", str(FIXTURE), "--http", str(port),
                                *arguments], stdout=subprocess.PIPE, text=True,
                               start_new_session=True)
    test.addCleanup(lambda: (kill_quietly(process.pid), process.wait(5), process.stdout.close()))
    line = process.stdout.readline()
    return process, json.loads(line)["port"]


class ReachCase(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.store = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        self.workers: list = []

    def reach(self, config: dict) -> None:
        (self.home / "reach.json").write_text(json.dumps(config))

    def host(self, policy=None, **environ) -> AgentHost:
        policy = policy or (lambda request, results, tools=(), final=False: "ok")
        host = AgentHost(self.home, workspace=self.workspace,
                         environ={**self.environ, **environ},
                         worker_factory=factory_for(policy, self.workers))
        self.addCleanup(host.close)
        return host

    @staticmethod
    def wait(predicate, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True
