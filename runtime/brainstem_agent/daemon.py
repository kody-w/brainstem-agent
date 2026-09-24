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
``GET /v1/health`` (liveness versus readiness, every check with a reason and a fix),
``GET /v1/version``, ``POST /v1/turn`` (the full turn result, for the CLI), ``/v1/tool``,
``/v1/cancel``, ``/v1/wake``, ``/v1/drain`` (finish the running work, start nothing new) and
``/v1/stop``. The token is never printed or logged.

The same listener serves the owner-only web companion and every documented route
(``api.ROUTES``, ``GET /v1/api``): the gate, sessions and hardening live in
``surface.py`` and ``companion.py``; streamed turns (``POST /v1/requests`` and their
event streams) in ``streaming.py``.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from . import health, hygiene, lifeline, release, schedules, streaming
from .broker import LoopbackHTTPServer
from .credential_state import classify as credential_problem
from .host import AgentHost, HostError, TurnResult
from .longturn import TurnBudget
from .state import StateError

__all__ = ["Client", "Daemon", "DaemonError", "DaemonUnavailable", "connect", "read_record",
           "service", "spawn", "stop"]

RECORD = "daemon.json"
LAST_STOP = "last-stop.json"
# The last port, preferred on the next start when nothing listens there: an open companion
# tab keeps its origin across a restart and can tell "restarted" from "unreachable".
PORT_RECORD = "port.json"
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[1])
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_MAX_BODY = 1 << 20
_TURN_TIMEOUT = 300.0
_STARTUP_RETRY_SECONDS = 60.0
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
_HYGIENE_SECONDS = 60.0
_DRAINING = ("The daemon is draining for maintenance (an upgrade, rollback or stop); nothing was "
             "run. Try again once it is back (brainstem-agent status).")


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


    def stream(self, path: str, *, timeout: float = 60.0):
        """Yield the JSON events of a ``text/event-stream`` route until it ends."""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="GET",
            headers={"Authorization": "Bearer " + self._token, "Accept": "text/event-stream"})
        try:
            response = _OPENER.open(request, timeout=timeout)
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
        with response:
            data: list[str] = []
            while True:
                try:
                    raw = response.readline(1 << 22)
                except OSError as error:
                    raise DaemonError(f"The daemon's stream broke: {type(error).__name__}") \
                        from None
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    continue
                if data:
                    yield json.loads("\n".join(data))
                    data = []


def connect(home: Path | str) -> Client | None:
    record = read_record(home)
    return None if record is None else Client(record)


