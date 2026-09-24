"""A captured, unchanged Grail worker: one private generation per start.

Each generation gets a byte-identical copy of the verified Grail source (runtime
state files live beside brainstem.py, so copies are never shared), a bridge-only
AGENTS_PATH, a private HOME/TMPDIR, an empty .env that stops python-dotenv's
upward search, a Seatbelt profile and a scrubbed environment. With a lifeline
supervisor the generation is recorded before its tree exists, so the worker
dies with its host and a dead host's tree is reaped. Stop kills the whole
process group, confirms it is gone (zombie-only groups included), re-verifies
every tracked file and deletes the tree.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

from . import lifeline, sandbox
from .credentials import GitHubCredential
from .grail import GrailSource, GrailSourceError, verify_tree
from .organs.shell import owner_homes

__all__ = ["BRIDGE_FILE", "PROBE_FILE", "SOUL_FILE", "GrailWorker", "WorkerConfig", "WorkerError"]

PACKAGE = Path(__file__).resolve().parent
BRIDGE_FILE = PACKAGE / "bridge" / "rapp_bridge_agent.py"
PROBE_FILE = PACKAGE / "bridge" / "rapp_probe_agent.py"
SOUL_FILE = PACKAGE / "data" / "soul.md"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SYSTEM_DENIED = ("/Users", "/Volumes", "/private/var/folders", "/private/tmp")
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
LOG_MAX_BYTES = 4 << 20


class WorkerError(RuntimeError):
    """The Grail worker could not start, answer or stop cleanly."""


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    home: Path
    source: GrailSource
    python: Path
    broker_url: str
    credential: GitHubCredential | None = field(default=None, repr=False)
    model: str = "auto"
    deny_read: tuple[Path, ...] = ()
    readable: tuple[Path, ...] = ()
    probe: Mapping | None = None
    startup_timeout: float = 90.0
    supervisor: lifeline.Supervisor | None = field(default=None, repr=False, compare=False)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _programs(python: Path) -> tuple[str, ...]:
    """Executables a worker leader runs: sandbox-exec, then the interpreter it execs.

    A macOS framework interpreter's ``bin/python3.x`` re-execs the binary inside
    ``Resources/Python.app``, so that binary is part of the worker's identity too.
    """
    real = Path(os.path.realpath(python))
    app = real.parent.parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return (sandbox.sandbox_exec_path(), str(real), *((str(app),) if app.is_file() else ()))


def _base_prefix(python: Path) -> Path | None:
    config = Path(python).parent.parent / "pyvenv.cfg"
    try:
        for line in config.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home" and value.strip():
                return Path(os.path.realpath(value.strip())).parent
    except OSError:
        return None
    return None


class GrailWorker:
    def __init__(self, config: WorkerConfig, *, register: Callable[[str, str], str]) -> None:
        self.config = config
        self.worker_id = config.worker_id
        self._register = register
        self.generation: str | None = None
        self.url: str | None = None
        self.pid: int | None = None
        self.pgid: int | None = None
        self.tree: Path | None = None
        self.log_path: Path | None = None
        self._process: subprocess.Popen | None = None
        self._pump: threading.Thread | None = None
        self._pumped = threading.Event()
        self._tracked: lifeline.Tracked | None = None
        self._secrets: list[str] = []
        self._stop_lock = threading.Lock()
        self._stopped: dict | None = None

    # -- start -------------------------------------------------------------------
    def _tree_path(self) -> Path:
        return Path(os.path.realpath(self.config.home)) / "workers" / self.worker_id / self.generation

    def _prepare_tree(self) -> dict:
        config = self.config
        tree = self._tree_path()
        tree.mkdir(parents=True, mode=0o700)
        self.tree = tree
        grail = tree / "rapp_brainstem"
        for relative in config.source.inventory:
            target = grail / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(config.source.root / relative, target)
            os.chmod(target, 0o444)
        untracked = verify_tree(grail, config.source.inventory, allow_extra=False)
        env_file = grail / ".env"
        env_file.write_text("")
        os.chmod(env_file, 0o600)
        agents = tree / "agents"
        agents.mkdir(mode=0o700)
        shutil.copyfile(BRIDGE_FILE, agents / BRIDGE_FILE.name)
        if config.probe is not None:
            shutil.copyfile(PROBE_FILE, agents / PROBE_FILE.name)
        for item in agents.iterdir():
            os.chmod(item, 0o400)
        os.chmod(agents, 0o500)
        shutil.copyfile(SOUL_FILE, tree / "soul.md")
        os.chmod(tree / "soul.md", 0o400)
        for name in ("home", "tmp"):
            (tree / name).mkdir(mode=0o700)
        return {"ok": True, "files": len(config.source.inventory), "untracked": untracked}

    def _policy(self, broker_port: int) -> sandbox.SandboxPolicy:
        config, tree = self.config, self.tree
        grail = tree / "rapp_brainstem"
        tracked = [grail / relative for relative in config.source.inventory]
        venv = Path(config.python).parent.parent
        readable = [tree, venv, *config.readable]
        base = _base_prefix(config.python)
        if base is not None:
            readable.append(base)
        return sandbox.SandboxPolicy(
            read_denied=(*_SYSTEM_DENIED, *owner_homes(os.environ), Path(config.home),
                         *config.deny_read),
            readable=tuple(readable),
            writable=(grail, tree / "home", tree / "tmp"),
            write_denied=(*tracked, grail / "agents", grail / "tests", grail / ".vscode",
                          grail / ".env", grail / ".brainstem_model", tree / "agents"),
            network="outbound",
            loopback_ports=(broker_port,),
        )

    def start(self) -> dict:
        if self._process is not None or self._stopped is not None:
            raise WorkerError("A worker generation starts only once.")
        started = time.monotonic()
        config = self.config
        self.generation = "g" + secrets.token_hex(6)
        key = self._register(self.worker_id, self.generation)
        self._secrets = [value for value in (key, config.credential.value if config.credential
                                             else "") if value]
        if config.credential and len(config.credential.value) >= 8:
            self._secrets.append(config.credential.value[:8])
        # Preparation and launch are atomic with respect to stop(): a concurrent stop
        # (cancellation) either finds nothing launched or kills what was launched.
        problem, phase = None, "prepare"
        with self._stop_lock:
            if self._stopped is not None:
                raise WorkerError("The worker was stopped before it started.")
            try:
                if config.supervisor is not None:
                    self._tracked = config.supervisor.track("worker", self._tree_path())
                integrity = self._prepare_tree()
                phase = "launch"
                port = self._launch(key)
            except (OSError, GrailSourceError, sandbox.SandboxUnavailable,
                    lifeline.LifelineError) as error:
                problem = error
        if problem is not None:
            self.stop()
            if phase == "prepare":
                raise WorkerError(f"The worker copy of Grail could not be prepared: {problem}") \
                    from None
            raise WorkerError(f"The Grail worker could not be launched: {problem}") from None
        self._pump = threading.Thread(target=self._copy_log, daemon=True, name="grail-log")
        self._pump.start()
        self.url = f"http://127.0.0.1:{port}"
        health = self._wait_ready(started)
        return {"worker_id": self.worker_id, "generation": self.generation, "pid": self.pid,
                "start_seconds": round(time.monotonic() - started, 3),
                "integrity_before": integrity, "health": health.get("status"),
                "model": health.get("model")}

    def _launch(self, key: str) -> int:
        config, tree = self.config, self.tree
        port = _free_port()
        broker_port = int(config.broker_url.rsplit(":", 1)[-1])
        env = {
            "HOME": str(tree / "home"), "TMPDIR": str(tree / "tmp"), "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8", "PORT": str(port), "AGENTS_PATH": str(tree / "agents"),
            "SOUL_PATH": str(tree / "soul.md"), "GITHUB_MODEL": config.model,
            "GITHUB_TOKEN": config.credential.value if config.credential else "",
            "BRAINSTEM_LAN_MODE": "false", "VOICE_MODE": "false",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
            "BRAINSTEM_AGENT_BROKER_URL": config.broker_url,
            "BRAINSTEM_AGENT_WORKER_ID": self.worker_id,
            "BRAINSTEM_AGENT_WORKER_GENERATION": self.generation,
            "BRAINSTEM_AGENT_WORKER_KEY": key,
        }
        if config.probe is not None:
            env["BRAINSTEM_AGENT_PROBE"] = json.dumps(dict(config.probe))
            env["BRAINSTEM_AGENT_PROBE_NONCE"] = str(config.probe.get("nonce", ""))
        logs = Path(config.home) / "logs" / "workers"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_path = logs / f"{self.worker_id}-{self.generation}.log"
        argv = sandbox.wrap([str(config.python), "brainstem.py"], self._policy(broker_port),
                            profile_path=tree / "sandbox.sb")
        # Gated: Grail starts only once its pid is recorded, so no dead host leaves it orphaned.
        self._process = lifeline.gated_popen(
            argv, tracked=self._tracked, expect=_programs(config.python),
            cwd=tree / "rapp_brainstem", env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.pid = self.pgid = self._process.pid
        return port

    def _redact(self, text: str) -> str:
        for value in self._secrets:
            text = text.replace(value, "[REDACTED]")
        return _TOKEN_SHAPES.sub(r"\1[REDACTED]", text)

    def _copy_log(self) -> None:
        """Copy the worker's output into its log (redacted), rotating once past
        ``LOG_MAX_BYTES`` so a long-lived warm worker's log stays bounded."""
        def open_log():
            descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                 | os.O_NOFOLLOW, 0o600)
            return os.fdopen(descriptor, "w", encoding="utf-8")

        log, written = None, 0
        try:
            log = open_log()
            for raw in iter(self._process.stdout.readline, b""):
                line = self._redact(raw.decode("utf-8", "replace"))
                if written + len(line) > LOG_MAX_BYTES:
                    log.close()
                    os.replace(self.log_path, self.log_path.with_name(self.log_path.name + ".1"))
                    log, written = open_log(), 0
                log.write(line)
                log.flush()
                written += len(line)
        finally:
            if log is not None:
                log.close()
            self._pumped.set()

    def log_tail(self, lines: int = 3) -> str:
        try:
            return " | ".join(self.log_path.read_text().strip().splitlines()[-lines:])[:600]
        except (OSError, AttributeError):
            return ""

    def _wait_ready(self, started: float) -> dict:
        deadline = started + self.config.startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                tail = self.log_tail()
                self.stop()
                raise WorkerError(f"The Grail worker exited during startup. {tail}".strip())
            try:
                health = self.health()
                break
            except (OSError, WorkerError, ValueError):
                time.sleep(0.2)
        else:
            self.stop()
            raise WorkerError("The Grail worker did not become ready in time.")
        if self.config.credential is not None:
            if health.get("status") != "ok":
                self.stop()
                raise WorkerError("The Copilot credential was rejected: Grail reports "
                                  f"{health.get('auth_error') or health.get('status')}.")
            if health.get("copilot") == "no_access":
                self.stop()
                raise WorkerError("The Copilot credential has no Copilot access.")
        return health

    # -- requests ----------------------------------------------------------------
    def health(self, headers: Mapping[str, str] | None = None) -> dict:
        request = urllib.request.Request(self.url + "/health", headers=dict(headers or {}))
        with _OPENER.open(request, timeout=30) as response:
            if response.status != 200:
                raise WorkerError(f"Grail /health returned {response.status}")
            document = json.loads(response.read())
        if not isinstance(document, dict):
            raise WorkerError("Grail /health did not return an object")
        return document

    def stream_chat(self, request: Mapping, grant: str, *,
                    read_timeout: float = 300.0) -> Iterator[bytes]:
        body = json.dumps(dict(request)).encode("utf-8")
        http = urllib.request.Request(self.url + "/chat/stream", data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "text/event-stream",
            "X-Brainstem-Agent-Grant": grant})
        try:
            response = _OPENER.open(http, timeout=read_timeout)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(65536)).get("error", "")
            except (ValueError, AttributeError):
                detail = ""
            raise WorkerError(self._redact(f"Grail refused the turn ({error.code}): {detail}")[:500])
        with response:
            while True:
                line = response.readline(1 << 20)
                if not line:
                    return
                yield line

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._stopped is None

    def verify_integrity(self) -> list[str]:
        return verify_tree(self.tree / "rapp_brainstem", self.config.source.inventory,
                           allow_extra=True)

    # -- stop --------------------------------------------------------------------
    def stop(self, *, grace: float = 2.0, timeout: float = 5.0) -> dict:
        with self._stop_lock:
            if self._stopped is not None:
                return self._stopped
            started = time.monotonic()
            exit_code, state = None, lifeline.GONE
            process = self._process
            if process is not None:
                result = lifeline.stop_group(self.pgid, process=process, grace=grace,
                                             timeout=timeout)
                exit_code, state = result["exit_code"], result["group_state"]
            seconds = round(time.monotonic() - started, 3)
            if self._pump is not None:
                self._pumped.wait(2)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            after = {"ok": False, "files": len(self.config.source.inventory), "untracked": [],
                     "detail": "no worker tree"}
            if self.tree is not None and self.tree.exists():
                try:
                    after = {"ok": True, "files": len(self.config.source.inventory),
                             "untracked": self.verify_integrity()}
                except GrailSourceError as error:
                    after.update(detail=str(error))
                lifeline.remove_tree(self.tree)
                try:  # workers/<id> holds only this generation (helpers each get their own)
                    self.tree.parent.rmdir()
                except OSError:
                    pass
            if self._tracked is not None and state != lifeline.ALIVE:
                self._tracked.finish()
            self._stopped = {"seconds": seconds, "exit_code": exit_code,
                             "group_gone": state == lifeline.GONE, "group_state": state,
                             "integrity_after": after}
            return self._stopped
