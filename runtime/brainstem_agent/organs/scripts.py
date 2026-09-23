"""Script organ: many tool operations in one Grail tool round.

``run_script`` (``scripts.run``) runs a short Python script (standard library only) with
this interpreter in isolated mode inside Seatbelt: no network, the workspace readable but
not writable, writes only in a private scratch directory that is deleted afterwards. The
script calls cell tools through a pipe to the host (``call(tool, **arguments)``): each
inner call is made by the broker on behalf of the ``run_script`` call, so it gets its own
receipt (``<call_id>.<n>``) and passes the same grant and capability checks as a call
from the model, and only ``INNER_TOOLS`` are allowed. Bounded: inner calls, seconds,
script size and output. The process group is supervised by the lifeline (it dies with
its host) and killed on timeout, cancellation or return.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import lifeline, sandbox
from ..paths import holds
from .base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec, clip
from .shell import owner_homes

INNER_TOOLS = frozenset({"read_file", "write_file", "list_files", "recall", "skill_view",
                         "session_search"})
MAX_INNER_CALLS = 50
MAX_OUTPUT = 64_000
_SYSTEM_DENIED = ("/Users", "/Volumes", "/private/var/folders", "/private/tmp")
_BOOT = r'''
import json, os, sys
_requests = os.fdopen(int(sys.argv[1]), "w", encoding="utf-8")
_answers = os.fdopen(int(sys.argv[2]), "r", encoding="utf-8")


class ToolError(Exception):
    """A cell tool refused the call or failed."""


def _call(tool, arguments):
    _requests.write(json.dumps({"tool": tool, "arguments": arguments}) + "\n")
    _requests.flush()
    line = _answers.readline()
    if not line:
        raise ToolError("the cell closed the tool channel")
    answer = json.loads(line)
    if not answer.get("ok"):
        raise ToolError(answer.get("content") or "the tool failed")
    return answer


def call(tool, **arguments):
    """Call a cell tool; returns its text result, raises ToolError when it fails."""
    return _call(tool, arguments).get("content") or ""


def read_text(path):
    """A workspace file's exact text, through the read_file tool (without its header line).
    Raises ToolError when the tool could return only part of the file (too large, or not
    UTF-8 text): use call("read_file", path=...) for a preview instead."""
    answer = _call("read_file", {"path": path})
    result = answer.get("content") or ""
    if answer.get("exact") is not True:
        raise ToolError(f"read_text: read_file returned only part of {path} (too large or "
                        "not UTF-8 text); call('read_file', path=...) shows a preview")
    return result.split(":\n", 1)[1] if ":\n" in result else result


def write_text(path, content):
    """Create or replace a workspace file through the write_file tool."""
    return call("write_file", path=path, content=str(content))


with open(sys.argv[3], encoding="utf-8") as handle:
    _source = handle.read()
sys.argv = ["script.py"]
_globals = {"__name__": "__main__", "call": call, "ToolError": ToolError,
            "read_text": read_text, "write_text": write_text}
exec(compile(_source, "script.py", "exec"), _globals)
'''


def _programs(python: str) -> tuple[str, ...]:
    real = os.path.realpath(python)
    app = Path(real).parent.parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return (real, *((str(app),) if app.is_file() else ()))


def standard_library(python: str) -> tuple[Path, ...]:
    """The directories holding ``python``'s standard library (``lib-dynload`` included), for
    this process's own interpreter or a venv of it; empty for any other interpreter."""
    if os.path.realpath(python) != os.path.realpath(sys.executable):
        return ()
    found = {sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")}
    return tuple(sorted(Path(os.path.realpath(item)) for item in found
                        if item and os.path.isabs(item)))


class ScriptOrgan:
    name = "scripts"

    def __init__(self, *, run_root: Path, deny_read: Sequence[Path] = (),
                 environ: Mapping[str, str] | None = None,
                 supervisor: lifeline.Supervisor | None = None,
                 crash: Callable[[str], None] = lambda _name: None,
                 python: str | None = None) -> None:
        self.run_root = Path(run_root)
        self.deny_read = tuple(Path(item) for item in deny_read)
        self.environ = dict(os.environ if environ is None else environ)
        self.supervisor = supervisor
        self.crash = crash
        self.python = python or sys.executable
        self.library = standard_library(self.python)

    def tools(self) -> list[ToolSpec]:
        names = ", ".join(sorted(INNER_TOOLS))
        return [ToolSpec(
            "run_script", "Run a short Python 3 script (standard library only) to do many "
            "operations in one step, for example a loop over files. It runs in a sandbox with "
            "no network; the workspace is its working directory and is read-only for the "
            "script, so change files through tools: call(tool, **arguments) returns a cell "
            "tool's text result (raises ToolError on failure); helpers read_text(path) and "
            f"write_text(path, content). Tools available inside: {names}. Print what you want "
            f"returned. At most {MAX_INNER_CALLS} tool calls; default 30 s.",
            {"type": "object", "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": 20_000,
                         "description": "The Python source."},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120,
                                    "description": "Default 30."}},
             "required": ["code"]}, "scripts.run", "external", timeout_seconds=150)]

    def context(self, context: BindContext) -> str | None:
        return None

    def policy(self, workspace: Path, call_dir: Path) -> sandbox.SandboxPolicy:
        """No network; the workspace readable (not writable); writes only in the call dir.

        An interpreter installed inside a denied area (a pyenv or uv Python under the home)
        could not even start, so its standard library directory is re-allowed for reading;
        never one that is or holds a denied area itself."""
        denied = (*_SYSTEM_DENIED, *owner_homes(self.environ), *self.deny_read)
        library = tuple(path for path in self.library
                        if any(holds(area, path) for area in denied)
                        and not any(holds(path, area) for area in denied))
        return sandbox.SandboxPolicy(
            read_denied=denied, readable=(workspace, call_dir, *library), writable=(call_dir,),
            network="none")

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        if tool != "run_script":
            raise OrganError(f"Unknown script tool {tool!r}.")
        context.check()
        if context.call_tool is None:
            raise OrganError("Scripts need the cell's broker to call tools.")
        if not sandbox.available(self.environ):
            raise OrganError("The script sandbox (Seatbelt) is unavailable; refusing to run the "
                             "script without isolation.")
        timeout = min(float(arguments.get("timeout_seconds") or 30), max(1.0,
                                                                         context.remaining()))
        workspace = Path(os.path.realpath(context.workspace_root))
        call_dir = self.run_root / re.sub(r"[^A-Za-z0-9_-]", "_", context.call_id)[:64]
        for directory in (call_dir / "home", call_dir / "tmp"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        call_dir = Path(os.path.realpath(call_dir))
        (call_dir / "script.py").write_text(arguments["code"], encoding="utf-8")
        requests_read, requests_write = os.pipe()
        answers_read, answers_write = os.pipe()
        child_ends = (requests_write, answers_read)
        tracked = None
        try:
            argv = sandbox.wrap([self.python, "-I", "-S", "-c", _BOOT, str(requests_write),
                                 str(answers_read), str(call_dir / "script.py")],
                                self.policy(workspace, call_dir), environ=self.environ)
            if self.supervisor is not None:
                tracked = self.supervisor.track("script", call_dir)
            env = {"HOME": str(call_dir / "home"), "TMPDIR": str(call_dir / "tmp"),
                   "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONIOENCODING": "utf-8"}
            process = lifeline.gated_popen(
                argv, tracked=tracked,
                expect=(sandbox.sandbox_exec_path(self.environ), *_programs(self.python)),
                cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True, pass_fds=child_ends)
        except (OSError, sandbox.SandboxUnavailable, lifeline.LifelineError) as error:
            for descriptor in (requests_read, requests_write, answers_read, answers_write):
                os.close(descriptor)
            if tracked is not None:
                tracked.finish()
            shutil.rmtree(call_dir, ignore_errors=True)
            raise OrganError(f"The script could not start: {error}") from None
        for descriptor in child_ends:
            os.close(descriptor)
        counts = {"calls": 0, "ok": 0, "failed": 0, "refused": 0}
        requests = os.fdopen(requests_read, "r", encoding="utf-8", errors="replace")
        answers = os.fdopen(answers_write, "w", encoding="utf-8")

        def serve() -> None:
            """Answer the script's tool calls, one at a time, in order."""
            try:
                for line in requests:
                    if len(line) > 2_000_000:
                        answer = {"ok": False, "content": "The tool call is too large."}
                    elif counts["calls"] >= MAX_INNER_CALLS:
                        counts["refused"] += 1
                        answer = {"ok": False, "content": f"The script used all {MAX_INNER_CALLS}"
                                                          " tool calls it may make."}
                    else:
                        try:
                            request = json.loads(line)
                            name, given = request["tool"], request.get("arguments") or {}
                            if not isinstance(name, str) or not isinstance(given, dict):
                                raise ValueError
                        except (ValueError, KeyError, TypeError):
                            answer = {"ok": False, "content": "Malformed tool call."}
                        else:
                            counts["calls"] += 1
                            answer = context.call_tool(name, given, allowed=INNER_TOOLS)
                            counts["ok" if answer.get("ok") else "failed"] += 1
                            self.crash("script.inner")
                    answers.write(json.dumps(answer) + "\n")
                    answers.flush()
            except (OSError, ValueError):
                return

        chunks: list[bytes] = []
        size = [0]

        def pump() -> None:
            try:
                while True:
                    data = process.stdout.read1(65536)
                    if not data:
                        return
                    if size[0] < MAX_OUTPUT * 2:
                        chunks.append(data)
                    size[0] += len(data)
            except (OSError, ValueError):
                return

        server = threading.Thread(target=serve, daemon=True, name="script-calls")
        reader = threading.Thread(target=pump, daemon=True, name="script-output")
        server.start()
        reader.start()
        started = time.monotonic()
        timed_out = cancelled = False
        code = None
        try:
            while True:
                try:
                    code = process.wait(0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if context.cancelled.is_set():
                    cancelled = True
                    break
                if time.monotonic() - started >= timeout:
                    timed_out = True
                    break
        finally:
            stopped = lifeline.stop_group(process.pid, process=process, grace=0, timeout=5.0)
            reader.join(5)
            process.stdout.close()
            try:
                answers.close()
            except OSError:
                pass
            server.join(5)
            try:
                requests.close()
            except OSError:
                pass
            shutil.rmtree(call_dir, ignore_errors=True)
            if tracked is not None and stopped["group_state"] != lifeline.ALIVE:
                tracked.finish()
        if code is None:
            code = stopped["exit_code"]
        output = b"".join(chunks).decode("utf-8", "replace")
        status = f"exit code {code}"
        if timed_out:
            status = f"timed out after {timeout:g}s (script stopped)"
        elif cancelled:
            status = "cancelled (script stopped)"
        summary = (f"[{counts['calls']} tool call(s) through the cell: {counts['ok']} ok, "
                   f"{counts['failed']} failed" + (f", {counts['refused']} over the limit"
                                                   if counts["refused"] else "") + "]")
        text = clip(f"{status}\n{output}\n{summary}" if output else f"{status}\n{summary}",
                    MAX_OUTPUT)
        ok = code == 0 and not timed_out and not cancelled
        return ToolResult(text, ok=ok, evidence={
            "exit_code": code, "timed_out": timed_out, "cancelled": cancelled,
            "inner_calls": counts["calls"], "inner_ok": counts["ok"],
            "inner_failed": counts["failed"], "inner_refused": counts["refused"],
            "sandbox": sandbox.ENVIRONMENT, "seconds": round(time.monotonic() - started, 3),
            "output_bytes": size[0], "group_state": stopped["group_state"]})
