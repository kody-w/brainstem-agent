"""Long-lived daemon hygiene and honest status, unit tier.

Real daemon, host, broker, store, sandbox and lifeline; only Grail is replaced by
scripted workers (some with a real process group, so stop reports are measured).

- Per-turn broker state never accumulates across many turns and tool calls.
- A transient store failure while the daemon starts (a locked database, a failing
  recovery) delays it; it does not crash it.
- Stopping during a turn reports the worker that turn used and its measured group.
- A program launched just before its host dies is never left running unrecorded
  (the launch gate), for shell commands and for Grail workers.
- A tool whose receipt cannot be recorded never lets its turn report success.
- ``status`` never calls a still-starting worker warm.
- No server the cell starts asks DNS about its own address (``HTTPServer.server_bind``'s
  ``socket.getfqdn``), which stalls for tens of seconds on hosts with slow reverse DNS.
"""

import ast
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from acceptance_support import (OWNER_HOME, RUNTIME, criteria, isolated_env, kill_quietly,
                                pid_running, private_dir, run_cli, wait_until, write_token_file)
from pathlib import Path
from brainstem_agent import daemon, lifeline, sandbox
from brainstem_agent.host import AgentHost
from brainstem_agent.state import StateError, Store
from daemon_support import ScriptedWorker, daemon_env
from test_cell_host import FakeWorker, done, sse


def broker_state(broker):
    return {"binds": len(broker._binds), "bind_counts": len(broker._bind_counts),
            "cancelled": len(broker._cancelled), "inflight": len(broker._inflight)}


class Case(unittest.TestCase):
    def setUp(self):
        self.scratch = private_dir(self)
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(self.scratch)
        self.environ = daemon_env({"HOME": str(self.scratch)}, self.token, self.scratch)
        self.workers = []

    def remember(self, worker):
        self.workers.append(worker)
        return worker

    def serve(self, cell):
        """Serve ``cell`` on a thread; returns (ready event, box with status/evidence/error)."""
        ready, box = threading.Event(), {}

        def target():
            try:
                box["evidence"] = cell.serve(ready=lambda status: (box.update(status=status),
                                                                   ready.set()))
            except BaseException as error:  # noqa: BLE001 - the test inspects it
                box["error"] = error
                ready.set()
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        box["thread"] = thread

        def cleanup():
            cell.stop()
            thread.join(60)
        self.addCleanup(cleanup)
        return ready, box


class BookkeepingTests(Case):
    @criteria("B1")
    def test_many_turns_and_tool_calls_leave_no_per_turn_state_behind(self):
        cancel = threading.Event()

        def script(worker, request, grant):
            bound = worker.bind(grant)
            worker.invoke(grant, bound, "list_files", {"path": "."})
            yield sse({"type": "delta", "text": "working"})
            if "cancel" in request["user_input"]:
                cancel.set()
                yield sse({"type": "delta", "text": "still working"})
            yield done(request, "ok")
        host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                         worker_factory=lambda **options: self.remember(
                             FakeWorker(script, **options)))
        self.addCleanup(host.close)
        for number in range(30):
            if number == 10:
                cancelled = host.chat("cancel me", cancel_event=cancel)
                self.assertEqual(cancelled.state, "cancelled")
                cancel.clear()
            self.assertTrue(host.chat(f"turn {number}").ok)
            self.assertTrue(host.invoke_tool("list_files", {"path": "."})["ok"])
        self.assertEqual(broker_state(host.broker),
                         {"binds": 0, "bind_counts": 0, "cancelled": 0, "inflight": 0})
        self.assertEqual((host._inputs, host._calls._events, host._calls._cancelled,
                          host.memory_organ._counts), ({}, {}, set(), {}))
        self.assertEqual(len(host.receipts()), 61)  # the durable record is all still there