def port_is_free(port: int) -> bool:
    """Whether nothing accepts connections on 127.0.0.1:``port`` (a listener on every
    address included); anything but a refusal counts as taken."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def _previous_port(home: Path) -> int | None:
    """The port this home's daemon last used (a private file of ours), else None."""
    try:
        descriptor = os.open(home / "run" / PORT_RECORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return None
        try:
            port = json.loads(handle.read(256))["port"]
        except (ValueError, KeyError, TypeError):
            return None
    return port if isinstance(port, int) and 1024 <= port <= 65535 else None


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
        # Answer deltas are observed on the way from each worker to the (unchanged) host.
        self.tee = streaming.StreamTee()
        factory = self.tee.wrap(worker_factory, lambda: self.host._grail_worker)
        # A store that is briefly locked or failing delays the start instead of ending it.
        self.host = _retrying(lambda: AgentHost(
            self.home, workspace=workspace, cache=cache, environ=self.environ,
            worker_factory=factory, clock=_steady_clock()), self._error, "store")
        self.host.schedules_changed = self._changed
        # Background processes outlive the turn that started them while the daemon runs.
        self.host.process_organ.long_lived = True
        self.scheduler = schedules.Scheduler(self.host.store, self._run_occurrence,
                                             idle=self._idle, on_error=self._error)
        self.turns = threading.Lock()
        self.stopping = threading.Event()
        # Draining: the running turn and scheduled run finish; nothing new starts.
        self.draining = threading.Event()
        self.scheduler.paused = self.draining.is_set
        self.events = self.host.events
        self._hygiene_after = 0.0
        self._last_ready: bool | None = None
        self._warm_error: str | None = None
        self._check_cache: dict[str, tuple[float, Any]] = {}
        self._requests: dict[str, threading.Event] = {}
        # Progress events of recent requests (``/v1/progress``), bounded and short-lived.
        self._progress: collections.OrderedDict = collections.OrderedDict()
        self._progress_lock = threading.Lock()
        self._token = secrets.token_urlsafe(32)
        # Streamed turns (POST /v1/requests), the companion's sessions and the HTTP gate.
        self.requests = streaming.Requests()
        from .surface import Surface

        self.surface = Surface(self)
        self.companion = self.surface.auth
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
                self._server = self._bind()
                threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                 daemon=True, name="brainstem-agent-control").start()
                self._write_record()
                self._loop = threading.Thread(target=self.scheduler.run, daemon=True,
                                              name="brainstem-agent-scheduler")
                self._loop.start()
                self.events.write("daemon.started", version=release.identity()["id"],
                                  port=self._server.server_address[1])
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
        self.events.write("daemon.stopped", seconds=self.stop_evidence["seconds"],
                          group_gone=self.stop_evidence["group_gone"],
                          drained=self.draining.is_set())
        return self.stop_evidence

    def _bind(self) -> "_Server":
        """The control server on 127.0.0.1: the previous port when nothing listens there,
        else any free port. The port is recorded for the next start."""
        server, preferred = None, _previous_port(self.home)
        if preferred is not None and port_is_free(preferred):
            try:
                server = _Server(("127.0.0.1", preferred), self._handler())
            except OSError:
                server = None
        if server is None:
            server = _Server(("127.0.0.1", 0), self._handler())
        try:
            _write_private(self.home / "run" / PORT_RECORD, {"port": server.server_address[1]})
        except OSError:
            pass  # only the next start's preference is lost
        return server

    def _write_record(self) -> None:
        _write_private(self.home / "run" / RECORD,
                       {"pid": os.getpid(), "port": self._server.server_address[1],
                        "token": self._token, "started_at": self.started_at,
                        "workspace": str(self.host.workspace),
                        "version": release.identity()["id"]})

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
        self._maintain()
        if (self.stopping.is_set() or self.draining.is_set() or time.monotonic() < self._warm_after
                or not self.turns.acquire(blocking=False)):
            return
        try:
            # Nothing starts while the credential is known to be rejected (no retry loop); a
            # changed credential file is picked up here without a restart.
            self.host.warm()
            self._warm_after, self._warm_error = 0.0, None
        except Exception as error:  # no credential, no network, bad cache: retry later
            # A rejected credential is gated by its recorded state instead (nothing starts
            # until the token file changes), so a new sign-in is picked up at the next wake.
            self._warm_after = 0.0 if credential_problem(str(error)) else time.monotonic() + 60
            self._warm_error = _redacted(str(error))
            self._error(f"warm worker: {error}")
            self.events.write("worker.warm_failed", level="warn", error=str(error)[:300])
        finally:
            self.turns.release()

    def _maintain(self) -> None:
        """Periodic hygiene (bounded logs) and a log line whenever readiness changes."""
        if time.monotonic() < self._hygiene_after:
            return
        self._hygiene_after = time.monotonic() + _HYGIENE_SECONDS
        try:
            done = hygiene.log_hygiene(self.home, hygiene.load_policy(self.home, self.environ)[0])
            if done["rotated"] or done["worker_logs_removed"]:
                self.events.write("logs.rotated", **done)
            report = self.health()
            if report["ready"] != self._last_ready:
                self.events.write("health.changed", level="info" if report["ready"] else "warn",
                                  ready=report["ready"], live=report["live"],
                                  failing=report["failing"])
                self._last_ready = report["ready"]
        except Exception as error:  # hygiene never takes the daemon down
            self._error(f"maintenance: {error}")

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

    def turn(self, body: Mapping[str, Any], *, sink: Callable[[dict], None] | None = None,
             acquired: Callable[[], None] | None = None) -> dict:
        message = body.get("message")
        if self.draining.is_set():
            return TurnResult(False, "failed", False, None, body.get("session_id"), None,
                              _DRAINING, {"refused": "draining", "worker": None,
                                          "grail_calls": 0}).to_json()
        with self._cancellable(body) as cancel:
            if not isinstance(message, str):
                raise ValueError("message must be a string")
            budget = self._budget(body)
            sink = sink or self._progress_sink(body)
            timeout = float(body.get("timeout") or _TURN_TIMEOUT)
            if not self._acquire(cancel, timeout):
                waited = cancel.is_set() or self.stopping.is_set()
                return TurnResult(False, "cancelled" if waited else "failed", False, None,
                                  body.get("session_id"), None,
                                  "The turn was cancelled before it started." if waited else
                                  f"The worker stayed busy with another turn for {timeout:g}s; "
                                  "nothing was run.", {"worker": None, "grail_calls": 0}).to_json()
            if self.draining.is_set():  # it waited behind the turn a drain let finish
                self.turns.release()
                return TurnResult(False, "failed", False, None, body.get("session_id"), None,
                                  _DRAINING, {"refused": "draining", "worker": None,
                                              "grail_calls": 0}).to_json()
            if acquired is not None:
                acquired()
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

    def start_request(self, body: Mapping[str, Any]) -> dict:
        """Start a turn on a daemon thread and return at once (``queued`` until the one worker
        is free); its events, answer deltas included, stream from ``/v1/requests/<id>/events``
        and the turn is cancelled through ``/v1/cancel`` with the request id."""
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be non-empty text")
        session_id = body.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string")
        if self.stopping.is_set():
            raise HostError("The daemon is stopping.")
        request = self.requests.create(message)
        request.emit({"event": "request.queued", "session_id": session_id})
        sink = streaming.follow(self.tee, request.emit)
        work = {key: body.get(key) for key in ("message", "session_id", "capabilities",
                                                "workspace", "budget", "idempotency_key",
                                                "timeout")}
        work["request_id"] = request.request_id

        def run() -> None:
            try:
                result = self.turn(work, sink=sink,
                                   acquired=lambda: request.emit({"event": "request.running"}))
            except Exception as error:  # a request always finishes, honestly
                result = TurnResult(False, "failed", False, None, session_id, None,
                                    _redacted(f"The turn could not run: {error}"),
                                    {"worker": None, "grail_calls": 0}).to_json()
            finally:
                sink.close()
            request.emit({"event": "request.finished", "result": result})

        threading.Thread(target=run, daemon=True, name="brainstem-agent-request").start()
        return request.describe()

    def tool(self, body: Mapping[str, Any]) -> dict:
        if self.draining.is_set():
            return {"ok": False, "content": _DRAINING, "refused": "draining"}
        with self._cancellable(body) as cancel:
            return self.host.invoke_tool(str(body.get("name")), dict(body.get("arguments") or {}),
                                         capabilities=body.get("capabilities"),
                                         cancel_event=cancel, workspace=body.get("workspace"))

    def _cached(self, key: str, seconds: float, compute: Callable[[], Any]) -> Any:
        found = self._check_cache.get(key)
        if found is None or time.monotonic() - found[0] > seconds:
            found = (time.monotonic(), compute())
            self._check_cache[key] = found
        return found[1]

    def health(self, worker: dict | None | bool = False) -> dict:
        """Liveness (this process answers and its schedule loop runs) versus readiness (a
        turn can run now), each check with a reason and a fix. Checks that read files or
        start a process are reused for up to 30 s. ``worker``: the worker snapshot a caller
        shows next to this report (``status``), so the two never disagree."""
        loop_alive = self._loop is not None and self._loop.is_alive()
        checks = [
            health.Check("process", True, f"pid {os.getpid()} answers", kind="liveness"),
            health.Check("scheduler_loop", loop_alive, "the schedule loop is running" if
                         loop_alive else "the schedule loop has stopped",
                         "Restart the daemon: brainstem-agent stop, then brainstem-agent serve "
                         "--detach", kind="liveness"),
            self._cached("grail", 30, lambda: health.grail_source_check(self.host.cache)),
            self._cached("venv", 30, lambda: health.worker_interpreter_check(
                self.host.cache, self.environ, import_check=False)),
            health.credential_check(self.home, self.environ),
            self._cached("sandbox", 300, lambda: health.sandbox_check(self.environ)),
            health.disk_check(self.home, self.environ),
        ]
        try:
            self.host.store.next_wake()
            checks.append(health.Check("store", True, "opens and answers"))
        except StateError as error:
            checks.append(health.Check("store", False, f"the store failed: {error}",
                                       "See brainstem-agent logs; if it persists, restore the "
                                       "latest backup (brainstem-agent restore)"))
        if worker is False:
            worker = self.host.worker_status()
        if worker is not None and worker["state"] in ("warm", "busy"):
            checks.append(health.Check("worker", True, f"a verified worker is {worker['state']}",
                                       detail={"worker_state": worker["state"]}))
        elif worker is not None and worker["state"] == "starting":
            checks.append(health.Check("worker", False, "the warm worker is still starting",
                                       "Wait a few seconds", detail={"worker_state": "starting"}))
        else:
            credential = next(item for item in checks if item.id == "credential")
            reason = ("no warm worker: " + (self._warm_error or "it has not started yet"))
            fix = credential.fix if not credential.ok else (
                "It starts by itself within 60 s; see brainstem-agent logs --event 'worker.*'")
            checks.append(health.Check("worker", False, reason[:400], fix,
                                       detail={"worker_state": None}))
        checks.append(health.mcp_check(self.home, self.host.mcp_organ.status(), required=True))
        checks.append(health.Check("draining", not self.draining.is_set(),
                                   "accepting work" if not self.draining.is_set() else
                                   "draining for maintenance: no new turns start",
                                   "Wait for the upgrade, rollback or stop to finish"))
        return {"product": "Brainstem Agent", "pid": os.getpid(),
                "version": release.identity()["id"], **health.summarize(checks)}

    def drain(self, body: Mapping[str, Any]) -> dict:
        """Stop starting work (turns, tools, scheduled runs) and wait up to ``timeout`` seconds
        for the running turn and scheduled run to finish. ``resume`` undoes it."""
        if body.get("resume") is True:
            self.draining.clear()
            self.scheduler.wake()
            self.events.write("daemon.resumed")
            return {"ok": True, "draining": False}
        timeout = min(3600.0, max(0.0, float(body.get("timeout") or 300.0)))
        started = time.monotonic()
        if not self.draining.is_set():
            self.draining.set()
            self.events.write("daemon.draining", timeout=timeout)
        while time.monotonic() - started < timeout:
            if self.host.active_turn is None and self.scheduler.current is None:
                break
            time.sleep(0.1)
        drained = self.host.active_turn is None and self.scheduler.current is None
        return {"ok": drained, "draining": True, "drained": drained,
                "seconds": round(time.monotonic() - started, 3),
                "active_turn": self.host.active_turn,
                "running_occurrence": (self.scheduler.current or {}).get("occurrence_id")}

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
            "companion": self.companion.describe(),
            "draining": self.draining.is_set(),
            "readiness": self.health(worker),
        }

    def _handler(self):
        return self.surface.handler_class()


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


