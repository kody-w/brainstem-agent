"""Support for the operable-cell specs (H1-H12): candidate releases, a fake launchctl, a
home with real turns (fake Grail), and time travel for retention.

Everything runs against temporary homes; nothing touches the owner's installed brainstem,
LaunchAgents, launchd domain or the cell's default home.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(RUNTIME))

from acceptance_support import private_dir, write_token_file  # noqa: E402
from test_cell_host import FakeWorker, done, sse  # noqa: E402

PACKAGE = RUNTIME / "brainstem_agent"
SCHEMA_ANCHOR = "_KNOWN_LAYOUTS = {1: (_V1_TABLES,), 2: (_V2_TABLES, _V2S_TABLES, _V2L_TABLES)}"


def copy_package(target_root: Path) -> Path:
    """A copy of this runtime's package (no caches) under ``target_root/brainstem_agent``."""
    destination = Path(target_root) / "brainstem_agent"
    shutil.copytree(PACKAGE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def write_manifest(root: Path) -> dict:
    """Regenerate a copied package's release manifest with that package's own code."""
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PYTHONPATH": str(root),
           "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "brainstem_agent.release", "--write"],
                            capture_output=True, text=True, env=env, timeout=120, cwd=str(root))
    if result.returncode != 0:
        raise AssertionError(f"manifest: {result.stderr[-800:]}")
    return json.loads((root / "brainstem_agent" / "data" / "release-manifest.json").read_text())


def make_candidate(root: Path, version: str, *, schema_extension: bool = False,
                   broken_serve: bool = False) -> Path:
    """A local candidate release (a directory holding ``brainstem_agent``): this runtime with
    another version, optionally a store schema extension (a new table, migrating this
    version's layout) or a ``serve`` that fails at startup."""
    root = Path(root)
    package = copy_package(root)
    init = package / "__init__.py"
    from brainstem_agent import __version__

    assert version != __version__, "a candidate needs another version than this runtime's"
    init.write_text(init.read_text().replace(f'__version__ = "{__version__}"',
                                             f'__version__ = "{version}"'))
    if schema_extension:
        state = package / "state.py"
        text = state.read_text()
        assert SCHEMA_ANCHOR in text, "the store's layout anchor moved; update ops_support"
        text = text.replace(SCHEMA_ANCHOR, (
            "_V2O_TABLES = frozenset(_SCHEMA)\n"
            "_SCHEMA[\"upgrade_probe\"] = \"CREATE TABLE upgrade_probe (probe_id INTEGER PRIMARY KEY)\"\n"
            "_KNOWN_LAYOUTS = {1: (_V1_TABLES,), 2: (_V2_TABLES, _V2S_TABLES, _V2L_TABLES, "
            "_V2O_TABLES)}"))
        state.write_text(text)
    if broken_serve:
        cli = package / "cli.py"
        text = cli.read_text()
        anchor = "def cmd_serve(arguments, environ) -> int:\n"
        assert anchor in text
        cli.write_text(text.replace(anchor, anchor + "    raise SystemExit(3)\n"))
    write_manifest(root)
    return root


_WHEEL_PROBE = ("import importlib.util, json\n"
                "try:\n"
                "    import setuptools\n"
                "    version = setuptools.__version__\n"
                "except Exception:\n"
                "    version = None\n"
                "print(json.dumps({'setuptools': version,\n"
                "                  'wheel': importlib.util.find_spec('wheel') is not None}))\n")


def offline_wheel_problem(python: Path) -> str | None:
    """Why ``pip install --no-index --no-build-isolation ./runtime`` cannot work in the venv of
    ``python`` (None when it can): setuptools builds wheels itself only from 70.1 on; an older
    one needs the ``wheel`` package, which a fresh venv does not have (python.org's 3.11
    installers bundle setuptools 65)."""
    result = subprocess.run([str(python), "-I", "-c", _WHEEL_PROBE], capture_output=True,
                            text=True, timeout=120)
    try:
        found = json.loads(result.stdout)
    except ValueError:
        return f"the venv's interpreter could not be probed (exit {result.returncode})"
    return wheel_problem(found.get("setuptools"), bool(found.get("wheel")))


def wheel_problem(setuptools_version: str | None, has_wheel: bool) -> str | None:
    zipapp = ("the offline path there is the zipapp (python -m brainstem_agent.release "
              "--zipapp brainstem-agent.pyz)")
    if has_wheel and setuptools_version:
        return None
    if setuptools_version is None:
        return f"the fresh venv has no setuptools, so pip cannot build offline; {zipapp}"
    numbers = [int(part) for part in re.findall(r"\d+", setuptools_version)[:2]]
    if tuple(numbers + [0] * (2 - len(numbers))) >= (70, 1):
        return None
    return (f"the fresh venv's setuptools {setuptools_version} builds no wheel by itself (70.1 "
            f"or newer does, or the wheel package) and the venv has no wheel package; {zipapp}")