class StartupStoreFailureTests(Case):
    def factory(self, **options):
        return self.remember(ScriptedWorker(**options))

    @criteria("B1")
    def test_a_failing_recovery_at_startup_is_retried_not_fatal(self):
        original, failures = Store.recover_interrupted, {"left": 2}

        def flaky(store):
            if failures["left"]:
                failures["left"] -= 1
                raise StateError("Fixture database transaction failed")
            return original(store)
        with mock.patch.object(Store, "recover_interrupted", flaky):
            cell = daemon.Daemon(self.home, workspace=self.workspace, environ=self.environ,
                                 worker_factory=self.factory)
            ready, box = self.serve(cell)
            self.assertTrue(ready.wait(60))
        self.assertNotIn("error", box, repr(box.get("error")))
        self.assertEqual(failures["left"], 0)
        errors = [item["error"] for item in cell.status()["last_errors"]]
        self.assertTrue(any("startup" in error and "recovery" in error for error in errors),
                        errors)
        self.assertIsNotNone(daemon.read_record(self.home))
        cell.stop()
        box["thread"].join(60)
        self.assertIn("evidence", box)

    @criteria("B1")
    def test_a_database_locked_by_another_process_at_startup_delays_the_daemon(self):
        """The lock is released only once the daemon has reported it: however long SQLite's
        own busy wait takes on a slow host, the daemon waits, reports and then starts."""
        AgentHost(self.home, workspace=self.workspace, environ=self.environ).close()
        locker = sqlite3.connect(self.home / "state" / "agent.sqlite3", timeout=0,
                                 isolation_level=None, check_same_thread=False)
        locker.execute("BEGIN EXCLUSIVE")
        held = [True]

        def release():
            if held and held.pop():
                locker.rollback()
                locker.close()
        self.addCleanup(release)
        reported, made = threading.Event(), {}
        original = daemon.Daemon._error

        def noting(cell, text):
            original(cell, text)
            if text.startswith("startup store"):
                reported.set()

        def construct():
            try:
                made["cell"] = daemon.Daemon(self.home, workspace=self.workspace,
                                             environ=self.environ, worker_factory=self.factory)
            except BaseException as error:  # noqa: BLE001 - the test inspects it
                made["error"] = error
        with mock.patch.object(daemon.Daemon, "_error", noting):
            constructing = threading.Thread(target=construct, daemon=True)
            constructing.start()
            self.assertTrue(reported.wait(120), "the locked store is reported while it waits")
            self.assertTrue(constructing.is_alive(), "it keeps waiting while the store is locked")
            release()
            constructing.join(120)
        self.assertNotIn("error", made, repr(made.get("error")))
        cell = made["cell"]
        self.assertTrue(any(item["error"].startswith("startup store") for item in cell.errors))
        ready, box = self.serve(cell)
        self.assertTrue(ready.wait(60))
        self.assertNotIn("error", box, repr(box.get("error")))
        self.assertTrue(any("startup" in item["error"] for item in cell.status()["last_errors"]))


class ProcessWorker(ScriptedWorker):
    """A scripted worker with a real process group, so stop reports can be measured."""

    stubborn = False

    def start(self):
        info = super().start()
        self.process = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
        self.pid = self.pgid = self.process.pid
        return {**info, "pid": self.pid}

    def stop(self, **options):
        evidence = super().stop(**options)
        if self.stubborn:  # a group that survives its stop
            return {**evidence, "group_gone": False, "group_state": lifeline.ALIVE}
        stopped = lifeline.stop_group(self.pgid, process=self.process, grace=0, timeout=5)
        return {**evidence, "group_gone": stopped["group_gone"],
                "group_state": stopped["group_state"]}

    def stream_chat(self, request, grant, *, read_timeout=300.0):
        def stream():  # like Grail's stream, it breaks when the worker is stopped
            self.bind(grant)
            yield sse({"type": "delta", "text": "working"})
            if self.stopped.wait(20):
                raise ConnectionResetError("the worker was stopped mid-turn")
            yield done(request, "paused")
        return stream()


