"""Process organ: sandboxed background processes that outlive a turn while the daemon runs.

Tools (``processes.run``): ``process_start``, ``process_status``, ``process_read``,
``process_write`` and ``process_stop``. A process runs ``/bin/sh -c`` inside the same
Seatbelt profile as the shell tool (no network, writes only in the workspace and its
private directory), in its own process group supervised by the lifeline, so it dies
with its host. Every process is journaled in the store (``processes``) as ``starting``
before it is launched, ``running`` once its pid is known and ``exited``, ``stopped`` or
``failed`` when it ends; after a crash, recovery marks what was running ``lost``.
Bounded: running processes per host, output kept per process (the first
``OUTPUT_CAP`` bytes are kept on disk and counted beyond), input per write. A process
belongs to its workspace: another workspace can neither see nor touch it. ``close()``
(the daemon's ``stop``, or the end of an in-process command) stops them all.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import lifeline, sandbox
from ..state import StateError
from .base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec
from .shell import owner_homes

MAX_RUNNING = 4
OUTPUT_CAP = 1 << 20
_SYSTEM_DENIED = ("/Users", "/Volumes", "/private/var/folders", "/private/tmp")
_SHELLS = ("/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash")
_ID = {"type": "string", "minLength": 1, "maxLength": 64,
       "description": "The process id (proc_...) from process_start."}


class _Process:
    def __init__(self, process_id: str, namespace: str, name: str, directory: Path) -> None:
        self.process_id, self.namespace, self.name = process_id, namespace, name
        self.directory = directory
        self.output = directory / "output.log"
        self.popen: subprocess.Popen | None = None
        self.tracked: lifeline.Tracked | None = None
        self.state = "starting"
        self.exit_code: int | None = None
        self.bytes = 0
        self.started = time.time()
        self.finished: float | None = None
        self.lock = threading.Lock()
        self.pumped = threading.Event()


class ProcessOrgan:
    name = "processes"

    def __init__(self, store, *, run_root: Path, deny_read: Sequence[Path] = (),
                 environ: Mapping[str, str] | None = None,
                 supervisor: lifeline.Supervisor | None = None,
                 crash: Callable[[str], None] = lambda _name: None,
                 max_running: int = MAX_RUNNING) -> None:
        self.store = store
        self.run_root = Path(run_root)
        self.deny_read = tuple(Path(item) for item in deny_read)
        self.environ = dict(os.environ if environ is None else environ)
        self.supervisor = supervisor
        self.crash = crash
        self.max_running = max_running
        # True in the daemon: a process then outlives the turn that started it.
        self.long_lived = False
        self._processes: dict[str, _Process] = {}
        self._lock = threading.Lock()
        self._closed = False

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("process_start", "Start a long-running /bin/sh command in the background "
                     "(sandboxed, no network, in the workspace), for example a build, a server "
                     "or a watcher. Returns its process id at once; poll it with process_status, "
                     "read its output with process_read, send it input with process_write and "
                     "stop it with process_stop.",
                     {"type": "object", "properties": {
                         "command": {"type": "string", "minLength": 1, "maxLength": 8000},
                         "name": {"type": "string", "maxLength": 80,
                                  "description": "A short label."}},
                      "required": ["command"]}, "processes.run", "external"),
            ToolSpec("process_status", "Show a background process's state (running or exited, "
                     "exit code, runtime, output size), or every process of this workspace with "
                     "process_id 'all'.",
                     {"type": "object", "properties": {"process_id": {
                         **_ID, "default": "all",
                         "description": "A process id, or 'all'."}},
                      "required": ["process_id"]}, "processes.run", "read"),
            ToolSpec("process_read", "Read a background process's output (stdout and stderr "
                     "together). Without offset: the latest output. With offset: from that byte "
                     "(use the next offset it reports to follow new output).",
                     {"type": "object", "properties": {
                         "process_id": _ID,
                         "offset": {"type": "integer", "minimum": 0},
                         "max_chars": {"type": "integer", "minimum": 1, "maximum": 16000,
                                       "description": "Default 4000."}},
                      "required": ["process_id"]}, "processes.run", "read"),
            ToolSpec("process_write", "Send text to a background process's standard input.",
                     {"type": "object", "properties": {
                         "process_id": _ID,
                         "input": {"type": "string", "maxLength": 8000,
                                   "description": "Text to send; include a newline to end a "
                                                  "line."},
                         "close_stdin": {"type": "boolean",
                                         "description": "Close its input afterwards (end of "
                                                        "file)."}},
                      "required": ["process_id", "input"]}, "processes.run", "external"),
            ToolSpec("process_stop", "Stop a background process and everything it started.",
                     {"type": "object", "properties": {"process_id": _ID},
                      "required": ["process_id"]}, "processes.run", "external"),
        ]

    def context(self, context: BindContext) -> str | None:
        return None

    def policy(self, workspace: Path, directory: Path) -> sandbox.SandboxPolicy:
        return sandbox.SandboxPolicy(
            read_denied=(*_SYSTEM_DENIED, *owner_homes(self.environ), *self.deny_read),
            readable=(workspace, directory), writable=(workspace, directory), network="none")

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        context.check()
        if tool == "process_start":
            return self._start(context, arguments["command"], arguments.get("name") or "")
        if tool == "process_status":
            return self._status(context, arguments.get("process_id") or "all")
        process = self._own(context, arguments["process_id"])
        if tool == "process_read":
            return self._read(process, arguments.get("offset"), arguments.get("max_chars") or 4000)
        if tool == "process_write":
            return self._write(process, arguments["input"], bool(arguments.get("close_stdin")))
        if tool == "process_stop":
            self.stop(process, "stopped")
            return ToolResult(f"Stopped {process.process_id} ('{process.name}'): "
                              f"{self._describe(process)}.",
                              evidence={"process_id": process.process_id, "state": process.state})
        raise OrganError(f"Unknown process tool {tool!r}.")

    # -- lifecycle ---------------------------------------------------------------------
    def _start(self, context: InvocationContext, command: str, name: str) -> ToolResult:
        if not sandbox.available(self.environ):
            raise OrganError("The process sandbox (Seatbelt) is unavailable; refusing to run "
                             "without isolation.")
        with self._lock:
            if self._closed:
                raise OrganError("The cell is stopping; no new process can start.")
            running = sum(1 for item in self._processes.values()
                          if item.state in ("starting", "running"))
            if running >= self.max_running:
                raise OrganError(f"At most {self.max_running} background processes may run at "
                                 "once; stop one first.")
            process_id = "proc_" + secrets.token_hex(6)
            label = " ".join((name or command).split())[:80] or process_id
            directory = self.run_root / process_id
            process = _Process(process_id, context.namespace, label, directory)
            finished = [item for item in self._processes.values()
                        if item.state not in ("starting", "running")]
            for item in finished[:max(0, len(self._processes) - 15)]:
                self._processes.pop(item.process_id, None)
                lifeline.remove_tree(item.directory)
            self._processes[process_id] = process
        for sub in ("home", "tmp"):
            (directory / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_real = Path(os.path.realpath(directory))
        workspace = Path(os.path.realpath(context.workspace_root))
        try:
            # Journaled before it exists, so a crash can never leave an unknown process.
            self.store.create_process(context.namespace, process_id, turn_id=context.turn_id,
                                      call_id=context.call_id, name=label, command=command)
        except StateError as error:
            self._forget(process)
            raise OrganError(f"The process could not be recorded, so it was not started: "
                             f"{error}") from None
        try:
            argv = sandbox.wrap(["/bin/sh", "-c", command],
                                self.policy(workspace, directory_real), environ=self.environ)
            if self.supervisor is not None:
                process.tracked = self.supervisor.track("process", directory)
            env = {"HOME": str(directory_real / "home"), "TMPDIR": str(directory_real / "tmp"),
                   "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                   "PWD": str(workspace), "SHELL": "/bin/sh"}
            process.popen = lifeline.gated_popen(
                argv, tracked=process.tracked,
                expect=(sandbox.sandbox_exec_path(self.environ), *_SHELLS), cwd=workspace,
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True)
        except (OSError, sandbox.SandboxUnavailable, lifeline.LifelineError) as error:
            process.state = "failed"
            self._record(process, "failed")
            if process.tracked is not None:
                process.tracked.finish()
            self._forget(process)
            raise OrganError(f"The process could not start: {error}") from None
        process.state = "running"
        self._record(process, "running", pid=process.popen.pid)
        self.crash("process.started")
        threading.Thread(target=self._pump, args=(process,), daemon=True,
                         name="brainstem-agent-process").start()
        lasting = ("It keeps running after this turn while the Brainstem Agent daemon runs."
                   if self.long_lived else
                   "It keeps running until this command ends (start the daemon with `serve` "
                   "for processes that outlive a turn).")
        return ToolResult(f"Started {process_id} ('{label}', pid {process.popen.pid}) in the "
                          f"background, sandboxed with no network. {lasting}",
                          evidence={"process_id": process_id, "pid": process.popen.pid})

    def _pump(self, process: _Process) -> None:
        """Copy output to the process's log (first OUTPUT_CAP bytes), then record its end."""
        popen = process.popen
        try:
            descriptor = os.open(process.output, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                 | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as log:
                while True:
                    data = popen.stdout.read1(65536)
                    if not data:
                        break
                    with process.lock:
                        keep = max(0, OUTPUT_CAP - process.bytes)
                        if keep:
                            log.write(data[:keep])
                            log.flush()
                        process.bytes += len(data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                code = popen.wait(5)
            except subprocess.TimeoutExpired:
                code = None
            for stream in (popen.stdout, popen.stdin if code is not None else None):
                try:
                    stream.close()
                except (OSError, ValueError, AttributeError):
                    pass
            process.pumped.set()
            with process.lock:
                if process.state == "running" and code is not None:
                    process.state, process.exit_code = "exited", code
                    process.finished = time.time()
                    finished = True
                else:
                    finished = False
            if finished:
                # Whatever it left in its group goes with it.
                stopped = lifeline.stop_group(popen.pid, process=popen, grace=0, timeout=3.0)
                if process.tracked is not None and stopped["group_state"] != lifeline.ALIVE:
                    process.tracked.finish()
                self._record(process, "exited", exit_code=code)

    def stop(self, process: _Process, state: str = "stopped") -> None:
        popen = process.popen
        with process.lock:
            active = process.state in ("starting", "running")
            if active:
                process.state = state
                process.finished = time.time()
        if not active or popen is None:
            return
        stopped = lifeline.stop_group(popen.pid, process=popen, grace=1.0, timeout=4.0)
        process.exit_code = stopped["exit_code"]
        process.pumped.wait(5)
        try:
            popen.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass
        if process.tracked is not None and stopped["group_state"] != lifeline.ALIVE:
            process.tracked.finish()
        self._record(process, state, exit_code=process.exit_code)

    def close(self) -> None:
        """Stop every process this host started (the daemon's stop, a command's end)."""
        with self._lock:
            self._closed = True
            processes = list(self._processes.values())
        threads = [threading.Thread(target=self.stop, args=(item,), daemon=True)
                   for item in processes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        for item in processes:
            lifeline.remove_tree(item.directory)

    def active(self) -> list[dict]:
        with self._lock:
            return [{"process_id": item.process_id, "name": item.name, "state": item.state,
                     "pid": item.popen.pid if item.popen else None}
                    for item in self._processes.values() if item.state == "running"]

    # -- tools -------------------------------------------------------------------------
    def _own(self, context: InvocationContext, process_id: str) -> _Process:
        with self._lock:
            process = self._processes.get(process_id)
        if process is None or process.namespace != context.namespace:
            known = self.store.get_process(context.namespace, process_id) \
                if process_id.startswith("proc_") else None
            if known is not None:
                raise OrganError(f"{process_id} is {known['state']}; it is no longer running "
                                 "in this cell.")
            raise OrganError(f"No background process {process_id} in this workspace.")
        return process

    def _describe(self, process: _Process) -> str:
        end = process.finished or time.time()
        text = f"{process.state} for {end - process.started:.1f}s"
        if process.popen is not None:
            text += f" (pid {process.popen.pid})"
        if process.exit_code is not None:
            text += f", exit code {process.exit_code}"
        return text + f", {process.bytes} output bytes"

    def _status(self, context: InvocationContext, process_id: str) -> ToolResult:
        if process_id in ("all", "*"):
            with self._lock:
                mine = [item for item in self._processes.values()
                        if item.namespace == context.namespace]
            if not mine:
                return ToolResult("No background processes in this workspace.",
                                  evidence={"count": 0})
            lines = [f"{item.process_id} '{item.name}': {self._describe(item)}" for item in mine]
            return ToolResult("\n".join(lines), evidence={"count": len(mine)})
        process = self._own(context, process_id)
        return ToolResult(f"{process.process_id} '{process.name}': {self._describe(process)}",
                          evidence={"process_id": process.process_id, "state": process.state,
                                    "exit_code": process.exit_code})

    def _read(self, process: _Process, offset: int | None, max_chars: int) -> ToolResult:
        with process.lock:
            kept = min(process.bytes, OUTPUT_CAP)
            total = process.bytes
        start = max(0, kept - max_chars) if offset is None else min(int(offset), kept)
        try:
            with open(process.output, "rb") as handle:
                handle.seek(start)
                data = handle.read(min(max_chars, max(0, kept - start)))
        except OSError:
            data = b""
        end = start + len(data)
        text = data.decode("utf-8", "replace")
        note = (f"[bytes {start}-{end} of {total}; next offset {end}; {self._describe(process)}"
                + ("; output beyond the first 1 MiB is not kept" if total > OUTPUT_CAP else "")
                + "]")
        return ToolResult(f"{text}\n{note}" if text else note,
                          evidence={"process_id": process.process_id, "offset": start,
                                    "next_offset": end, "bytes": total})

    def _write(self, process: _Process, text: str, close: bool) -> ToolResult:
        popen = process.popen
        if popen is None or process.state != "running":
            raise OrganError(f"{process.process_id} is {process.state}; it takes no input.")
        try:
            if text:
                popen.stdin.write(text.encode("utf-8"))
                popen.stdin.flush()
            if close:
                popen.stdin.close()
        except (OSError, ValueError):
            raise OrganError(f"{process.process_id} no longer accepts input.") from None
        return ToolResult(f"Sent {len(text)} characters to {process.process_id}"
                          + (" and closed its input." if close else "."),
                          evidence={"process_id": process.process_id, "chars": len(text),
                                    "closed": close})

    # -- bookkeeping -------------------------------------------------------------------
    def _record(self, process: _Process, state: str, **fields: Any) -> None:
        try:
            self.store.update_process(process.process_id, state, output_bytes=process.bytes,
                                      **fields)
        except StateError:
            pass  # recovery settles a record left running

    def _forget(self, process: _Process) -> None:
        with self._lock:
            self._processes.pop(process.process_id, None)
        shutil.rmtree(process.directory, ignore_errors=True)
