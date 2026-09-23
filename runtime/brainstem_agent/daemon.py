"""Brainstem Agent daemon: one owner-only process that keeps the cell alive.

It holds the home's exclusive lock for its whole life (so a second ``serve``, or
an in-process turn, refuses; interrupted work is recovered once at start), keeps
one warm, integrity-verified Grail worker, and runs the single schedule loop
(``schedules.Scheduler``). Chat and scheduled turns run one at a time on that
worker.

Control surface: HTTP on 127.0.0.1 with a random bearer token that lives only in
``run/daemon.json`` (0600 inside the 0700 home). Requests without the token, or
with a Host other than this loopback address, are refused. Grail workers and
shell commands cannot reach it: their sandboxes deny loopback (except a worker's
own broker) and reading the home. Routes: RAPP/1 ``POST /chat`` (exactly
``response``, ``agent_logs``, ``session_id``) and the private ``GET /v1/status``,
``POST /v1/turn`` (the full turn result, for the CLI), ``/v1/tool``, ``/v1/cancel``,
``/v1/wake`` and ``/v1/stop``. The token is never printed or logged.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import hmac
import json
import os
import plistlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Mapping

from . import lifeline, schedules
from .broker import LoopbackHTTPServer
from .host import AgentHost, HostError, TurnResult
from .longturn import TurnBudget
from .state import StateError

__all__ = ["Client", "Daemon", "DaemonError", "DaemonUnavailable", "connect", "read_record",
           "service", "spawn", "stop"]

RECORD = "daemon.json"
LAST_STOP = "last-stop.json"
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_MAX_BODY = 1 << 20
_TURN_TIMEOUT = 300.0
_STARTUP_RETRY_SECONDS = 60.0
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")


def _redacted(text: str) -> str:
    return _TOKEN_SHAPES.sub(r"\1[REDACTED]", text)[:600]


def _retrying(action: Callable[[], Any], note: Callable[[str], None], where: str, *,
              seconds: float = _STARTUP_RETRY_SECONDS) -> Any:
    """Run a startup step, retrying a failing store (locked, busy) with backoff for a while."""
    deadline, delay = time.monotonic() + seconds, 0.1
    while True:
        try:
            return action()
        except StateError as error:
            if time.monotonic() >= deadline:
                raise
            note(f"startup {where}: {error} (retrying)")
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


class DaemonError(RuntimeError):
    """The daemon refused, failed or could not be started."""


class DaemonUnavailable(DaemonError):
    """No daemon accepted the connection (nothing was sent)."""


def _steady_clock() -> Callable[[], float]:
    """Wall time that never regresses (grant clocks refuse a clock that goes backwards)."""
    base, anchor = time.time(), time.monotonic()
    return lambda: base + (time.monotonic() - anchor)


def record_path(home: Path | str) -> Path:
    return Path(os.path.realpath(home)) / "run" / RECORD


def read_record(home: Path | str) -> dict | None:
    """The live daemon's record: a private file of ours whose pid still runs."""
    try:
        descriptor = os.open(record_path(home), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            return None
        try:
            record = json.loads(handle.read(65536))
            os.kill(int(record["pid"]), 0)
        except (ValueError, KeyError, TypeError, OSError):
            return None
    return record if isinstance(record.get("port"), int) and record.get("token") else None


class Client:
    def __init__(self, record: Mapping[str, Any]) -> None:
        self.pid, self.port = int(record["pid"]), int(record["port"])
        self._token = str(record["token"])

    def __repr__(self) -> str:
        return f"Client(pid={self.pid}, port={self.port})"

    def call(self, method: str, path: str, body: Mapping | None = None, *,
             timeout: float = 30.0) -> dict:
        data = None if body is None else json.dumps(dict(body)).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                return json.loads(response.read(1 << 24) or b"{}")
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(65536) or b"{}").get("error")
            except ValueError:
                detail = None
            raise DaemonError(f"The daemon refused the request ({error.code}): {detail}") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (ConnectionRefusedError, FileNotFoundError)):
                raise DaemonUnavailable("No daemon is listening.") from None
            raise DaemonError(f"The daemon connection failed: {error.reason}") from None
        except (OSError, ValueError) as error:
            raise DaemonError(f"The daemon connection failed: {type(error).__name__}") from None


