"""Support for the companion-surface specs (G1-G12): a real daemon with scripted
workers that stream answers, raw HTTP helpers that control every header, the login flow,
an SSE reader and hostile seed data.

``ScriptedStreamWorker`` replaces only the Grail process (like ``test_cell_host``'s
``FakeWorker``, which it is): it binds its grant with the daemon's real broker and invokes
real organs. Directives in the user message script a turn:

``[[say "text"]]``      stream the text word by word as Grail ``delta`` frames
``[[delta "text"]]``    one raw delta frame
``[[pause 2.5]]``       wait (ends early when the worker is stopped: cancellation)
``[[slow 20]]``         a long-running answer: a delta every 0.2 s for up to 20 s
``[[answer "text"]]``   the final (``done``) answer, instead of what was streamed
``[[nodone]]``          end the stream without ``done`` (Grail failed)
``[[crash]]``           break the stream (the worker died mid-turn)
``[[<tool> {json}]]``   invoke that cell tool (its result line joins the answer)

Run as a script, ``python companion_support.py serve <dir> [--seed hostile]`` serves a
daemon (scripted workers) in the foreground for the browser specs and prints one JSON line
(``port``, ``home``, ``workspace``, ``pid``) once it is ready (and seeded).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance_support import (  # noqa: E402
    private_dir, remove_tree, wait_until, write_token_file,
)
from daemon_support import directives  # noqa: E402
from test_cell_host import FakeWorker, done, sse  # noqa: E402

# One line, every classic HTML/script vector at once (and a template-injection probe).
HOSTILE = ('<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'
           '<svg onload="window.__pwned=3"></svg><iframe srcdoc="<script>parent.__pwned=4'
           '</script>"></iframe><a href="javascript:window.__pwned=5">x</a>{{7*7}}')
SHORT_HOSTILE = 'sched <img src=x onerror="window.__pwned=9"><script>window.__pwned=2</script>'
HOSTILE_FILE = 'notes/<img src=x onerror="window.__pwned=6">.txt'
ESCAPES = "\x1b]52;c;cHduZWQ=\x07\x1b[2J\x1b]0;owned\x07\x1b]8;;http://evil.test\x07link\x1b]8;;\x07"


def _pause(worker, seconds: float) -> bool:
    """Sleep up to ``seconds``; False when the worker was stopped meanwhile (cancelled)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if worker.stopped.wait(0.02):
            return False
    return True


def script(worker, request, grant):
    bound = worker.bind(grant)
    said, lines, answer = [], [], None
    for name, argument in directives(request["user_input"]):
        if name == "say":
            for piece in re.findall(r"\S+\s*", str(argument)):
                said.append(piece)
                yield sse({"type": "delta", "text": piece})
                if not _pause(worker, 0.01):
                    return
        elif name == "delta":
            said.append(str(argument))
            yield sse({"type": "delta", "text": str(argument)})
        elif name == "pause":
            if not _pause(worker, float(argument)):
                return
        elif name == "slow":
            deadline = time.monotonic() + float(argument)
            count = 0
            while time.monotonic() < deadline:
                count += 1
                said.append(f"tick{count} ")
                yield sse({"type": "delta", "text": f"tick{count} "})
                if not _pause(worker, 0.2):
                    return
        elif name == "answer":
            answer = str(argument)
        elif name == "nodone":
            return
        elif name == "crash":
            raise OSError("the fake worker's stream broke")
        else:
            status, body = worker.invoke(grant, bound, name, argument or {})
            lines.append(f"{name}:{status}:{body.get('ok')}:"
                         f"{body.get('content', body.get('error'))}")
            yield sse({"type": "agent", "logs": f"[{name}] done"})
    text = answer if answer is not None else ("".join(said) or "\n".join(lines) or "ok")
    yield done(request, text)


class ScriptedStreamWorker(FakeWorker):
    def __init__(self, **options):
        super().__init__(script, **options)

    def start(self):
        return {**super().start(), "start_seconds": 0.0, "pid": None}


def factory(**options):
    return ScriptedStreamWorker(**options)


