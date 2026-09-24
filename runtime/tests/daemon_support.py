"""Support for daemon and scheduler specs (unit tier: real daemon code, fake Grail).

``ScriptedWorker`` replaces only the Grail process, exactly like ``test_cell_host``'s
``FakeWorker`` (it is one): it binds its grant with the daemon's real broker and
invokes real organs. Its turn is scripted by directives in the user message:
``[[tools]]`` answers with the advertised tool names, ``[[pause 2.5]]`` sleeps
without a tool, and ``[[<tool> {json}]]`` invokes that cell tool.

Run as a script, this module serves a daemon with scripted workers:
``python daemon_support.py <home> <workspace>`` (a real process that tests can
SIGKILL mid-occurrence).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance_support import cli_json, run_cli, wait_until  # noqa: E402
from test_cell_host import FakeWorker, done, sse  # noqa: E402

NAME = re.compile(r"\[\[(\w+)")


def directives(text: str):
    """Yield (name, JSON argument) for each [[name json]] in text; arguments may nest [[...]]."""
    decoder, index = json.JSONDecoder(), 0
    while (start := text.find("[[", index)) != -1:
        match = NAME.match(text, start)
        if match is None:
            index = start + 2
            continue
        argument, end = None, match.end()
        if text.startswith(" ", end):
            try:
                argument, end = decoder.raw_decode(text, end + 1)
            except ValueError:  # e.g. a directive truncated inside a schedule's echoed name
                index = end
                continue
        if not text.startswith("]]", end):
            index = end
            continue
        index = end + 2
        yield match.group(1), argument


def scripted(worker, request, grant):
    bound = worker.bind(grant)
    replies = []
    for name, argument in directives(request["user_input"]):
        if name == "tools":
            replies.append("tools=" + ",".join(sorted(tool["name"] for tool in bound["tools"])))
        elif name == "context":
            replies.append("context=" + bound["context"])
        elif name == "pause":
            time.sleep(float(argument))
            replies.append("paused")
        else:
            status, body = worker.invoke(grant, bound, name, argument or {})
            replies.append(f"{name}:{status}:{body.get('ok')}:"
                           f"{body.get('content', body.get('error'))}")
    yield sse({"type": "delta", "text": "working"})
    yield done(request, "\n".join(replies) or "ok")


class ScriptedWorker(FakeWorker):
    starts = 0

    def __init__(self, **options):
        super().__init__(scripted, **options)

    def start(self):
        ScriptedWorker.starts += 1
        started = {**super().start(), "start_seconds": 0.0, "pid": None}
        # Like unchanged Grail at startup: GitHub rejects a revoked token (credential specs).
        if self.credential is not None and self.credential.value.startswith("ghu_Revoked"):
            from brainstem_agent.worker import WorkerError
            raise WorkerError("The Copilot credential was rejected: Grail reports "
                              "invalid_credentials.")
        return started


def factory(**options):
    return ScriptedWorker(**options)


def daemon_env(base: dict, token_file: Path, scratch: Path) -> dict:
    empty = scratch / "no-brainstem"
    empty.mkdir(exist_ok=True, mode=0o700)
    return {**base, "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token_file),
            "BRAINSTEM_HOME": str(empty)}


def spawn_fake_daemon(home: Path, workspace: Path, env: dict) -> subprocess.Popen:
    """A real daemon process with scripted workers; waits until its record answers."""
    process = subprocess.Popen([sys.executable, str(HERE / "daemon_support.py"), str(home),
                                str(workspace)], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    from brainstem_agent import daemon

    def answering():
        record = daemon.read_record(home)
        return record is not None and record["pid"] == process.pid
    if not wait_until(lambda: answering() or process.poll() is not None, 30):
        process.kill()
        raise AssertionError("the fake daemon never became ready")
    if process.poll() is not None:
        raise AssertionError("the fake daemon exited: " + process.stderr.read()[-600:])
    return process


def cli(arguments, env, **options) -> dict:
    return cli_json(run_cli(arguments, env, **options))


def main() -> int:
    from brainstem_agent import daemon

    home, workspace = Path(sys.argv[1]), Path(sys.argv[2])
    cell = daemon.Daemon(home, workspace=workspace, environ=dict(os.environ),
                         worker_factory=factory)
    daemon.serve_foreground(cell, ready=lambda status: print(json.dumps(status), flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