def _child_env(environ: Mapping[str, str], package_parent: str | None = None) -> dict:
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_parent or _PACKAGE_PARENT]
        + [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item])
    if package_parent:
        env["BRAINSTEM_AGENT_REDIRECTED"] = Path(package_parent).name
    return env


def spawn(home: Path, environ: Mapping[str, str], arguments: list[str], *,
          timeout: float = 60.0, package_parent: str | None = None) -> dict:
    """Start ``serve`` detached (own session, log in logs/daemon.log); wait until it answers.
    ``package_parent`` runs another installed version (upgrade, rollback)."""
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    hygiene.log_hygiene(home, hygiene.load_policy(home, environ)[0])
    log = os.open(logs / "daemon.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                  0o600)
    try:
        process = subprocess.Popen([sys.executable, "-m", "brainstem_agent", "serve", *arguments],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=_child_env(environ, package_parent), cwd=str(home),
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


def service_plist_path(home: Path, environ: Mapping[str, str]) -> Path:
    """Where this home's LaunchAgent plist lives (``BRAINSTEM_AGENT_LAUNCH_AGENTS`` redirects)."""
    label = "com.brainstem-agent.cell." + hashlib.sha256(str(home).encode()).hexdigest()[:12]
    directory = Path(environ.get("BRAINSTEM_AGENT_LAUNCH_AGENTS")
                     or os.path.expanduser("~/Library/LaunchAgents"))
    return directory / f"{label}.plist"


def service(action: str, home: Path, environ: Mapping[str, str], *, dry_run: bool,
            package_parent: str | None = None) -> dict:
    """Install or uninstall the LaunchAgent that keeps this home's daemon running.

    ``BRAINSTEM_AGENT_LAUNCH_AGENTS`` and ``BRAINSTEM_AGENT_LAUNCHCTL`` redirect the
    directory and the launchctl binary (tests never touch the real user domain);
    ``package_parent`` makes the service run another installed version (upgrade, rollback).
    """
    path = service_plist_path(home, environ)
    label, directory = path.stem, path.parent
    launchctl = environ.get("BRAINSTEM_AGENT_LAUNCHCTL") or "/bin/launchctl"
    domain = f"gui/{os.getuid()}"
    env = {"PYTHONPATH": package_parent or _PACKAGE_PARENT, "BRAINSTEM_AGENT_HOME": str(home),
           "PATH": "/usr/bin:/bin"}
    if package_parent:
        env["BRAINSTEM_AGENT_REDIRECTED"] = Path(package_parent).name
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