class StopDuringTurnTests(Case):
    def stop_mid_turn(self, stubborn=False):
        def factory(**options):
            worker = ProcessWorker(**options)
            worker.stubborn = stubborn
            return self.remember(worker)
        cell = daemon.Daemon(self.home, workspace=self.workspace, environ=self.environ,
                             worker_factory=factory)
        ready, box = self.serve(cell)
        self.assertTrue(ready.wait(60))
        self.assertTrue(wait_until(lambda: any(w.get("warm") for w in cell.status()["workers"]),
                                   15))
        worker = self.workers[-1]
        self.addCleanup(kill_quietly, worker.pid)
        client, outcome = daemon.connect(self.home), {}
        turn = threading.Thread(target=lambda: outcome.update(result=client.call(
            "POST", "/v1/turn", {"message": "[[pause 20]]", "workspace": str(self.workspace)},
            timeout=90)), daemon=True)
        turn.start()
        self.assertTrue(wait_until(lambda: (cell.host.active_turn or {}).get("phase")
                                   == "streaming", 15))
        stopped = daemon.stop(self.home)
        box["thread"].join(60)
        turn.join(30)
        return worker, stopped, box["evidence"], outcome.get("result")

    @criteria("B1")
    def test_stop_during_a_turn_reports_that_turns_worker_and_its_measured_group(self):
        worker, stopped, evidence, result = self.stop_mid_turn()
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(stopped["ok"] and stopped["workers_gone"], stopped)
        self.assertEqual([item["pgid"] for item in stopped["workers"]], [worker.pgid])
        self.assertIsNotNone(evidence["worker"], evidence)
        self.assertEqual(evidence["worker"]["pgid"], worker.pgid)
        self.assertEqual([(item["pgid"], item["group_state"]) for item in evidence["workers"]],
                         [(worker.pgid, lifeline.GONE)])
        self.assertTrue(evidence["group_gone"])
        self.assertFalse(pid_running(worker.pid))

    @criteria("B1")
    def test_a_worker_group_that_survives_its_stop_is_reported_alive(self):
        worker, stopped, evidence, _result = self.stop_mid_turn(stubborn=True)
        self.assertTrue(pid_running(worker.pid))
        self.assertFalse(stopped["workers_gone"], stopped)
        self.assertFalse(stopped["ok"])
        self.assertFalse(evidence["group_gone"], evidence)
        self.assertEqual(evidence["workers"][0]["group_state"], lifeline.ALIVE)


SHELL_HOST = r"""
import os, signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brainstem_agent import lifeline
from brainstem_agent.organs.base import InvocationContext
from brainstem_agent.organs.shell import ShellOrgan
home, workspace = Path(sys.argv[2]), Path(sys.argv[3])
supervisor = lifeline.Supervisor(home)
# The host dies right after it launched the command, before it could record the pid.
lifeline.Tracked.started = lambda self, *args, **kwargs: os.kill(os.getpid(), signal.SIGKILL)
organ = ShellOrgan(run_root=home / "run" / "shell", deny_read=(), environ=dict(os.environ),
                   supervisor=supervisor)
context = InvocationContext(owner="local", workspace=str(workspace), namespace="ns",
                            session_id="s", turn_id="t", call_id="call_gate",
                            workspace_root=workspace, capabilities=("shell.run",),
                            deadline=time.monotonic() + 60)
organ.invoke(context, "run_command", {"command": "echo $$ > ran.pid; exec /bin/sleep 30"})
"""

WORKER_HOST = r"""
import hashlib, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brainstem_agent import lifeline
from brainstem_agent.grail import GrailSource
from brainstem_agent.worker import GrailWorker, WorkerConfig
home, source = Path(sys.argv[2]), Path(sys.argv[3])
inventory = {"brainstem.py": hashlib.sha256((source / "brainstem.py").read_bytes()).hexdigest()}
supervisor = lifeline.Supervisor(home)
lifeline.Tracked.started = lambda self, *args, **kwargs: os.kill(os.getpid(), signal.SIGKILL)
config = WorkerConfig(worker_id="wgate", home=home, source=GrailSource("0" * 40, source, inventory),
                      python=Path("/bin/sh"), broker_url="http://127.0.0.1:9",
                      supervisor=supervisor)
GrailWorker(config, register=lambda *_: "key").start()
"""


@unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
class LaunchGateTests(Case):
    """The window between Popen and recording the pid (found by a fault-injection soak)."""

    def host(self, script, *arguments):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(OWNER_HOME), "LANG": "en_US.UTF-8"}
        return subprocess.run([sys.executable, "-c", script, str(RUNTIME), *map(str, arguments)],
                              capture_output=True, text=True, timeout=60, env=env)

    def assert_never_ran(self, marker):
        time.sleep(1.5)
        if marker.exists():
            pid = int(marker.read_text().strip() or 0)
            if pid:
                self.addCleanup(kill_quietly, pid)
            self.fail(f"the program ran after its host died (pid {pid}, running "
                      f"{pid_running(pid) if pid else False}), unrecorded")

    @criteria("A8", "B8")
    def test_a_shell_command_launched_just_before_its_host_dies_never_runs(self):
        host = self.host(SHELL_HOST, self.home, self.workspace)
        self.assertEqual(host.returncode, -signal.SIGKILL, host.stderr[-500:])
        self.assert_never_ran(self.workspace / "ran.pid")

    @criteria("A8", "B1")
    def test_a_grail_worker_launched_just_before_its_host_dies_never_runs(self):
        source = private_dir(self)
        (source / "brainstem.py").write_text('echo $$ > "$HOME/ran.pid"\nexec /bin/sleep 30\n')
        host = self.host(WORKER_HOST, self.home, source)
        self.assertEqual(host.returncode, -signal.SIGKILL, host.stderr[-500:])
        [tree] = (self.home / "workers" / "wgate").iterdir()
        self.assert_never_ran(tree / "home" / "ran.pid")


class ReceiptFailureTests(Case):
    def host_with(self, seen):
        def script(worker, request, grant):
            bound = worker.bind(grant)
            try:
                seen.append(worker.invoke(grant, bound, "write_file",
                                          {"path": "out.txt", "content": "made"}))
            except OSError as error:  # the bridge turns this into "the cell did not answer"
                seen.append(("no answer", type(error).__name__))
            yield done(request, "Wrote out.txt.")
        host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                         worker_factory=lambda **options: FakeWorker(script, **options))
        self.addCleanup(host.close)
        return host

    @criteria("A8", "B8")
    def test_a_tool_whose_outcome_cannot_be_recorded_never_lets_its_turn_succeed(self):
        seen = []
        host = self.host_with(seen)
        original = host.store.finish_receipt

        def failing(receipt_id, state, result):
            if state in ("succeeded", "failed"):
                raise StateError("Fixture database transaction failed")
            return original(receipt_id, state, result)
        with mock.patch.object(host.store, "finish_receipt", failing):
            result = host.chat("Write out.txt")
        self.assertEqual((result.ok, result.state), (False, "uncertain"), result.error)
        self.assertIn("could not be recorded", result.error)
        self.assertEqual((self.workspace / "out.txt").read_text(), "made")  # it did happen
        [(status, body)] = seen
        self.assertEqual((status, body["ok"]), (200, True))  # and the model was told so
        self.assertIn("Wrote 4 bytes", body["content"])
        [receipt] = host.receipts(turn_id=result.turn_id)
        self.assertEqual(receipt["state"], "started")  # left for recovery: uncertain

    @criteria("A8", "B8")
    def test_a_tool_call_that_cannot_be_recorded_is_not_run_and_its_turn_fails(self):
        seen = []
        host = self.host_with(seen)
        with mock.patch.object(host.store, "begin_receipt",
                               side_effect=StateError("Fixture database transaction failed")):
            result = host.chat("Write out.txt")
        self.assertEqual((result.ok, result.state), (False, "failed"), result.error)
        self.assertIn("could not be recorded", result.error)
        self.assertFalse((self.workspace / "out.txt").exists())
        [(status, body)] = seen
        self.assertEqual(status, 503)
        self.assertIn("not run", body["error"])


class SlowStartWorker(ScriptedWorker):
    gate = threading.Event()
    entered = threading.Event()

    def start(self):
        self.generation = "g" + secrets.token_hex(6)
        self.key = self.register(self.worker_id, self.generation)
        SlowStartWorker.entered.set()
        SlowStartWorker.gate.wait(60)
        return {"generation": self.generation, "integrity_before": {"ok": True, "files": 30},
                "start_seconds": 0.0, "pid": None}


