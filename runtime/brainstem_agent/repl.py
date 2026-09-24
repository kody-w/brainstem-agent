"""Brainstem Agent's interactive terminal: ``brainstem-agent`` (no command) or ``repl``.

A line REPL on the same engine as every other surface. Each turn goes through the daemon
when one runs for the home (``POST /v1/requests`` and its event stream, the same routes
the web companion uses), otherwise in-process on a host this session keeps (its worker
stays warm between turns). The answer streams as it arrives; tools, steps and helpers
print one line each; the final state and answer are the engine's (never the streamed
text). Ctrl-C cancels the running turn and keeps the session; Ctrl-D or ``/exit`` quits.

Untrusted text (answers, tool output, names) is printed with control characters made
visible (``\\x1b``), so nothing the model or a web page writes can drive the terminal
(clipboard writes, hyperlinks, title changes, screen clears). Input history lives in
``<home>/state/repl_history`` (0600, never through a symlink; credential-shaped lines are
not saved).

``--json`` is a line protocol for agents (``brainstem-agent-repl/1``). Each stdin line is
plain text (as typed) or a JSON object: ``{"op": "chat", "text": ...}``,
``{"op": "command", "text": "/sessions"}``, ``{"op": "cancel"}`` (cancels the running
turn; read while it runs) or ``{"op": "exit"}``. Each stdout line is one JSON object:
``ready`` (protocol, mode, session, commands), ``delta`` (answer text; ``t`` is the
daemon's event time, when there is one), ``event`` (a
progress event), ``result`` (the turn's full result), ``command`` (a slash command's
data), ``notice``, ``error`` and finally ``bye``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from . import daemon, streaming, views
from .credentials import credential_kinds

__all__ = ["COMMANDS", "PROTOCOL", "Repl", "safe_text"]

PROTOCOL = "brainstem-agent-repl/1"
COMMANDS = {
    "/help": "show this help",
    "/new": "start a new session",
    "/sessions": "list this workspace's sessions",
    "/resume": "/resume <session id>: continue a session (its history is resent)",
    "/history": "show the current session's turns",
    "/skills": "list skills and their review state",
    "/memory": "list workspace and profile facts",
    "/schedules": "list schedules",
    "/inbox": "results of scheduled runs",
    "/status": "daemon, worker, session and workspace",
    "/stop": "cancel the turn the daemon is running now (from any surface)",
    "/exit": "quit (also Ctrl-D)",
}
# C0 and C1 controls (but tab and newline), DEL, line and paragraph separators, and every
# Unicode Bidi_Control character (ALM, LRM, RLM, embeddings, overrides, isolates).
_BIDI_CONTROLS = "\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"
_UNSAFE = re.compile(f"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029{_BIDI_CONTROLS}]")
HISTORY_LINES = 1000


def safe_text(text: Any) -> str:
    """Untrusted text for a terminal: controls, C1 and bidi controls made visible."""
    return _UNSAFE.sub(lambda match: (f"\\x{ord(match.group()):02x}" if ord(match.group()) < 256
                                      else f"\\u{ord(match.group()):04x}"), str(text))


class History:
    """The REPL's input history in the cell home (append-only, owner-only)."""

    def __init__(self, home: Path) -> None:
        self.path = home / "state" / "repl_history"

    def _open(self, flags: int) -> int | None:
        state = self.path.parent
        try:
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = os.lstat(state)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            descriptor = os.open(self.path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            return None
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(descriptor)
            return None
        return descriptor

    def load(self) -> list[str]:
        """The newest lines, credential-shaped ones left out (whoever wrote the file)."""
        descriptor = self._open(os.O_RDONLY | os.O_CREAT)
        if descriptor is None:
            return []
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\n") for line in handle.readlines()[-HISTORY_LINES:]]
        return [line for line in lines if line.strip() and not credential_kinds(line)]

    @staticmethod
    def keeps(line: str) -> bool:
        """Whether a typed line may be remembered at all (in memory or on disk)."""
        return bool(line.strip()) and not credential_kinds(line)

    def add(self, line: str) -> bool:
        line = line.replace("\n", " ").strip()
        if not self.keeps(line):
            return False
        descriptor = self._open(os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        if descriptor is None:
            return False
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line[:4000] + "\n")
        return True


class _Local:
    """In-process turns on a host this REPL keeps (no daemon)."""

    mode = "in-process"

    def __init__(self, home: Path, environ: dict, workspace: str | None,
                 worker_factory: Callable[..., Any] | None) -> None:
        from .host import AgentHost

        self.tee = streaming.StreamTee()
        holder: dict = {}
        self.host = AgentHost(home, workspace=Path(workspace) if workspace else None,
                              environ=environ,
                              worker_factory=self.tee.wrap(worker_factory,
                                                           lambda: holder["host"]._grail_worker))
        holder["host"] = self.host

    def turn(self, message: str, session_id: str | None, capabilities, on_event,
             cancel: threading.Event) -> dict:
        from .host import HostCancelled, TurnResult

        sink = streaming.follow(self.tee, on_event)
        try:
            with self.host.exclusive(cancel=cancel):
                result = self.host.chat(message, session_id=session_id, capabilities=capabilities,
                                        cancel_event=cancel, progress=sink)
        except HostCancelled as error:
            result = TurnResult(False, "cancelled", False, None, session_id, None, str(error),
                                {"worker": None, "grail_calls": 0})
        finally:
            sink.close()
        return result.to_json()

    def query(self, name: str, **options) -> dict:
        host = self.host
        if name == "sessions":
            return views.sessions(host, active=views.live_turns(host))
        if name == "session":
            return views.session(host, options["session_id"], active=views.live_turns(host))
        if name == "skills":
            return views.skills(host)
        if name == "memory":
            return views.memory(host)
        if name == "schedules":
            return views.schedules(host)
        if name == "inbox":
            return views.inbox(host)
        if name == "status":
            return {"running": False, "mode": self.mode, "workspace": str(host.workspace),
                    "workers": [] if host.worker_status() is None else [host.worker_status()]}
        if name == "stop":
            return {"cancelled": False, "reason": "no daemon: a turn here stops with Ctrl-C"}
        raise ValueError(name)

    def close(self) -> None:
        self.host.close()


class _Remote:
    """Turns and queries through the daemon's documented routes."""

    mode = "daemon"
    ROUTES = {"sessions": "/v1/sessions", "skills": "/v1/skills", "memory": "/v1/memory",
              "schedules": "/v1/schedules", "inbox": "/v1/inbox", "status": "/v1/status"}

    def __init__(self, client: daemon.Client, workspace: str | None) -> None:
        self.client, self.workspace = client, workspace

    def turn(self, message: str, session_id: str | None, capabilities, on_event,
             cancel: threading.Event) -> dict:
        body = {"message": message, "session_id": session_id, "capabilities": capabilities}
        if self.workspace:
            body["workspace"] = self.workspace
        started = self.client.call("POST", "/v1/requests", body, timeout=30)
        request_id = started["request_id"]
        finished = threading.Event()

        def watch() -> None:
            while not finished.is_set():
                if cancel.wait(0.05):
                    try:
                        self.client.call("POST", "/v1/cancel", {"request_id": request_id},
                                         timeout=10)
                    except daemon.DaemonError:
                        pass
                    return

        threading.Thread(target=watch, daemon=True, name="brainstem-agent-repl-cancel").start()
        seq, result = 0, None
        try:
            for _attempt in range(20):
                try:
                    for event in self.client.stream(f"/v1/requests/{request_id}/events?after={seq}"):
                        seq = max(seq, int(event.get("seq") or 0))
                        if event.get("event") == "request.finished":
                            result = event.get("result")
                        else:
                            on_event(event)
                except daemon.DaemonUnavailable:
                    raise
                except daemon.DaemonError:
                    time.sleep(0.5)  # a broken stream: re-attach after the last event seen
                if result is not None:
                    return result
            raise daemon.DaemonError("The daemon's event stream ended without a result.")
        finally:
            finished.set()

    def query(self, name: str, **options) -> dict:
        if name == "session":
            return self.client.call("GET", f"/v1/sessions/{options['session_id']}", timeout=30)
        if name == "stop":
            return self.client.call("POST", "/v1/cancel", {"active": True}, timeout=30)
        answer = self.client.call("GET", self.ROUTES[name], timeout=30)
        if name == "status":
            answer = {**answer, "mode": self.mode}
        return answer

    def close(self) -> None:
        return None


class Repl:
    def __init__(self, environ: dict, *, session: str | None = None, json_mode: bool = False,
                 workspace: str | None = None, capabilities: list[str] | None = None,
                 quiet: bool = False, stdin: TextIO | None = None, stdout: TextIO | None = None,
                 stderr: TextIO | None = None,
                 worker_factory: Callable[..., Any] | None = None) -> None:
        self.environ = environ
        self.home = Path(os.path.realpath(environ.get("BRAINSTEM_AGENT_HOME")
                                          or os.path.expanduser("~/.brainstem-agent")))
        self.session = session
        self.json = json_mode
        self.workspace = workspace
        self.capabilities = capabilities
        self.quiet = quiet
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.worker_factory = worker_factory
        self.backend: _Local | _Remote | None = None
        self.history = History(self.home)
        self.cancel = threading.Event()
        self.busy = threading.Event()
        self.lines: queue.Queue = queue.Queue()
        self._midline = False
        self._lock = threading.Lock()
        self._streamed = ""
        self._exit_after_turn = False
        self._rl: Any = None  # the readline module, on a terminal

    # -- output -------------------------------------------------------------------------
    def emit(self, document: dict) -> None:
        with self._lock:
            print(json.dumps(document), file=self.stdout, flush=True)

    def say(self, text: str, *, error: bool = False) -> None:
        if self.json:
            self.emit({"type": "error" if error else "notice", "text": text})
            return
        with self._lock:
            if self._midline:
                print(file=self.stdout, flush=True)
                self._midline = False
            print(text, file=self.stderr if error else self.stdout, flush=True)

    def progress(self, line: str) -> None:
        if self.quiet:
            return
        with self._lock:
            if self._midline:
                print(file=self.stdout, flush=True)
                self._midline = False
            print("· " + safe_text(line), file=self.stderr, flush=True)

    # -- backend ------------------------------------------------------------------------
    def backend_for_turn(self) -> _Local | _Remote:
        client = daemon.connect(self.home)
        if client is not None:
            if not isinstance(self.backend, _Remote) or self.backend.client.pid != client.pid:
                if isinstance(self.backend, _Local):
                    self.backend.close()
                self.backend = _Remote(client, self.workspace)
                self.say(f"(through the daemon, pid {client.pid})")
            return self.backend
        if not isinstance(self.backend, _Local):
            self.backend = _Local(self.home, self.environ, self.workspace, self.worker_factory)
            self.say("(no daemon: turns run in this process)")
        return self.backend

    # -- turns --------------------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        from .cli import progress_line

        kind = event.get("event")
        if kind == "segment.started" and event.get("segment", 1) > 1 and not event.get("child"):
            self._streamed = ""  # a continuation: its answer is the one that counts
        if kind == "answer.delta":
            self._streamed += str(event.get("text", ""))
            if self.json:
                self.emit({"type": "delta", "text": event.get("text", ""), "t": event.get("t")})
            else:
                with self._lock:
                    self.stdout.write(safe_text(event.get("text", "")))
                    self.stdout.flush()
                    self._midline = not str(event.get("text", "")).endswith("\n")
            return
        if kind == "turn.started" and event.get("session_id") and not event.get("child"):
            self.session = event["session_id"]
        if self.json:
            self.emit({"type": "event", "event": event})
            return
        line = progress_line(event) if kind != "turn.finished" else None
        if kind in ("request.queued", "request.running", "turn.started"):
            line = {"request.queued": "queued (waiting for the worker)",
                    "request.running": "running"}.get(kind)
        if line:
            self.progress(line)

    def run_turn(self, message: str) -> dict | None:
        self.cancel.clear()
        self._streamed = ""
        outcome: dict = {}
        finished = threading.Event()

        def work() -> None:
            try:
                backend = self.backend_for_turn()
                outcome["result"] = backend.turn(message, self.session, self.capabilities,
                                                 self.on_event, self.cancel)
            except BaseException as error:  # reported on the main thread
                outcome["error"] = error
            finally:
                finished.set()

        self.busy.set()
        threading.Thread(target=work, daemon=True, name="brainstem-agent-repl-turn").start()
        try:
            while not finished.wait(0.05):
                pass
        finally:
            self.busy.clear()
        if "error" in outcome:
            self.say(f"Brainstem Agent: {safe_text(outcome['error'])}", error=True)
            return None
        result = outcome["result"]
        if result.get("session_id"):
            self.session = result["session_id"]
        self.report(result)
        return result

    def report(self, result: dict) -> None:
        if self.json:
            self.emit({"type": "result", "result": result})
            return
        state = result.get("state")
        answer = (result.get("response") or {}).get("response") if result.get("ok") else None
        with self._lock:
            if self._midline:
                print(file=self.stdout)
                self._midline = False
        receipts = ", ".join(f"{item['tool']} {item['state']}" for item in
                             (result.get("evidence") or {}).get("receipts", [])) or "none"
        if state == "succeeded":
            # The streamed text was shown as it came; the engine's final answer is repeated
            # when it differs (it is the one the store records).
            if answer is not None and answer.strip() != self._streamed.strip():
                self.say("Answer: " + safe_text(answer))
            self.say(f"[succeeded · receipts: {safe_text(receipts)}]")
        else:
            self.say(f"[{state}: {safe_text(result.get('error') or 'no answer')}]"
                     + (f" receipts: {safe_text(receipts)}" if receipts != "none" else ""),
                     error=state != "cancelled")

    # -- commands -----------------------------------------------------------------------
    def query(self, name: str, **options) -> dict:
        return self.backend_for_turn().query(name, **options)

    def command(self, text: str) -> bool:
        """Run a slash command; False means exit."""
        name, _, rest = text.strip().partition(" ")
        rest = rest.strip()
        if name in ("/exit", "/quit"):
            return False
        try:
            if name == "/help":
                data = {"commands": COMMANDS}
                human = "\n".join(f"  {key:<11} {value}" for key, value in COMMANDS.items())
            elif name == "/new":
                self.session = None
                data, human = {"session_id": None}, "New session."
            elif name == "/resume":
                if not rest:
                    raise ValueError("usage: /resume <session id>")
                data = self.query("session", session_id=rest)
                self.session = rest
                human = f"Resumed {rest}:\n" + self._transcript(data)
            elif name == "/history":
                if not self.session:
                    data, human = {"turns": []}, "(a new session: nothing yet)"
                else:
                    data = self.query("session", session_id=self.session)
                    human = self._transcript(data)
            elif name == "/sessions":
                data = self.query("sessions")
                human = "\n".join(f"  {item['session_id']}  {item['turns']} turn(s), last "
                                  f"{item['last_label']}: {safe_text(item['last_input'][:70])}"
                                  for item in data["sessions"]) or "(no sessions)"
            elif name == "/skills":
                data = self.query("skills")
                human = "\n".join(f"  {item['name']} ({item['scope']}, {item['state']}, "
                                  f"{item['review']}, v{item['version']}"
                                  f"{', pending v' + str(item['pending']) if item['pending'] else ''}"
                                  f"): {safe_text(item['description'])}"
                                  for item in data["skills"]) or "(no skills)"
            elif name == "/memory":
                data = self.query("memory")
                human = "\n".join(f"  [{item['scope']} {item['fact_id']}] {safe_text(item['text'])}"
                                  for item in data["facts"]) or "(no facts)"
            elif name == "/schedules":
                data = self.query("schedules")
                human = "\n".join(f"  {item['schedule_id']} '{safe_text(item['name'])}': "
                                  f"{item['state']}, {item['when']}, next "
                                  f"{item['next_fire_local']}"
                                  for item in data["schedules"]) or "(no schedules)"
            elif name == "/inbox":
                data = self.query("inbox")
                human = "\n".join(
                    f"  {item['state']:<9} {safe_text(item.get('name') or item['schedule_id'])}: "
                    f"{safe_text(((item.get('result') or {}).get('response') or (item.get('result') or {}).get('error') or '')[:100])}"
                    for item in data["inbox"]) or "(the inbox is empty)"
            elif name == "/status":
                data = self.query("status")
                data = {**data, "session_id": self.session}
                worker = (data.get("workers") or [None])[0]
                human = (f"  mode {data.get('mode')}, session {self.session or 'new'}, worker "
                         f"{worker['state'] if worker else 'not started'}"
                         + (f", daemon pid {data['pid']} ({data['health']})" if data.get("pid")
                            else ""))
            elif name == "/stop":
                data = self.query("stop")
                human = (f"Cancelled turn {data.get('turn_id')}." if data.get("cancelled")
                         else f"Nothing to stop ({data.get('reason', 'no turn was running')}).")
            else:
                raise ValueError(f"unknown command {name}; /help lists them")
        except (ValueError, KeyError, daemon.DaemonError) as error:
            self.say(safe_text(error), error=True)
            return True
        except Exception as error:  # a store refusal, for example an unknown session
            self.say(f"{name}: {safe_text(error)}", error=True)
            return True
        if self.json:
            self.emit({"type": "command", "command": name, "result": data})
        else:
            self.say(human)
        return True

    @staticmethod
    def _transcript(view: dict) -> str:
        lines = []
        for turn in view.get("turns", [])[-10:]:
            lines.append(f"  you: {safe_text(turn['user_input'][:200])}")
            answer = turn["response"] if turn["label"] == "succeeded" else f"[{turn['label']}]"
            lines.append(f"  agent: {safe_text((answer or '')[:300])}")
        return "\n".join(lines) or "  (no turns)"

    # -- the loop -----------------------------------------------------------------------
    def _reader(self) -> None:
        """JSON mode: stdin is read on a thread so a cancel arrives while a turn runs."""
        for raw in self.stdin:
            line = raw.rstrip("\n")
            try:
                document = json.loads(line) if line.lstrip().startswith("{") else None
            except ValueError:
                document = None
            if isinstance(document, dict) and document.get("op") == "cancel":
                if self.busy.is_set():
                    self.cancel.set()
                continue
            self.lines.put(line)
        self.lines.put(None)

    def _next(self) -> str | None:
        if self.json:
            return self.lines.get()
        try:
            return input("you> " if self.stdin.isatty() else "")
        except EOFError:
            return None

    def run(self) -> int:
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

        def interrupt(signum, _frame):
            if self.busy.is_set():
                self.cancel.set()  # cancel the turn; the session stays
                self._exit_after_turn = self._exit_after_turn or signum == signal.SIGTERM
            elif signum == signal.SIGTERM:
                raise SystemExit(0)
            else:
                raise KeyboardInterrupt

        for number in previous:
            signal.signal(number, interrupt)
        tty = not self.json and hasattr(self.stdin, "isatty") and self.stdin.isatty()
        if tty:
            self._readline()
        mode = "daemon" if daemon.connect(self.home) else "in-process"
        if self.json:
            self.emit({"type": "ready", "protocol": PROTOCOL, "mode": mode,
                       "session_id": self.session, "commands": sorted(COMMANDS)})
            threading.Thread(target=self._reader, daemon=True, name="brainstem-agent-repl-in").start()
        else:
            self.say(f"Brainstem Agent ({mode}). /help lists commands; Ctrl-C cancels a running "
                     "turn; Ctrl-D or /exit quits.")
        try:
            while True:
                try:
                    line = self._next()
                except KeyboardInterrupt:
                    self.say("(Ctrl-D or /exit quits)")
                    continue
                if line is None:
                    break
                text, document = line, None
                if self.json and line.lstrip().startswith("{"):
                    try:
                        document = json.loads(line)
                    except ValueError:
                        self.say("not JSON", error=True)
                        continue
                    op = document.get("op")
                    if op == "exit":
                        break
                    text = str(document.get("text") or "")
                    if op == "command" and not text.startswith("/"):
                        text = "/" + text
                    elif op not in ("chat", "command"):
                        self.say(f"unknown op {op!r}", error=True)
                        continue
                text = text.strip()
                if not text:
                    continue
                self.remember(text)
                if text.startswith("/") and (document is None or document.get("op") != "chat"):
                    if not self.command(text):
                        break
                    continue
                self.run_turn(text)
                if self._exit_after_turn:
                    break
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            if self.backend is not None:
                self.backend.close()
            if self.json:
                self.emit({"type": "bye", "session_id": self.session})
        return 0

    # -- history and readline -------------------------------------------------------------
    def remember(self, text: str) -> None:
        """Keep a typed line in the history file and, on a terminal, in readline's own
        history (the up arrow); a credential-shaped line goes into neither."""
        if not History.keeps(text):
            if not self.json:
                self.say("(not kept in history: it looks like a credential)")
            return
        self.history.add(text)
        if self._rl is not None:
            self._rl.add_history(text.replace("\n", " "))

    def _readline(self) -> None:
        try:
            import readline
        except ImportError:
            self._rl = None
            return
        self._rl = readline
        # Lines are added by ``remember`` only, so a credential never reaches the up arrow.
        readline.set_auto_history(False)
        readline.clear_history()
        for line in self.history.load():
            readline.add_history(line)