def cell_env(root: Path) -> dict:
    """An environment that can never see the owner's installed credential."""
    empty = root / "no-brainstem"
    empty.mkdir(exist_ok=True, mode=0o700)
    credential = root / "credential"  # never beside (or above) the workspace
    credential.mkdir(exist_ok=True, mode=0o700)
    token = write_token_file(credential)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("BRAINSTEM_")}
    env.update({"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token), "BRAINSTEM_HOME": str(empty),
                "BRAINSTEM_AGENT_HOME": str(root / "home")})
    return env


class Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status, self.header_list, self.body = status, headers, body

    def header(self, name: str) -> str | None:
        for key, value in self.header_list:
            if key.lower() == name.lower():
                return value
        return None

    def headers(self, name: str) -> list[str]:
        return [value for key, value in self.header_list if key.lower() == name.lower()]

    def json(self):
        return json.loads(self.body or b"{}")


class Harness:
    """A real daemon (scripted workers) in this process, plus clients for its surface."""

    def __init__(self, root: Path, *, workspace: Path | None = None) -> None:
        from brainstem_agent import daemon

        self.root = root
        self.env = cell_env(root)
        self.home = root / "home"
        self.home.mkdir(exist_ok=True, mode=0o700)
        self.workspace = workspace or (root / "workspace")
        self.workspace.mkdir(exist_ok=True, mode=0o700)
        self.cell = daemon.Daemon(self.home, workspace=self.workspace, environ=self.env,
                                  worker_factory=factory)
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.cell.serve,
                                       kwargs={"ready": lambda _status: self.ready.set()},
                                       daemon=True, name="companion-test-daemon")
        self.thread.start()
        if not self.ready.wait(30):
            raise AssertionError("the test daemon never became ready")
        record = daemon.read_record(self.home)
        self.port, self.token = record["port"], record["token"]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.host_header = f"127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.cell.stop()
        self.thread.join(60)

    # -- raw HTTP -------------------------------------------------------------------
    def raw(self, method: str, path: str, *, headers=None, body=None, host="default",
            timeout: float = 30.0) -> Response:
        """One request with full control over the headers (``host=None`` sends no Host)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                connection.putheader("Host", self.host_header)
            elif isinstance(host, (list, tuple)):
                for value in host:
                    connection.putheader("Host", value)
            elif host is not None:
                connection.putheader("Host", host)
            data = None
            if body is not None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
            for key, value in (headers or {}).items():
                if value is not None:  # None: leave this header out
                    connection.putheader(key, value)
            if data is not None:
                connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
            answer = connection.getresponse()
            return Response(answer.status, answer.getheaders(), answer.read())
        finally:
            connection.close()

    def bearer(self, method: str, path: str, body=None, **options) -> Response:
        headers = {"Authorization": "Bearer " + self.token, **options.pop("headers", {})}
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        return self.raw(method, path, headers=headers, body=body, **options)

    # -- the companion's login and requests ------------------------------------------
    def mint(self) -> str:
        """A one-time login URL, exactly as ``brainstem-agent open`` asks for it."""
        answer = self.bearer("POST", "/v1/companion/login", {})
        assert answer.status == 200, (answer.status, answer.body[:300])
        return answer.json()["url"]

    def exchange(self, token: str, *, headers=None) -> Response:
        base = {"Origin": self.origin, "Content-Type": "application/json",
                "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
        base.update(headers or {})
        return self.raw("POST", "/v1/companion/session", headers=base, body={"token": token})

    def login(self) -> "Session":
        token = self.mint().split("#", 1)[1]
        answer = self.exchange(token)
        assert answer.status == 200, (answer.status, answer.body[:300])
        cookie = answer.header("Set-Cookie").split(";", 1)[0]
        return Session(self, cookie, answer.json()["csrf"])


class Session:
    """A signed-in companion tab: its cookie and its CSRF secret (the page's view)."""

    def __init__(self, harness: Harness, cookie: str, csrf: str) -> None:
        self.harness, self.cookie, self.csrf = harness, cookie, csrf

    def headers(self, method: str = "GET", **extra) -> dict:
        headers = {"Cookie": self.cookie, "X-Brainstem-CSRF": self.csrf,
                   "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
                   "Sec-Fetch-Dest": "empty"}
        if method != "GET":
            headers.update({"Origin": self.harness.origin, "Content-Type": "application/json"})
        headers.update(extra)
        return {key: value for key, value in headers.items() if value is not None}

    def call(self, method: str, path: str, body=None, **extra) -> Response:
        if method != "GET" and body is None:
            body = {}
        return self.harness.raw(method, path, headers=self.headers(method, **extra), body=body)

    def events(self, request_id: str, *, after: int = 0, timeout: float = 60.0) -> list[dict]:
        """Read one request's SSE stream to its end; returns the decoded events in order."""
        answer = self.harness.raw("GET", f"/v1/requests/{request_id}/events?after={after}",
                                  headers=self.headers(), timeout=timeout)
        assert answer.status == 200, (answer.status, answer.body[:300])
        return parse_sse(answer.body)


def parse_sse(raw: bytes) -> list[dict]:
    events = []
    for frame in raw.decode("utf-8").split("\n\n"):
        data = [line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


# -- hostile seed data (every field the companion renders) ---------------------------------
def hostile_turns() -> list[str]:
    """Turns whose inputs, answers, tool arguments and results, memory, skills and schedule
    names all carry the hostile payload (they run through the real engine paths)."""
    say = json.dumps(HOSTILE)
    return [
        f"{HOSTILE} [[say {say}]]",
        f"[[write_file {json.dumps({'path': HOSTILE_FILE, 'content': HOSTILE})}]] "
        f"[[read_file {json.dumps({'path': HOSTILE_FILE})}]] [[answer {say}]]",
        f"remember this [[remember {json.dumps({'text': 'fact ' + HOSTILE, 'scope': 'workspace'})}]] "
        f"[[remember {json.dumps({'text': 'profile ' + HOSTILE, 'scope': 'profile'})}]]",
        "save this as a skill called hostile-skill [[skill_save " + json.dumps({
            "name": "hostile-skill", "description": HOSTILE, "when_to_use": HOSTILE,
            "steps": [HOSTILE, "second " + HOSTILE]}) + "]]",
        "[[schedule_create " + json.dumps({  # names are at most 80 characters
            "name": SHORT_HOSTILE, "prompt": f"[[say {say}]]", "in_seconds": 1}) + "]]",
    ]


def seed_hostile(harness: Harness) -> None:
    for message in hostile_turns():
        answer = harness.bearer("POST", "/v1/turn", {"message": message, "timeout": 60},
                                timeout=90)
        assert answer.status == 200 and answer.json()["ok"], answer.body[:400]
        assert ":False:" not in answer.json()["response"]["response"], answer.body[:600]
    harness.cell.host.egress.write({"turn": "turn_hostile", "tool": "web_fetch",
                                    "method": "GET", "host": HOSTILE, "ip": "93.184.216.34",
                                    "port": 443, "path": "/" + HOSTILE, "status": 200,
                                    "bytes": 10, "seconds": 0.1})
    store = harness.cell.host.store
    namespace = harness.cell.host.namespace
    # The scheduled run fires about a second later; wait for its hostile answer in the inbox.
    if not wait_until(lambda: any(item["state"] == "succeeded" for item in
                                  store.list_occurrences(namespace)), 30):
        raise AssertionError("the hostile schedule never ran")


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "serve":
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    harness = Harness(root)
    if "--seed" in sys.argv and sys.argv[sys.argv.index("--seed") + 1] == "hostile":
        seed_hostile(harness)
    # A worker that answers slowly, for streaming and cancellation in the browser specs.
    print(json.dumps({"ready": True, "port": harness.port, "home": str(harness.home),
                      "workspace": str(harness.workspace), "pid": os.getpid(),
                      "env": {"BRAINSTEM_AGENT_HOME": str(harness.home),
                              "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE":
                                  harness.env["BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"],
                              "BRAINSTEM_HOME": harness.env["BRAINSTEM_HOME"]}}), flush=True)
    stopping = threading.Event()
    import signal

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stopping.set())
    while not stopping.wait(0.2):
        if not harness.thread.is_alive():
            break
    harness.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ESCAPES", "HOSTILE", "HOSTILE_FILE", "SHORT_HOSTILE", "Harness", "Session", "cell_env", "factory",
           "hostile_turns", "parse_sse", "private_dir", "remove_tree", "script",
           "seed_hostile"]