class StatusTests(Case):
    @criteria("B1")
    def test_status_never_reports_a_still_starting_worker_as_warm(self):
        SlowStartWorker.gate.clear()
        SlowStartWorker.entered.clear()
        self.addCleanup(SlowStartWorker.gate.set)
        cell = daemon.Daemon(self.home, workspace=self.workspace, environ=self.environ,
                             worker_factory=lambda **options: self.remember(
                                 SlowStartWorker(**options)))
        ready, box = self.serve(cell)
        self.assertTrue(ready.wait(60))
        self.assertTrue(SlowStartWorker.entered.wait(15))
        env = daemon_env(isolated_env(self.home, self.scratch), self.token, self.scratch)
        [starting] = cell.status()["workers"]
        self.assertEqual((starting["state"], starting["warm"]), ("starting", False))
        human = run_cli(["status"], env, timeout=60).stdout
        self.assertIn("0 warm worker(s)", human)
        SlowStartWorker.gate.set()
        self.assertTrue(wait_until(lambda: cell.status()["workers"][0]["warm"], 15))
        [warm] = cell.status()["workers"]
        self.assertEqual(warm["state"], "warm")
        self.assertIn("1 warm worker(s)", run_cli(["status"], env, timeout=60).stdout)


def _no_reverse_dns(*_arguments):
    raise AssertionError("a reverse DNS lookup of a server's own address")


# Classes whose server_bind asks DNS for the bind address (socket.getfqdn).
_LOOKUP_SERVERS = frozenset({"HTTPServer", "ThreadingHTTPServer", "WSGIServer",
                             "BaseWSGIServer", "ThreadedWSGIServer"})


class ReverseDnsTests(Case):
    """On a host whose reverse lookup of 127.0.0.1 stalls (about 35 s on a hosted CI Mac),
    the stdlib ``HTTPServer.server_bind`` blocks in ``socket.getfqdn``; the cell's servers
    bind without it."""

    def factory(self, **options):
        return self.remember(ScriptedWorker(**options))

    @criteria("B1")
    def test_the_broker_and_the_daemon_start_promptly_without_reverse_dns(self):
        with mock.patch("socket.getfqdn", _no_reverse_dns), \
                mock.patch("socket.gethostbyaddr", _no_reverse_dns):
            started = time.monotonic()
            host = AgentHost(self.home, workspace=self.workspace, environ=self.environ)
            try:
                self.assertEqual(host.broker._server.server_name, "127.0.0.1")
                self.assertEqual(host.broker._server.server_port, host.broker.port)
            finally:
                host.close()
            cell = daemon.Daemon(self.home, workspace=self.workspace, environ=self.environ,
                                 worker_factory=self.factory)
            ready, box = self.serve(cell)
            self.assertTrue(ready.wait(30))
            self.assertNotIn("error", box, repr(box.get("error")))
            status = daemon.connect(self.home).call("GET", "/v1/status", timeout=10)
            self.assertLess(time.monotonic() - started, 15, "servers started promptly")
        self.assertEqual(status["pid"], cell.status()["pid"])

    @criteria("B1")
    def test_no_server_binds_with_the_stdlib_reverse_lookup(self):
        """Every HTTP server class in the product and the test fixtures overrides
        ``server_bind``, and none is a stdlib server instantiated directly."""
        found = []
        for path in sorted([*(RUNTIME / "brainstem_agent").rglob("*.py"),
                            *Path(__file__).resolve().parent.glob("*.py")]):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = {getattr(base, "id", getattr(base, "attr", None))
                             for base in node.bases}
                    binds = any(isinstance(item, ast.FunctionDef) and item.name == "server_bind"
                                for item in node.body)
                    if bases & _LOOKUP_SERVERS and not binds:
                        found.append(f"{path.name}:{node.lineno} class {node.name}")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", None))
                    if name in _LOOKUP_SERVERS or name == "make_server":
                        found.append(f"{path.name}:{node.lineno} {name}(...)")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
