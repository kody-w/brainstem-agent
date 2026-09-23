"""Shell organ: commands run in the workspace inside Seatbelt with no network.

Writes are limited to the workspace and a private per-call directory; the
owner's home, the cell's state and temporary areas are unreadable except the
workspace itself. Whatever is left in the command's process group is killed
when the command exits, times out, is cancelled or its wait loop fails, and the
group is confirmed gone before the call returns. With a lifeline supervisor the
group also dies with its host. There is no unsandboxed fallback.

Limit: a descendant that calls ``setsid()`` leaves the process group, so the
group kill cannot reach it; it keeps running inside the same no-network
sandbox (no writes outside the workspace, no reads of the owner's data).
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import lifeline, sandbox
from .base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec, clip

_MAX_OUTPUT = 64_000
_SYSTEM_DENIED = ("/Users", "/Volumes", "/private/var/folders", "/private/tmp")
_SHELLS = ("/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash")


def owner_homes(environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    homes = {os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)}
    if environ and environ.get("HOME"):
        homes.add(os.path.realpath(environ["HOME"]))
    return tuple(Path(home) for home in sorted(homes))


class ShellOrgan:
    name = "shell"

    def __init__(self, *, run_root: Path, deny_read: Sequence[Path] = (),
                 environ: Mapping[str, str] | None = None,
                 supervisor: lifeline.Supervisor | None = None) -> None:
        self.run_root = Path(run_root)
        self.deny_read = tuple(Path(item) for item in deny_read)
        self.environ = dict(os.environ if environ is None else environ)
        self.supervisor = supervisor

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            "run_command", "Run a /bin/sh command in the workspace directory inside a sandbox "
            "with no network access. Returns the exit code and combined output.",
            {"type": "object", "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 8000},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600,
                                    "description": "Default 60."}},
             "required": ["command"]}, "shell.run", "external", timeout_seconds=620)]

    def context(self, context: BindContext) -> str | None:
        return "Shell commands run in the workspace inside a sandbox with no network access."

    def policy(self, workspace: Path, call_dir: Path) -> sandbox.SandboxPolicy:
        """The Seatbelt policy for one command: reads and writes only in the workspace and
        its private call directory, no network (Mach and signal rules come from sandbox)."""
        return sandbox.SandboxPolicy(
            read_denied=(*_SYSTEM_DENIED, *owner_homes(self.environ), *self.deny_read),
            readable=(workspace, call_dir),
            writable=(workspace, call_dir),
            network="none",
        )

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        if tool != "run_command":
            raise OrganError(f"Unknown shell tool {tool!r}.")
        context.check()
        if not sandbox.available(self.environ):
            raise OrganError("The shell sandbox (Seatbelt) is unavailable; refusing to run the "
                             "command without isolation.")
        timeout = min(float(arguments.get("timeout_seconds", 60)), max(1.0, context.remaining()))
        workspace = Path(os.path.realpath(context.workspace_root))
        call_dir = self.run_root / re.sub(r"[^A-Za-z0-9_-]", "_", context.call_id)[:64]
        home, tmp = call_dir / "home", call_dir / "tmp"
        for directory in (home, tmp):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        call_dir = Path(os.path.realpath(call_dir))
        try:
            argv = sandbox.wrap(["/bin/sh", "-c", arguments["command"]],
                                self.policy(workspace, call_dir),
                                environ=self.environ)
        except sandbox.SandboxUnavailable as error:
            raise OrganError(str(error)) from None
        env = {"HOME": str(call_dir / "home"), "TMPDIR": str(call_dir / "tmp"),
               "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               "PWD": str(workspace), "SHELL": "/bin/sh"}
        tracked = self._track(call_dir)
        started = time.monotonic()
        try:
            # Gated: the command runs only once its pid is recorded (see lifeline).
            process = lifeline.gated_popen(
                argv, tracked=tracked, expect=(sandbox.sandbox_exec_path(self.environ), *_SHELLS),
                cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True)
        except (OSError, lifeline.LifelineError) as error:
            if tracked is not None:
                tracked.finish()
            shutil.rmtree(call_dir, ignore_errors=True)
            raise OrganError("The command could not start: "
                             f"{getattr(error, 'strerror', None) or error}") from None
        chunks: list[bytes] = []
        size = [0]
        drained = threading.Event()

        def pump() -> None:
            try:
                while True:
                    data = process.stdout.read1(65536)
                    if not data:
                        return
                    if size[0] < _MAX_OUTPUT * 2:
                        chunks.append(data)
                    size[0] += len(data)
            except (OSError, ValueError):
                return
            finally:
                drained.set()

        timed_out = cancelled = False
        code = None
        try:
            threading.Thread(target=pump, daemon=True, name="shell-output").start()
            while True:
                try:
                    code = process.wait(0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if context.cancelled.is_set():
                    cancelled = True
                elif time.monotonic() - started >= timeout:
                    timed_out = True
                if timed_out or cancelled:
                    break
        finally:
            # Runs even when the wait loop raises (KeyboardInterrupt included): nothing in
            # the group, background jobs included, may outlive the call.
            stopped = lifeline.stop_group(process.pid, process=process, grace=0, timeout=5.0)
            drained.wait(5)
            process.stdout.close()
            shutil.rmtree(call_dir, ignore_errors=True)
            if tracked is not None and stopped["group_state"] != lifeline.ALIVE:
                tracked.finish()
        if code is None:
            code = stopped["exit_code"]
        output = b"".join(chunks).decode("utf-8", "replace")
        status = f"exit code {code}"
        if timed_out:
            status = f"timed out after {timeout:g}s (process group killed)"
        elif cancelled:
            status = "cancelled (process group killed)"
        text = clip(f"{status}\n{output}" if output else status, _MAX_OUTPUT)
        ok = code == 0 and not timed_out and not cancelled
        return ToolResult(text, ok=ok, evidence={
            "exit_code": code, "timed_out": timed_out, "cancelled": cancelled,
            "sandbox": sandbox.ENVIRONMENT, "seconds": round(time.monotonic() - started, 3),
            "output_bytes": size[0], "group_state": stopped["group_state"],
            "group_gone": stopped["group_gone"]})

    def _track(self, call_dir: Path) -> lifeline.Tracked | None:
        if self.supervisor is None:
            return None
        try:
            return self.supervisor.track("shell", call_dir)
        except (lifeline.LifelineError, OSError):
            shutil.rmtree(call_dir, ignore_errors=True)
            raise OrganError("The command could not be supervised; refusing to run it.") from None