def connect(home: Path | str) -> Client | None:
    record = read_record(home)
    return None if record is None else Client(record)


class _Server(LoopbackHTTPServer):
    daemon_threads = True


def _write_private(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically write an owner-only (0600) JSON record."""
    temporary = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(dict(document), handle)
    os.replace(temporary, path)


class Daemon:
    """The always-on cell for one home: control server, warm worker and schedule loop."""

    def __init__(self, home: Path | str, *, workspace: Path | None = None, cache: Path | None = None,
                 environ: Mapping[str, str] | None = None,
                 worker_factory: Callable[..., Any] | None = None) -> None:
        self.home = Path(os.path.realpath(home))
        self.environ = dict(os.environ if environ is None else environ)
        self.errors: collections.deque = collections.deque(maxlen=20)
        # A store that is briefly locked or failing delays the start instead of ending it.
        self.host = _retrying(lambda: AgentHost(
            self.home, workspace=workspace, cache=cache, environ=self.environ,
            worker_factory=worker_factory, clock=_steady_clock()), self._error, "store")
        self.host.schedules_changed = self._changed
        # Background processes outlive the turn that started them while the daemon runs.
        self.host.process_organ.long_lived = True
        self.scheduler = schedules.Scheduler(self.host.store, self._run_occurrence,
                                             idle=self._idle, on_error=self._error)
        self.turns = threading.Lock()
        self.stopping = threading.Event()
        self._requests: dict[str, threading.Event] = {}
        # Progress events of recent requests (``/v1/progress``), bounded and short-lived.
        self._progress: collections.OrderedDict = collections.OrderedDict()
        self._progress_lock = threading.Lock()
        self._token = secrets.token_urlsafe(32)
        self._warm_after = 0.0
        self._server: _Server | None = None
        self._loop: threading.Thread | None = None
        self.started_at = time.time()
        self.stop_evidence: dict | None = None

    # -- lifecycle -------------------------------------------------------------------
    def serve(self, *, ready: Callable[[dict], None] | None = None) -> dict:
        """Run until ``stop()`` (or SIGTERM/SIGINT when on the main thread); returns stop evidence."""
        lock = contextlib.ExitStack()
        try:
            try:
                # Recovery (inside ``exclusive``) retries a failing store; a held lock refuses.
                _retrying(lambda: lock.enter_context(self.host.exclusive(wait=0)), self._error,
                          "recovery")
            except HostError:
                record = read_record(self.home)
                if record is None:
                    raise DaemonError("This home is busy (another Brainstem Agent command holds "
                                      "it); try again when it finishes.") from None
                raise DaemonError(f"Brainstem Agent is already running for this home (pid "
                                  f"{record['pid']}); a second daemon refuses.") from None
            try:
                self._server = _Server(("127.0.0.1", 0), self._handler())
                threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                 daemon=True, name="brainstem-agent-control").start()
                self._write_record()
                self._loop = threading.Thread(target=self.scheduler.run, daemon=True,
                                              name="brainstem-agent-scheduler")
                self._loop.start()
                if ready is not None:
                    ready(self.status())
                while not self.stopping.wait(0.25):
                    pass
            finally:
                evidence = self._shutdown()
            return evidence
        finally:
            lock.close()
            if not self.host._closed:
                self.host.close()

    def stop(self) -> None:
        self.stopping.set()

    def _shutdown(self) -> dict:
        started, since = time.monotonic(), time.time()
        before = self.host.worker_status()
        self.scheduler.stop()
        self.host.cancel()
        if self._loop is not None:
            self._loop.join(30)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        record_path(self.home).unlink(missing_ok=True)
        stopped = self.host.close() or {}
        # Every worker this stop ended (the one a cancelled turn used included), measured.
        seen: dict[tuple, dict] = {}
        for item in [before, *[entry for entry in self.host.discarded if entry["at"] >= since]]:
            if item is not None:
                key = (item["worker_id"], item["generation"])
                seen[key] = {**seen.get(key, {}), **item}
        workers = []
        for item in seen.values():
            pgid = item.get("pgid")
            if pgid:
                state = lifeline.group_state(pgid)
            else:  # no process of its own to measure (for example a fake Grail)
                state = lifeline.GONE if item.get("group_gone", True) is not False else "unknown"
            workers.append({key: item.get(key) for key in ("worker_id", "generation", "pid",
                                                           "pgid")} | {"group_state": state})
        integrity = stopped.get("integrity_after") or (self.host.last_stop or {}).get(
            "integrity_after")
        self.stop_evidence = {"seconds": round(time.monotonic() - started, 3),
                              "worker": workers[0] if workers else None, "workers": workers,
                              "group_gone": all(item["group_state"] == lifeline.GONE
                                                for item in workers),
                              "integrity_after": integrity}
        try:  # for ``stop``, which reads it once this daemon has released the home
            _write_private(self.home / "run" / LAST_STOP,
                           {"pid": os.getpid(), "stopped_at": time.time(), **self.stop_evidence})
        except OSError:
            pass
        return self.stop_evidence

    def _write_record(self) -> None:
        _write_private(self.home / "run" / RECORD,
                       {"pid": os.getpid(), "port": self._server.server_address[1],
                        "token": self._token, "started_at": self.started_at})

    # -- scheduling and warmth ---------------------------------------------------------
    def _changed(self) -> None:
        self.scheduler.wake()

    def _error(self, text: str) -> None:
        self.errors.appendleft({"at": time.time(), "source": "daemon", "error": _redacted(text)})

    def _acquire(self, cancel: threading.Event | None = None,
                 timeout: float | None = None) -> bool:
        """Wait for the one worker (turns run one at a time); give up on cancel, stop or
        after ``timeout`` seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not (self.stopping.is_set() or (cancel is not None and cancel.is_set())):
            if self.turns.acquire(timeout=0.1):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                break
        return False

    def _idle(self) -> None:
        self.host.mcp_organ.revive()  # crashed MCP servers restart here too (with backoff)
        if (self.stopping.is_set() or time.monotonic() < self._warm_after
                or not self.turns.acquire(blocking=False)):
            return
        try:
            self.host.warm()
            self._warm_after = 0.0
        except Exception as error:  # no credential, no network, bad cache: retry later
            self._warm_after = time.monotonic() + 60
            self._error(f"warm worker: {error}")
        finally:
            self.turns.release()

    def _run_occurrence(self, occurrence: dict) -> dict:
        if not self._acquire():
            return {"state": "cancelled",
                    "result": {"error": "The daemon stopped before the run started."}}
        try:
            return schedules.run_occurrence(self.host, occurrence)
        finally:
            self.turns.release()

    # -- requests ----------------------------------------------------------------------
    @contextlib.contextmanager
    def _cancellable(self, body: Mapping[str, Any]):
        """A cancel event that ``/v1/cancel`` can set by the request's ``request_id``."""
        event, key = threading.Event(), body.get("request_id")
        key = key if isinstance(key, str) and 0 < len(key) <= 64 else None
        if key is not None:
            self._requests[key] = event
        try:
            yield event
        finally:
            if key is not None:
                self._requests.pop(key, None)

    def _progress_sink(self, body: Mapping[str, Any]):
        key = body.get("request_id")
        if not (isinstance(key, str) and 0 < len(key) <= 64):
            return None
        with self._progress_lock:
            self._progress[key] = {"events": collections.deque(maxlen=2000), "seq": 0,
                                   "at": time.monotonic()}
            while len(self._progress) > 32:
                self._progress.popitem(last=False)

        def sink(event: dict) -> None:
            with self._progress_lock:
                entry = self._progress.get(key)
                if entry is not None:
                    entry["seq"] += 1
                    entry["events"].append((entry["seq"], event))
        return sink

    def progress(self, body: Mapping[str, Any]) -> dict:
        """The progress events of a request after sequence number ``after``."""
        after = int(body.get("after") or 0)
        with self._progress_lock:
            entry = self._progress.get(body.get("request_id"))
            events = [] if entry is None else [[seq, event] for seq, event in entry["events"]
                                               if seq > after]
        return {"events": events}

    @staticmethod
    def _budget(body: Mapping[str, Any]) -> TurnBudget | None:
        given = body.get("budget")
        if not isinstance(given, dict):
            return None
        known = set(TurnBudget.__dataclass_fields__)
        return TurnBudget(**{key: value for key, value in given.items() if key in known})

    def turn(self, body: Mapping[str, Any]) -> dict:
        message = body.get("message")
        with self._cancellable(body) as cancel:
            if not isinstance(message, str):
                raise ValueError("message must be a string")
            budget = self._budget(body)
            sink = self._progress_sink(body)
            timeout = float(body.get("timeout") or _TURN_TIMEOUT)
            if not self._acquire(cancel, timeout):
                waited = cancel.is_set() or self.stopping.is_set()
                return TurnResult(False, "cancelled" if waited else "failed", False, None,
                                  body.get("session_id"), None,
                                  "The turn was cancelled before it started." if waited else
                                  f"The worker stayed busy with another turn for {timeout:g}s; "
                                  "nothing was run.", {"worker": None, "grail_calls": 0}).to_json()
            try:
                result = self.host.chat(
                    message, session_id=body.get("session_id"),
                    idempotency_key=body.get("idempotency_key"),
                    capabilities=body.get("capabilities"), timeout=timeout, cancel_event=cancel,
                    workspace=body.get("workspace"), budget=budget, progress=sink)
            finally:
                self.turns.release()
        document = result.to_json()
        document["evidence"]["daemon"] = {"pid": os.getpid()}
        return document

    def tool(self, body: Mapping[str, Any]) -> dict:
        with self._cancellable(body) as cancel:
            return self.host.invoke_tool(str(body.get("name")), dict(body.get("arguments") or {}),
                                         capabilities=body.get("capabilities"),
                                         cancel_event=cancel, workspace=body.get("workspace"))

    def status(self) -> dict:
        store = self.host.store
        store_error = None
        try:
            states = collections.Counter(item["state"] for item in store.list_schedules(None))
            wake = store.next_wake()
            failures = [{"at": item["finished_at"], "source": item["occurrence_id"],
                         "error": (item["result"] or {}).get("error") or item["state"]}
                        for item in store.list_occurrences(states=("failed", "uncertain"),
                                                           limit=5)]
        except StateError as error:  # a failing store never takes the status surface down
            states, wake, failures, store_error = collections.Counter(), None, [], str(error)
        worker = self.host.worker_status()
        ready = worker is not None and worker["state"] in ("warm", "busy")
        loop_alive = self._loop is not None and self._loop.is_alive()
        current = self.scheduler.current
        return {
            "ok": True, "running": True, "product": "Brainstem Agent", "pid": os.getpid(),
            "home": str(self.home), "started_at": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "health": "ok" if loop_alive and store_error is None and (ready or not self.errors)
            else "degraded",
            "store": {"ok": store_error is None, "error": store_error},
            "workers": [] if worker is None else [worker],
            "active_turn": self.host.active_turn,
            "processes": self.host.process_organ.active(),
            "mcp": self.host.mcp_organ.status(),
            "scheduler": {
                "loop_alive": loop_alive, "next_fire_at": wake,
                "next_fire_local": schedules.local_iso(wake, schedules.local_zone()),
                "running_occurrence": current["occurrence_id"] if current else None,
                "schedules": dict(states)},
            "last_errors": sorted(list(self.errors) + failures,
                                  key=lambda item: -(item["at"] or 0))[:10],
        }

    def _handler(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def _reply(self, status: int, body: Mapping[str, Any]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def _refusal(self) -> tuple[int, str] | None:
                port = daemon._server.server_address[1]
                if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    return 403, "This control surface only answers on its loopback address."
                given = self.headers.get("Authorization") or ""
                if not (given.startswith("Bearer ") and hmac.compare_digest(
                        given[7:].encode(), daemon._token.encode())):
                    return 401, "Unauthorized: the daemon only answers its owner."
                return None

            def do_GET(self) -> None:
                refusal = self._refusal()
                if refusal:
                    return self._reply(refusal[0], {"error": refusal[1]})
                if self.path != "/v1/status":
                    return self._reply(404, {"error": "Unknown route."})
                self._reply(200, daemon.status())

            def do_POST(self) -> None:
                refusal = self._refusal()
                if refusal:
                    return self._reply(refusal[0], {"error": refusal[1]})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > _MAX_BODY:
                        return self._reply(413, {"error": "Request body is too large."})
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._reply(400, {"error": "The body must be a JSON object."})
                try:
                    self._route(body)
                except (ValueError, TypeError, HostError) as error:
                    self._reply(400, {"error": _redacted(str(error))})
                except StateError as error:  # the store failed: nothing is claimed as done
                    self._reply(503, {"error": _redacted(f"The cell's store failed: {error}")})

            def _route(self, body: dict) -> None:
                if self.path == "/v1/turn":
                    return self._reply(200, daemon.turn(body))
                if self.path == "/chat":  # RAPP/1: exactly response, agent_logs, session_id
                    result = daemon.turn({"message": body.get("user_input"),
                                          "session_id": body.get("session_id")})
                    if result["ok"]:
                        return self._reply(200, result["response"])
                    return self._reply(502 if result["state"] != "cancelled" else 409,
                                       {"error": result["error"], "state": result["state"]})
                if self.path == "/v1/tool":
                    return self._reply(200, daemon.tool(body))
                if self.path == "/v1/cancel":
                    if body.get("active") is True:  # the `cancel` command: the active turn
                        return self._reply(200, daemon.host.cancel())
                    event = daemon._requests.get(body.get("request_id"))
                    if event is not None:
                        event.set()
                    return self._reply(200, {"cancelled": event is not None})
                if self.path == "/v1/progress":
                    return self._reply(200, daemon.progress(body))
                if self.path == "/v1/wake":
                    daemon.scheduler.wake()
                    return self._reply(200, {"ok": True})
                if self.path == "/v1/stop":
                    daemon.stop()  # from here on no worker starts; report the ones that exist
                    worker = daemon.host.worker_status()
                    return self._reply(200, {"ok": True, "stopping": True, "pid": os.getpid(),
                                             "workers": [] if worker is None else [worker]})
                self._reply(404, {"error": "Unknown route."})

        return Handler


# -- foreground, detached, stop ------------------------------------------------------------
def serve_foreground(daemon: Daemon, ready: Callable[[dict], None] | None = None) -> dict:
    """Serve on the main thread; SIGTERM and SIGINT stop cleanly."""
    previous = {number: signal.signal(number, lambda *_: daemon.stop())
                for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        return daemon.serve(ready=ready)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _child_env(environ: Mapping[str, str]) -> dict:
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [_PACKAGE_PARENT] + [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item])
    return env


def spawn(home: Path, environ: Mapping[str, str], arguments: list[str], *,
          timeout: float = 60.0) -> dict:
    """Start ``serve`` detached (own session, log in logs/daemon.log); wait until it answers."""
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = os.open(logs / "daemon.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                  0o600)
    try:
        process = subprocess.Popen([sys.executable, "-m", "brainstem_agent", "serve", *arguments],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=_child_env(environ), cwd=str(home),
                                   start_new_session=True)
    finally:
        os.close(log)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DaemonError(f"The daemon exited during startup (exit {process.returncode}); "
                              f"see {logs / 'daemon.log'}.")
        record = read_record(home)
        if record is not None and record.get("pid") == process.pid:
            try:
                return Client(record).call("GET", "/v1/status", timeout=5)
            except DaemonError:
                pass
        time.sleep(0.05)
    raise DaemonError("The daemon did not become ready in time.")


def _lock_free(home: Path) -> bool:
    try:
        descriptor = os.open(home / "state" / "host.lock", os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return True
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)


def _last_stop(home: Path, pid: int, since: float) -> dict:
    """The stop record a daemon (``pid``) wrote for a stop requested at ``since``."""
    try:
        descriptor = os.open(Path(home) / "run" / LAST_STOP, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            record = json.loads(handle.read(1 << 20))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict) or record.get("pid") != pid or not isinstance(
            record.get("stopped_at"), (int, float)) or record["stopped_at"] < since:
        return {}
    return record


def stop(home: Path, *, timeout: float = 30.0) -> dict:
    """Ask the daemon to stop; wait until it released the home and its workers are gone.

    Every worker group the daemon had, or reported stopping, is measured afterwards."""
    client = connect(home)
    if client is None:
        return {"ok": True, "stopped": False, "running": False}
    started, since = time.monotonic(), time.time()
    try:
        status = client.call("GET", "/v1/status", timeout=10)
        answer = client.call("POST", "/v1/stop", timeout=10)
    except DaemonUnavailable:
        return {"ok": True, "stopped": False, "running": False}
    deadline = started + timeout
    while time.monotonic() < deadline and (record_path(home).exists() or not _lock_free(home)):
        time.sleep(0.05)
    released = not record_path(home).exists() and _lock_free(home)
    final = _last_stop(home, client.pid, since) if released else {}
    pgids: list[int] = []
    for item in [*status.get("workers", []), *answer.get("workers", []),
                 *final.get("workers", [])]:
        if item.get("pgid") and item["pgid"] not in pgids:
            pgids.append(item["pgid"])
    groups = [{"pgid": pgid, "group_state": lifeline.group_state(pgid)} for pgid in pgids]
    gone = all(item["group_state"] == lifeline.GONE for item in groups)
    return {"ok": released and gone, "stopped": released, "pid": client.pid,
            "seconds": round(time.monotonic() - started, 3), "workers": groups,
            "workers_gone": gone}


# -- launchd -------------------------------------------------------------------------------
_PASSTHROUGH = ("BRAINSTEM_AGENT_CACHE", "BRAINSTEM_AGENT_WORKSPACE", "BRAINSTEM_AGENT_MODEL",
                "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE", "BRAINSTEM_HOME", "BRAINSTEM_AGENT_GRAIL_SEED")


def service(action: str, home: Path, environ: Mapping[str, str], *, dry_run: bool) -> dict:
    """Install or uninstall the LaunchAgent that keeps this home's daemon running.

    ``BRAINSTEM_AGENT_LAUNCH_AGENTS`` and ``BRAINSTEM_AGENT_LAUNCHCTL`` redirect the
    directory and the launchctl binary (tests never touch the real user domain).
    """
    label = "com.brainstem-agent.cell." + hashlib.sha256(str(home).encode()).hexdigest()[:12]
    directory = Path(environ.get("BRAINSTEM_AGENT_LAUNCH_AGENTS")
                     or os.path.expanduser("~/Library/LaunchAgents"))
    path = directory / f"{label}.plist"
    launchctl = environ.get("BRAINSTEM_AGENT_LAUNCHCTL") or "/bin/launchctl"
    domain = f"gui/{os.getuid()}"
    env = {"PYTHONPATH": _PACKAGE_PARENT, "BRAINSTEM_AGENT_HOME": str(home),
           "PATH": "/usr/bin:/bin"}
    env.update({key: environ[key] for key in _PASSTHROUGH if environ.get(key)})
    plist = plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "brainstem_agent", "serve"],
        "EnvironmentVariables": env, "WorkingDirectory": str(home), "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
        "StandardOutPath": str(home / "logs" / "daemon.log"),
        "StandardErrorPath": str(home / "logs" / "daemon.log")})
    if action == "install":
        commands = [[launchctl, "bootstrap", domain, str(path)]]
    else:
        commands = [[launchctl, "bootout", domain, str(path)]]
    document = {"ok": True, "action": action, "dry_run": dry_run, "label": label,
                "path": str(path), "commands": commands}
    if action == "install":
        document["plist"] = plist.decode("utf-8")
    if dry_run:
        return document
    if action == "install":
        (home / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plist)
    results = [subprocess.run(command, capture_output=True, text=True, timeout=30)
               for command in commands]
    if action == "uninstall":
        path.unlink(missing_ok=True)
    document["exit_codes"] = [result.returncode for result in results]
    document["ok"] = all(code == 0 for code in document["exit_codes"]) or action == "uninstall"
    return document