def fake_launchctl(directory: Path) -> tuple[Path, Path]:
    """A launchctl stand-in that records its arguments (one line per call) and succeeds."""
    log = Path(directory) / "launchctl.log"
    script = Path(directory) / "launchctl"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\nexit 0\n")
    script.chmod(0o755)
    return script, log


def fake_brainstem(directory: Path, token: str) -> Path:
    """A temporary stand-in for the installed brainstem: <dir>/.brainstem/src/rapp_brainstem/
    .copilot_token (0600). Returns the brainstem home (for BRAINSTEM_HOME)."""
    home = Path(directory) / ".brainstem"
    folder = home / "src" / "rapp_brainstem"
    folder.mkdir(parents=True, mode=0o700)
    write_token_file(folder, token, name=".copilot_token")
    return home


def replace_token(brainstem_home: Path, token: str) -> None:
    """Replace the token the way a new sign-in does (a new file moved into place)."""
    folder = Path(brainstem_home) / "src" / "rapp_brainstem"
    fresh = write_token_file(folder, token, name=".copilot_token.new")
    os.replace(fresh, folder / ".copilot_token")


def tree_snapshot(root: Path) -> dict:
    """Every path under ``root`` with its size and modification time (for no-change checks)."""
    found = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            found[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return found


def age_rows(database: Path, table: str, column: str, seconds: float, where: str = "1=1",
             params: tuple = ()) -> int:
    """Move rows' timestamps back by ``seconds`` (retention specs)."""
    connection = sqlite3.connect(database)
    try:
        changed = connection.execute(
            f"UPDATE {table} SET {column} = {column} - ? WHERE {where}",
            (seconds, *params)).rowcount
        connection.commit()
        return changed
    finally:
        connection.close()


def tool_turn(*calls):
    """A fake Grail script: bind, invoke each (tool, arguments), then answer."""
    def script(worker, request, grant):
        bound = worker.bind(grant)
        results = []
        for tool, arguments in calls:
            status, body = worker.invoke(grant, bound, tool, arguments)
            results.append(f"{tool}:{status}:{body.get('ok')}")
        yield sse({"type": "agent", "logs": "; ".join(results)})
        yield done(request, "done: " + ", ".join(results))
    return script


def failing_start(message: str):
    """A worker class whose start fails like Grail rejecting the credential."""
    class Rejected(FakeWorker):
        def start(self):
            super().start()
            from brainstem_agent.worker import WorkerError
            raise WorkerError(message)
    return Rejected


MARKER_WORDS = "zebra-quartz-7731"


def seed_home(test, *, turns: int = 2):
    """A home with real state made through the product (fake Grail): turns that remember a
    fact and write a file, a profile fact, an owner skill, a schedule with one run in the
    inbox. Returns (home, workspace, host environ)."""
    import time

    from brainstem_agent import schedules
    from brainstem_agent.host import AgentHost

    home, workspace = private_dir(test), private_dir(test)
    token = write_token_file(private_dir(test))
    environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token),
               "BRAINSTEM_HOME": str(private_dir(test)), "HOME": str(home)}
    script = tool_turn(("remember", {"text": "The owner's favorite color is teal."}),
                       ("write_file", {"path": "notes/hello.txt", "content": "hi\n"}))
    host = AgentHost(home, workspace=workspace, environ=environ,
                     worker_factory=lambda **options: FakeWorker(script, **options))
    try:
        session = None
        for number in range(turns):
            result = host.chat(f"Turn {number}: remember my color. {MARKER_WORDS}",
                               session_id=session)
            assert result.ok, result.error
            session = result.session_id
        host.store.add_fact(host.profile_namespace, "The owner lives in Lisbon.")
        host.store.save_skill(host.namespace, "make-todo", description="Make a todo list",
                              when_to_use="When asked for a todo list",
                              steps=["List the tasks", "Write todo.md"], author="owner",
                              review="approved")
        record = schedules.create_schedule(
            host.store, namespace=host.namespace, workspace=str(host.workspace),
            prompt="Summarize notes/", when={"every_seconds": 3600},
            capabilities=["files.read"], allowed=host.known_capabilities(),
            default=host.turn_capabilities(), created_by="owner", now=time.time(),
            name="hourly summary")
        schedules.change_schedule(host.store, host.namespace, record["schedule_id"], "run_now",
                                  {}, allowed=host.known_capabilities(), now=time.time(),
                                  writer="owner")
        with host.exclusive():
            schedules.Scheduler(host.store, lambda occurrence: schedules.run_occurrence(
                host, occurrence)).tick(schedule_id=record["schedule_id"], limit=1)
    finally:
        host.close()
    return home, workspace, environ


__all__ = ["FakeWorker", "age_rows", "copy_package", "done", "fake_brainstem", "fake_launchctl",
           "failing_start", "make_candidate", "offline_wheel_problem", "private_dir",
           "replace_token", "seed_home", "sse", "tool_turn", "tree_snapshot", "wheel_problem",
           "write_manifest"]
