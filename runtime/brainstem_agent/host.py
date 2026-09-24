"""AgentHost: the cell membrane where owner authority enters.

A turn: reserve (idempotent replay) -> mark running -> resend owned history ->
resolve the credential -> reuse or spawn a worker -> issue a grant bound to that
worker generation -> stream Grail's /chat/stream -> normalise the final ``done``
-> require a bind -> finish -> revoke. Missing binds, Grail errors, EOF without
``done`` and worker crashes never become success; a crash after any tool call
started is ``uncertain``. Cancellation (``cancel()`` or a caller's
``cancel_event``, honoured even before the turn starts) revokes the grant,
cancels in-flight tools and kills the worker's process group; the next turn gets
a fresh worker. A turn returns only after its tools have stopped, and
``close()`` stops in-flight tools before it returns. A lifeline supervisor
records every process group the host starts, so none outlives the host and the
next start reaps what a dead host left behind (``recovered``).

Long turns: while Grail keeps using all three tool rounds of a request, the turn
continues with another request (a segment) that resends the owner's request and the
journal of every tool call so far, within a budget (segments, tool calls, wall time;
a limit ends it ``partial``). Only the model's own words are resent in its voice, and an
answer that narrates tool calls in the journal's format without receipts is never
accepted. Helpers (``delegate_tasks``) run as child turns on fresh workers with at most
the parent's capabilities, as deep as the budget's ``max_depth``. Every turn, segment and
helper is journaled in the store before and after it acts, so recovery can settle whatever
a dead host left running without re-running it.
"""

from __future__ import annotations

import collections
import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import grail, hygiene, knowledge, lifeline, sandbox
from .adapter import DEFAULT_LIMITS, CoreContractError, build_core_request, normalize_sse
from .broker import Broker
from .credential_state import SIGN_IN, CredentialState
from .credential_state import classify as credential_problem
from .credentials import CredentialUnavailable, installed_credential_path, resolve_github_credential
from .longturn import (GRANT_MARGIN, JOURNAL_CHARS, TurnBudget, bounded_stream,
                       continuation_input, crash_point, model_words, partial_text,
                       segment_rounds, unreceipted_claims)
from .organs import BindContext
from .organs.base import OrganError, ToolResult
from .organs.delegate import DelegateOrgan
from .organs.files import FilesOrgan
from .organs.mcp import McpOrgan
from .organs.memory import OUTSIDE, MemoryOrgan
from .organs.processes import ProcessOrgan
from .organs.scripts import ScriptOrgan
from .organs.sessions import SessionOrgan
from .organs.shell import ShellOrgan
from .organs.skills import _SCHEDULED, SkillOrgan
from .organs.web import EgressLog, WebOrgan, read_config
from .observe import EventLog
from .paths import canonical, holds
from .policy import GrantAuthority, GrantDenied, RunBinding
from .retrieval import query_text, rank
from .schedules import ScheduleOrgan
from .session_index import SessionIndex
from .state import SCHEMA_VERSION, StateError, Store
from .worker import GrailWorker, WorkerConfig, WorkerError

__all__ = ["ALL_CAPABILITIES", "AgentHost", "CORE_CAPABILITIES", "DEFAULT_CAPABILITIES",
           "HostCancelled", "HostError", "LEARNING_CAPABILITIES", "LONG_TURN_CAPABILITIES",
           "TURN_CAPABILITIES", "TurnResult", "WEB_CAPABILITIES", "cell_namespace"]

OWNER = "local"
# The core organs (Cell v1); doctor --deep proves the bridge with exactly these tools.
CORE_CAPABILITIES = ("files.read", "files.write", "memory.read", "memory.write", "shell.run")
# The learning cell: the skill index and skill_view, skill_save, and session_search.
LEARNING_CAPABILITIES = ("skills.read", "skills.write", "sessions.read")
# The reaching cell: web_fetch and web_search under the owner's egress policy (reach.json).
# Each configured MCP server adds its own ``mcp.<server>`` (``AgentHost.known_capabilities``).
WEB_CAPABILITIES = ("web.fetch", "web.search")
# The core organs, schedules and learning (the learning cell's tool surface).
DEFAULT_CAPABILITIES = CORE_CAPABILITIES + ("schedule.read", "schedule.write") + \
    LEARNING_CAPABILITIES
# The long-horizon cell: delegate_tasks, run_script and the process_* tools. An owner chat
# turn (and so an owner-created schedule) gets them by default; a schedule a turn creates only
# when that turn names them.
LONG_TURN_CAPABILITIES = ("agents.delegate", "scripts.run", "processes.run")
# An owner chat turn also reaches the web and every configured MCP server; an owner-created
# schedule gets the same (``AgentHost.turn_capabilities``) unless --capabilities narrows it.
TURN_CAPABILITIES = DEFAULT_CAPABILITIES + LONG_TURN_CAPABILITIES + WEB_CAPABILITIES
ALL_CAPABILITIES = TURN_CAPABILITIES
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
_SETTLE_SECONDS = 5.0


class HostError(RuntimeError):
    """The host could not be prepared (paths, permissions, busy)."""


class HostCancelled(HostError):
    """Cancellation arrived before the work started (for example while waiting for the lock)."""


class _Cancelled(Exception):
    pass


def _when_set(event: threading.Event, action: Callable[[], Any], done: threading.Event) -> None:
    """Run ``action`` once if ``event`` is set before ``done`` is (a caller's cancel flag)."""
    def watch() -> None:
        while not done.is_set():
            if event.wait(0.05):
                if not done.is_set():
                    action()
                return

    threading.Thread(target=watch, daemon=True, name="brainstem-agent-cancel").start()


class _ToolCalls:
    """In-flight organ calls by turn, so the host can cancel and await them.

    Grant-bound calls can also be cancelled through the broker; owner-initiated
    (direct) calls have no grant, so the host needs its own view of every call.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: dict[str, list[threading.Event]] = {}
        self._cancelled: set[str] = set()
        self._closed = False

    def enter(self, turn_id: str, event: threading.Event) -> None:
        with self._condition:
            self._events.setdefault(turn_id, []).append(event)
            if self._closed or turn_id in self._cancelled:
                event.set()

    def leave(self, turn_id: str, event: threading.Event) -> None:
        with self._condition:
            events = self._events.get(turn_id, [])
            if event in events:
                events.remove(event)
            if not events:
                self._events.pop(turn_id, None)
            self._condition.notify_all()

    def cancel(self, turn_id: str | None = None, *, close: bool = False) -> None:
        """Cancel one turn's calls (also later ones), every in-flight call, or close."""
        with self._condition:
            self._closed = self._closed or close
            if turn_id is not None:
                self._cancelled.add(turn_id)
            for key, events in self._events.items():
                if turn_id is None or key == turn_id:
                    for event in events:
                        event.set()

    def release(self, turn_id: str) -> None:
        with self._condition:
            self._cancelled.discard(turn_id)

    def wait_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: not self._events, timeout)


class _TurnTools:
    """The tools each turn has called so far, in order (skill, memory and schedule
    governance read it), after the outside content the turn inherited: a helper's from the
    turns above it, a scheduled run's from the conversation that wrote its prompt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tools: dict[str, list[str]] = {}
        self._inherited: dict[str, list[str]] = {}

    def add(self, turn_id: str, tool: str) -> None:
        with self._lock:
            self._tools.setdefault(turn_id, []).append(tool)

    def inherit(self, turn_id: str, tools: Sequence[str]) -> None:
        with self._lock:
            self._inherited[turn_id] = list(tools)

    def before(self, turn_id: str) -> list[str]:
        """The inherited tools, then those called before the latest one (the caller)."""
        with self._lock:
            return [*self._inherited.get(turn_id, ()), *self._tools.get(turn_id, [])[:-1]]

    def drop(self, turn_id: str) -> None:
        with self._lock:
            self._tools.pop(turn_id, None)
            self._inherited.pop(turn_id, None)


class _GuardedOrgan:
    """An organ as the broker sees it, with every call registered in ``_ToolCalls``."""

    def __init__(self, organ: Any, calls: _ToolCalls, history: _TurnTools) -> None:
        self._organ, self._calls, self._history = organ, calls, history
        self.name = organ.name
        self.dynamic = getattr(organ, "dynamic", False)

    def tools(self):
        return self._organ.tools()

    def context(self, context: BindContext) -> str | None:
        return self._organ.context(context)

    def invoke(self, context, tool: str, arguments: Mapping[str, Any]):
        self._calls.enter(context.turn_id, context.cancelled)
        self._history.add(context.turn_id, tool)
        try:
            return self._organ.invoke(context, tool, arguments)
        finally:
            self._calls.leave(context.turn_id, context.cancelled)


def cell_namespace(owner: str, workspace: str) -> str:
    pair = json.dumps([owner, workspace], ensure_ascii=False, separators=(",", ":"))
    return "ws:" + hashlib.sha256(pair.encode("utf-8")).hexdigest()


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if info.st_uid != os.geteuid() or path.is_symlink():
        raise HostError(f"{path} must be a directory owned by you.")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


@dataclass
class TurnResult:
    ok: bool
    state: str
    replayed: bool
    turn_id: str | None
    session_id: str | None
    response: dict | None
    error: str | None
    evidence: dict = field(default_factory=dict)
    # A long turn stopped by a limit: {"limit", "text", "done", "failed", "uncertain_calls"}.
    partial: dict | None = None

    def to_json(self) -> dict:
        document = {"ok": self.ok, "state": self.state, "replayed": self.replayed,
                    "turn_id": self.turn_id, "session_id": self.session_id,
                    "response": self.response, "error": self.error, "evidence": self.evidence}
        if self.partial is not None:
            document["partial"] = self.partial
        return document


class _Run:
    """One running turn (the owner's, or a helper's child turn): budget, journal and the
    tool calls made so far, in order (the continuation journal)."""

    def __init__(self, *, turn_id: str, namespace: str, session_id: str, workspace: str,
                 capabilities: tuple[str, ...], message: str, budget: TurnBudget,
                 cancel: threading.Event, progress: Callable[[dict], None] | None,
                 depth: int = 0, root: str | None = None, child: bool = False) -> None:
        self.turn_id, self.namespace, self.session_id = turn_id, namespace, session_id
        self.workspace, self.capabilities, self.message = workspace, capabilities, message
        self.budget, self.cancel, self.depth = budget, cancel, depth
        self.root = root or turn_id
        self.child = child
        self._progress = progress
        self.started = time.monotonic()
        self.deadline = self.started + budget.max_seconds
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.admitted = 0
        self.segment = 0
        self.limit: str | None = None
        self.grant: str | None = None
        self.worker = None
        self.segments: list[dict] = []
        self.children: list[dict] = []
        self.child_count = 0
        self.unrecorded: dict[str, int] = {}
        self.binds = 0
        self.dispatched = False
        self.last_answer = ""
        # Calls the model's last answer narrated without receipts (never resent or quoted).
        self.rejected: list[str] = []
        # The calls of this run's helpers (and theirs), which an honest answer may quote.
        self.child_calls: list[dict] = []

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def emit(self, event: str, **fields: Any) -> None:
        if self._progress is None:
            return
        document = {"event": event, "turn_id": self.root, **fields}
        if self.child:
            document["child"] = self.turn_id
        try:
            self._progress(document)
        except Exception:
            pass  # a progress sink never breaks the turn


class AgentHost:
    def __init__(self, home: Path, *, workspace: Path | None = None, cache: Path | None = None,
                 environ: Mapping[str, str] | None = None, model: str | None = None,
                 worker_factory: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        # One spelling per place (links, letter case and firmlinks resolved), so namespaces,
        # Seatbelt rules and the workspace guard all agree whatever path the caller used.
        self.home = _private_dir(canonical(home))
        self.cache = canonical(
            cache or self.environ.get("BRAINSTEM_AGENT_CACHE") or self.home / "cache")
        explicit = self.environ.get("BRAINSTEM_AGENT_GITHUB_TOKEN_FILE")
        self._credential_dirs = tuple(dict.fromkeys(
            canonical(path).parent for path in (installed_credential_path(self.environ),
                                                *([Path(explicit)] if explicit else []))))
        chosen = workspace or self.environ.get("BRAINSTEM_AGENT_WORKSPACE")
        if chosen is None:
            chosen = _private_dir(self.home / "workspaces" / "default")
        self.workspace = self._checked_workspace(chosen)
        self.namespace = cell_namespace(OWNER, str(self.workspace))
        self.model = model or self.environ.get("BRAINSTEM_AGENT_MODEL") or "auto"
        state = _private_dir(self.home / "state")
        self.store = Store(state / "agent.sqlite3")
        # Operations: the local event log and the credential's recorded state (never its value).
        self.events = EventLog(self.home)
        self.credential_state = CredentialState(self.home)
        if self.store.pre_migration_backup:
            backup = self.store.pre_migration_backup
            self.events.write("store.migrated", from_schema_version=backup["from_schema_version"],
                              to_schema_version=SCHEMA_VERSION,
                              backup=Path(backup["path"]).name)
        try:
            self.supervisor = lifeline.Supervisor(self.home)
        except (OSError, lifeline.LifelineError) as error:
            self.store.close()
            raise HostError(f"The process lifeline could not be prepared: {error}") from None
        self.recovered = self.supervisor.recovered
        self.authority = GrantAuthority(self.store, clock=clock, mode="local")
        self.profile_namespace = knowledge.profile_namespace(OWNER)
        self._tool_history = _TurnTools()
        # A helper's task was written by the model, never the owner's words.
        turn_input = (lambda turn_id: "" if turn_id in self._child_turns
                      else self._inputs.get(turn_id, ""))
        # Facts reach the model through the budgeted learned-context block, not the organ.
        self.memory_organ = MemoryOrgan(self.store, in_context=False,
                                        prior_tools=self._tool_history.before,
                                        turn_input=turn_input,
                                        schedule_creator=self._schedule_creator)
        self.session_index = SessionIndex(state / "search.sqlite3")
        self._context_reports: dict[str, dict] = {}
        self.shell_organ = ShellOrgan(run_root=self.home / "run" / "shell",
                                      deny_read=(self.home, *self._credential_dirs),
                                      environ=self.environ, supervisor=self.supervisor)
        self._calls = _ToolCalls()
        # Schedule changes call ``schedules_changed`` (the daemon wakes its loop); its text,
        # if any, is appended to the tool result.
        self.schedules_changed: Callable[[], str | None] = lambda: None
        self.schedule_organ = ScheduleOrgan(self.store, notify=lambda: self.schedules_changed(),
                                            prior_tools=self._tool_history.before)
        self.skill_organ = SkillOrgan(
            self.store, profile_namespace=knowledge.profile_namespace,
            prior_tools=self._tool_history.before, turn_input=turn_input,
            schedule_creator=self._schedule_creator)
        self.session_organ = SessionOrgan(self.store, self.session_index)
        self.budget_defaults = TurnBudget.from_env(self.environ)
        self.delegate_organ = DelegateOrgan(self._delegate)
        self.script_organ = ScriptOrgan(run_root=self.home / "run" / "scripts",
                                        deny_read=(self.home, *self._credential_dirs),
                                        environ=self.environ, supervisor=self.supervisor,
                                        crash=lambda name: crash_point(name, self.environ))
        self.process_organ = ProcessOrgan(self.store, run_root=self.home / "run" / "processes",
                                          deny_read=(self.home, *self._credential_dirs),
                                          environ=self.environ, supervisor=self.supervisor,
                                          crash=lambda name: crash_point(name, self.environ))
        # The reaching cell: the owner's reach.json (web egress policy, MCP servers), read
        # fresh on every use; every outbound request lands in state/egress.jsonl.
        self.egress = EgressLog(state / "egress.jsonl")
        reach = lambda: read_config(self.home / "reach.json")  # noqa: E731
        self.web_organ = WebOrgan(config=reach, log=self.egress, environ=self.environ,
                                  budget_key=lambda turn_id: getattr(
                                      self._runs.get(turn_id), "root", turn_id))
        self.mcp_organ = McpOrgan(config=reach, run_root=self.home / "run" / "processes",
                                  deny_read=(self.home, *self._credential_dirs),
                                  environ=self.environ, supervisor=self.supervisor,
                                  log=self.egress, pins=state / "mcp-pins.json")
        self._runs: dict[str, _Run] = {}
        self._child_turns: set[str] = set()
        self.broker = Broker(
            organs=[_GuardedOrgan(organ, self._calls, self._tool_history)
                    for organ in (FilesOrgan(), self.memory_organ, self.shell_organ,
                                  self.schedule_organ, self.skill_organ, self.session_organ,
                                  self.delegate_organ, self.script_organ, self.process_organ,
                                  self.web_organ, self.mcp_organ)],
            resolve_grant=lambda grant, worker, generation: self.authority.resolve(
                grant, worker_id=worker, generation=generation),
            bind_context=self._bind_context, receipts=self.store,
            namespace=lambda binding: cell_namespace(binding.owner, binding.workspace),
            learned_context=self._learned_context, admit=self._admit, observe=self._observe)
        self._tool_names = frozenset(spec.name for spec in self.broker.tool_specs(
            ALL_CAPABILITIES))
        try:
            self.broker.start()
        except OSError as error:
            self.supervisor.close()
            self.store.close()
            raise HostError(f"The tool broker could not start: {error}") from None
        self._factory = worker_factory or self._grail_worker
        self._inputs: dict[str, str] = {}
        self._worker = None
        self._worker_lock = threading.Lock()
        self._discard_lock = threading.Lock()
        self._worker_fingerprint = None
        self._worker_evidence: dict | None = None
        self._turn: dict | None = None
        self._turn_lock = threading.Lock()
        self._closed = False
        self.last_stop: dict | None = None
        # The workers this host stopped (identity and measured outcome), newest last.
        self.discarded: collections.deque = collections.deque(maxlen=8)

    # -- helpers -------------------------------------------------------------------
    def _schedule_creator(self, schedule_id: str) -> str | None:
        try:
            return self.store.get_schedule(None, schedule_id)["created_by"]
        except StateError:
            return None

    def _writer_outside(self, namespace: str, message: str) -> list[str]:
        """The outside content (web, MCP, helpers' answers) that the conversation which wrote
        a scheduled run's prompt had read, and for a helper the web and MCP reads of the
        turns above it: the run starts tainted by it, so a page cannot plant a schedule whose
        later runs change memory, skills or schedules as if they were clean."""
        scheduled = _SCHEDULED.match(message)
        creator = self._schedule_creator(scheduled.group(1)) if scheduled else None
        if not creator or not creator.startswith("turn:"):
            return []
        found: set[str] = set()
        turn_id, own = creator[5:], True
        try:
            for _level in range(4):
                for receipt in self.store.list_receipts(namespace, turn_id=turn_id, limit=None):
                    tool = receipt["tool"]
                    if receipt["state"] != "denied" and tool.startswith(OUTSIDE) and (
                            own or tool != "delegate_tasks"):
                        found.add(tool)
                parent = self.store.step_parents([turn_id]).get(turn_id)
                if parent is None:
                    break
                turn_id, own = parent, False
        except StateError:
            pass
        return sorted(found)

    def _learned_context(self, context: BindContext) -> str:
        """The budgeted memory, profile, skill-index and context-file block for one bind."""
        try:
            text, report = knowledge.assemble(knowledge.gather(
                self.store, namespace=context.namespace, profile=self.profile_namespace,
                workspace_root=Path(context.workspace_root),
                query=query_text(context.user_input), capabilities=context.capabilities,
                now=time.time()))
        except (StateError, OSError, ValueError) as error:
            self._context_reports[context.turn_id] = {"error": type(error).__name__}
            raise
        sections = report["sections"]
        self.memory_organ.note_context(context.turn_id, sum(
            sections.get(name, {}).get("shown", 0) for name in ("profile", "memory")))
        self._context_reports[context.turn_id] = report
        while len(self._context_reports) > 64:
            self._context_reports.pop(next(iter(self._context_reports)))
        return text

    def _bind_context(self, binding: RunBinding) -> BindContext:
        return BindContext(
            owner=binding.owner, workspace=binding.workspace,
            namespace=cell_namespace(binding.owner, binding.workspace),
            session_id=binding.session_id, turn_id=binding.turn_id,
            workspace_root=Path(binding.workspace), user_input=self._inputs.get(binding.turn_id, ""),
            capabilities=binding.capabilities)

    def _grail_worker(self, *, worker_id, broker_url, credential, model, register, probe=None):
        source = grail.ensure_grail_source(self.cache, fetch=False)
        python = grail.worker_venv_python(self.cache, environ=self.environ)
        deny = [self.workspace]
        if credential is not None and credential.path:
            deny.append(Path(credential.path).parent)
        config = WorkerConfig(worker_id=worker_id, home=self.home, source=source, python=python,
                              broker_url=broker_url, credential=credential, model=model,
                              deny_read=tuple(deny), probe=probe, supervisor=self.supervisor)
        return GrailWorker(config, register=register)

    def _redact(self, text: str, secrets_: Sequence[str] = ()) -> str:
        for value in secrets_:
            if value:
                text = text.replace(value, "[REDACTED]")
        return _TOKEN_SHAPES.sub(r"\1[REDACTED]", text)[:600]

    @contextmanager
    def exclusive(self, wait: float = 10.0, *, cancel: threading.Event | None = None):
        """Serialise mutating commands across processes sharing this home.

        A set ``cancel`` event (for example from SIGINT) ends the wait at once with
        ``HostCancelled``, before any turn is reserved.
        """
        lock_path = self.home / "state" / "host.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + wait
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise HostCancelled("The request was cancelled before it started.")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise HostError("Brainstem Agent is busy with another turn in this home.")
                    if cancel is not None:
                        cancel.wait(0.05)
                    else:
                        time.sleep(0.05)
            self.store.recover_interrupted()
            yield
        finally:
            os.close(descriptor)

    def known_capabilities(self) -> tuple[str, ...]:
        """Every capability a turn or schedule may hold: the cell's and one per MCP server."""
        return ALL_CAPABILITIES + self.mcp_organ.capabilities()

    def turn_capabilities(self) -> tuple[str, ...]:
        """What an owner chat turn holds by default, and so an owner-created schedule unless
        the owner narrows it: the turn's capabilities and every configured MCP server."""
        return TURN_CAPABILITIES + self.mcp_organ.capabilities()

    def _capabilities(self, capabilities: Sequence[str] | None) -> tuple[str, ...]:
        chosen = tuple(dict.fromkeys(capabilities or self.turn_capabilities()))
        unknown = set(chosen) - set(self.known_capabilities())
        if unknown or not chosen:
            raise HostError(f"Unknown capabilities: {sorted(unknown)}")
        return chosen

    @property
    def active_worker(self) -> dict | None:
        worker = self._worker
        if worker is None:
            return None
        return {"worker_id": worker.worker_id, "generation": worker.generation,
                "pid": getattr(worker, "pid", None), "pgid": getattr(worker, "pgid", None)}

    @property
    def active_turn(self) -> dict | None:
        turn = self._turn
        return None if turn is None else {"turn_id": turn["turn_id"], "phase": turn["phase"]}

    def worker_status(self) -> dict | None:
        """One consistent view of the worker: ``starting`` until its start completed, then
        ``warm`` (idle) or ``busy`` (a turn is using it), ``stopped`` once it is gone."""
        with self._worker_lock:
            worker, evidence = self._worker, self._worker_evidence or {}
        if worker is None:
            return None
        generation = worker.generation
        ready = generation is not None and evidence.get("generation") == generation
        alive = bool(worker.alive())
        # A worker the host holds that is not ready is still starting (a failed start clears
        # it), even before its process exists.
        state = ("starting" if not ready else "stopped" if not alive
                 else "busy" if self._turn is not None else "warm")
        return {"worker_id": worker.worker_id, "generation": generation,
                "pid": getattr(worker, "pid", None), "pgid": getattr(worker, "pgid", None),
                "alive": alive, "state": state, "warm": state == "warm",
                "start_seconds": evidence.get("start_seconds") if ready else None,
                "integrity": (evidence.get("integrity_reuse") or evidence.get("integrity_before"))
                if ready else None}

    # -- workers -------------------------------------------------------------------
    def _discard_worker(self, **options) -> dict | None:
        # Held for the whole stop: a second caller (the turn itself after a cancel)
        # returns only once the worker's group is stopped and its evidence recorded.
        with self._discard_lock:
            with self._worker_lock:
                worker, self._worker = self._worker, None
            if worker is None:
                return None
            evidence = worker.stop(**options)
            self.broker.unregister_worker(worker.worker_id)
            self.last_stop = evidence
            self.discarded.append({
                "worker_id": worker.worker_id, "generation": worker.generation,
                "pid": getattr(worker, "pid", None), "pgid": getattr(worker, "pgid", None),
                "group_gone": evidence.get("group_gone"), "at": time.time()})
            if self._worker_evidence is not None:
                self._worker_evidence["integrity_after"] = evidence.get("integrity_after")
                self._worker_evidence["stop"] = {k: evidence.get(k) for k in
                                                 ("seconds", "exit_code", "group_gone")}
            return evidence

    def _ensure_worker(self, credential) -> dict:
        fingerprint = hashlib.sha256(credential.value.encode()).hexdigest()
        worker = self._worker
        failure = None
        if worker is not None and worker.alive() and self._worker_fingerprint == fingerprint:
            # A warm worker is reused only while its copy still matches the pinned inventory.
            verify = getattr(worker, "verify_integrity", None)
            started = time.monotonic()
            try:
                untracked = verify() if verify is not None else None
            except (grail.GrailSourceError, OSError) as error:
                untracked, failure = False, str(error)[:300]
            if untracked is not False:
                evidence = dict(self._worker_evidence or {})
                evidence["reused"] = True
                evidence["integrity_reuse"] = {
                    "ok": True, "untracked": untracked,
                    "seconds": round(time.monotonic() - started, 4)}
                with self._worker_lock:
                    self._worker_evidence = evidence
                return evidence
        self._discard_worker()
        worker = self._factory(worker_id="w" + secrets.token_hex(4), broker_url=self.broker.url,
                               credential=credential, model=self.model,
                               register=self.broker.register_worker)
        with self._worker_lock:
            self._worker, self._worker_fingerprint = worker, fingerprint
        try:
            started = worker.start()
        except BaseException:
            with self._worker_lock:
                if self._worker is worker:
                    self._worker = None
            self.broker.unregister_worker(worker.worker_id)
            raise
        evidence = {**started, "reused": False}
        if failure:
            evidence["replaced_after_integrity_failure"] = failure
        with self._worker_lock:
            self._worker_evidence = evidence
        return evidence

    def warm(self) -> dict | None:
        """Start, or re-verify and keep, the warm worker between turns (never during one).
        While the credential is known to be rejected nothing starts (no retry loop); a changed
        credential file clears that, and a new worker starts with the new token."""
        if self._closed or not self._turn_lock.acquire(blocking=False):
            return None
        try:
            credential = resolve_github_credential(environ=self.environ)
            verdict = self.credential_state.check(credential)
            if verdict.get("changed"):
                self.events.write("credential.changed", source=credential.source,
                                  previous_state=verdict.get("was"))
            if verdict.get("refuse"):
                return None
            try:
                evidence = self._ensure_worker(credential)
            except WorkerError as error:
                self._note_credential(credential, "failed", str(error))
                raise
            self._note_credential(credential, "started", None,
                                  fresh_worker=evidence.get("reused") is False)
            return evidence
        finally:
            self._turn_lock.release()

    # -- chat ----------------------------------------------------------------------
    def _checked_workspace(self, chosen: Path | str) -> Path:
        """The workspace's one spelling, refused where its file and shell tools would reach
        the cell's private state (knowledge, grants, the daemon's key), the Grail cache or
        the Copilot credential. Places are compared by identity, not by spelling, so a /var
        or symlinked-parent path, another letter case or a firmlink never gets past."""
        workspace = canonical(chosen)
        if not workspace.is_dir():
            raise HostError(f"The workspace {workspace} is not a directory.")
        rooms = self.home / "workspaces"
        if holds(workspace, self.home):
            raise HostError(f"The workspace {workspace} is the cell's home ({self.home}) or "
                            "contains it, so its tools would reach the cell's private state; "
                            "choose a project directory.")
        if holds(self.home, workspace) and (not holds(rooms, workspace)
                                            or holds(workspace, rooms)):
            raise HostError(f"The workspace {workspace} is inside the cell's home; only a "
                            f"directory under {rooms} can be a workspace there.")
        for label, place in (("the Grail cache", self.cache),
                             *(("the Copilot credential's directory", item)
                               for item in self._credential_dirs)):
            if holds(workspace, place) or holds(place, workspace):
                raise HostError(f"The workspace {workspace} holds or lies inside {label} "
                                f"({place}); choose a project directory.")
        return workspace

    def _workspace(self, workspace: Path | str | None) -> Path:
        if workspace is None:
            return self.workspace
        return self._checked_workspace(workspace)

    def chat(self, message: str, *, session_id: str | None = None,
             idempotency_key: str | None = None, capabilities: Sequence[str] | None = None,
             timeout: float = 300.0, cancel_event: threading.Event | None = None,
             workspace: Path | str | None = None, budget: TurnBudget | None = None,
             progress: Callable[[dict], None] | None = None) -> TurnResult:
        """Run one turn (in ``workspace``, default the host's). Setting ``cancel_event``
        (before or during the turn) cancels it. A turn continues across Grail requests
        (segments) while Grail keeps running out of tool rounds, within ``budget``
        (default ``budget_defaults``); ``progress`` receives its events."""
        cancel = cancel_event if cancel_event is not None else threading.Event()
        if not self._turn_lock.acquire(blocking=False):
            return TurnResult(False, "failed", False, None, session_id, None,
                              "This host already has an active turn.", {})
        try:
            if self._closed:
                return TurnResult(False, "failed", False, None, session_id, None,
                                  "This host is closed.", {})
            if cancel.is_set():
                return TurnResult(False, "cancelled", False, None, session_id, None,
                                  "The turn was cancelled before it started.",
                                  {"worker": None, "grail_calls": 0})
            refusal = self._preflight(workspace, session_id, idempotency_key)
            if refusal is not None:
                return refusal
            return self._chat(message, session_id, idempotency_key, capabilities, timeout, cancel,
                              workspace, budget or self.budget_defaults, progress)
        finally:
            self._turn_lock.release()

    def _preflight(self, workspace, session_id, idempotency_key) -> TurnResult | None:
        """Refuse a new turn at once, before anything is written or started, while disk space
        is below the floor or the Copilot credential is known to be rejected. A replay of a
        retained turn needs neither, so it is never refused."""
        if idempotency_key is not None:
            try:
                namespace = cell_namespace(OWNER, str(self._workspace(workspace)))
                if self.store.has_replay(namespace, session_id, idempotency_key):
                    return None
            except (HostError, StateError):
                return None  # the turn itself reports it
        reason, detail = None, {}
        message = hygiene.low_disk_refusal(self.home, self.environ)
        if message:
            reason, detail = "disk", {"disk": hygiene.disk_status(self.home, self.environ)}
        else:
            try:
                credential = resolve_github_credential(environ=self.environ)
            except CredentialUnavailable:
                return None  # the turn reports a missing credential as before
            verdict = self.credential_state.check(credential)
            if verdict.get("changed"):
                self.events.write("credential.changed", source=credential.source,
                                  previous_state=verdict.get("was"))
            if not verdict.get("refuse"):
                return None
            reason = "credential-" + verdict["state"].replace("_", "-")
            message = verdict["message"]
            detail = {"credential_state": {key: verdict.get(key) for key in
                                           ("state", "since", "retry_after", "attempts")}}
        self.events.write("turn.refused", level="warn", reason=reason, session_id=session_id,
                          scheduled=bool(idempotency_key and idempotency_key.startswith("occ_")))
        return TurnResult(False, "failed", False, None, session_id, None, message,
                          {"refused": reason, "worker": None, "grail_calls": 0, **detail})

    def _note_credential(self, credential, state: str, error: str | None, *,
                         fresh_worker: bool = False) -> str | None:
        """Record what a turn or warm start learned about the credential; returns the fix to
        add to the error when the credential itself was the problem."""
        if credential is None:
            return None
        kind = credential_problem(error) if state != "succeeded" and error else None
        if kind is not None:
            try:
                record = self.credential_state.record_failure(credential, kind, error)
            except OSError:
                return SIGN_IN
            self.events.write("credential.invalid", level="error", state=kind,
                              source=credential.source, attempts=record["attempts"],
                              retry_after=record["retry_after"], reason=error)
            return SIGN_IN if kind == "invalid" else (
                "Enable Copilot for the signed-in GitHub account, or sign in with one that has "
                "it.")
        if (state == "succeeded" or fresh_worker) and self.credential_state.record_success():
            self.events.write("credential.recovered", source=credential.source)
        return None

    def _replayed(self, reservation, namespace: str) -> TurnResult:
        if reservation.state == "succeeded":
            return TurnResult(True, "succeeded", True, reservation.turn_id,
                              reservation.session_id, dict(reservation.response), None,
                              {"class": "replay", "worker": None, "grail_calls": 0})
        state, partial = reservation.state, None
        try:
            step = self.store.get_step("journal_" + reservation.turn_id)
        except StateError:
            step = None
        if step is not None and step["state"] == "partial" and isinstance(step["result"], dict):
            state, partial = "partial", step["result"].get("partial")
        return TurnResult(False, state, True, reservation.turn_id, reservation.session_id, None,
                          f"The retained turn is {state}; it was not redispatched.",
                          {"worker": None, "grail_calls": 0}, partial)

    def _chat(self, message, session_id, idempotency_key, capabilities, timeout,
              cancel, workspace=None, budget: TurnBudget | None = None,
              progress: Callable[[dict], None] | None = None) -> TurnResult:
        budget = budget or self.budget_defaults
        try:
            chosen = self._workspace(workspace)
            namespace = cell_namespace(OWNER, str(chosen))
            caps = self._capabilities(capabilities)
            if budget.max_depth < 1:  # no helper may start, so none is offered
                caps = tuple(item for item in caps if item != "agents.delegate") or caps
            build_core_request(message, session_id or "preflight", [])
            reservation = self.store.reserve_chat(namespace, message, session_id, idempotency_key)
        except (HostError, StateError, CoreContractError) as error:
            return TurnResult(False, "failed", False, None, session_id, None,
                              f"The turn was refused: {error}", {})
        if not reservation.created:
            return self._replayed(reservation, namespace)
        turn_id, session = reservation.turn_id, reservation.session_id
        self.store.mark_chat_running(namespace, turn_id)
        evidence: dict[str, Any] = {
            "class": "live", "environment": sandbox.ENVIRONMENT,
            "grail": {"commit": grail.PINNED_COMMIT, "version": grail.VERSION,
                      "kernel_sha256": grail.KERNEL_SHA256},
            "model": self.model, "capabilities": list(caps), "history_messages": 0,
            "memory_facts_in_context": 0, "binds": 0, "receipts": [], "worker": None,
            "tool_started": False}
        run = _Run(turn_id=turn_id, namespace=namespace, session_id=session,
                   workspace=str(chosen), capabilities=caps, message=message, budget=budget,
                   cancel=cancel, progress=progress)
        turn = {"turn_id": turn_id, "phase": "preparing", "cancel": cancel, "grant": None,
                "run": run}
        self._turn = turn
        self._runs[turn_id] = run
        self._inputs[turn_id] = message
        self._tool_history.inherit(turn_id, self._writer_outside(namespace, message))
        if self._closed:
            cancel.set()
        ended = threading.Event()
        _when_set(cancel, lambda: self._cancel_turn(turn), ended)
        self._watch_deadline(run, ended, turn)
        state, response, error = "failed", None, None
        secrets_: list[str] = []
        journal = None
        credential = None
        self.events.write("turn.started", turn_id=turn_id, session_id=session,
                          workspace=namespace[:15], capabilities=len(caps),
                          scheduled=bool(idempotency_key and idempotency_key.startswith("occ_")))
        try:
            journal = self.store.begin_step(namespace, turn_id, "turn", 0, {
                "session_id": session, "capabilities": list(caps), "budget": budget.to_json(),
                "workspace": str(chosen)}, step_id="journal_" + turn_id)
            run.emit("turn.started", session_id=session, budget=budget.to_json())
            history = self.store.history(namespace, session)
            evidence["history_messages"] = len(history)
            credential = resolve_github_credential(environ=self.environ)
            secrets_.append(credential.value)
            evidence["credential"] = credential.describe()
            if cancel.is_set():
                raise _Cancelled()

            def worker_for_segment():
                evidence["worker"] = self._ensure_worker(credential)
                worker = self._worker
                if worker is None:
                    raise WorkerError("The Grail worker was stopped before the turn could start.")
                return worker

            turn["phase"] = "streaming"
            response = self._segments(run, history, worker_for_segment, secrets_, turn=turn)
            state = "succeeded" if response is not None else "partial"
        except _Cancelled:
            pass
        except CredentialUnavailable as problem:
            error = f"No usable Copilot credential: {problem}"
        except (grail.GrailSourceError, WorkerError, sandbox.SandboxUnavailable, GrantDenied,
                HostError) as problem:
            error = str(problem)
        except StateError as problem:
            error = f"The turn's journal could not be written: {problem}"
        except (OSError, ValueError) as problem:
            error = ("The Grail worker stopped or the stream broke mid-turn "
                     f"({type(problem).__name__}).")
        finally:
            ended.set()
            self._end_run(run)
            unrecorded = dict(run.unrecorded)
            try:
                receipts = self._settled_receipts(namespace, turn_id,
                                                  stuck=unrecorded.get("unfinished", 0))
                unknown = False
            except StateError:
                receipts, unknown = [], True
            self._calls.release(turn_id)
            evidence["binds"] = run.binds
            evidence["receipts"] = [{"tool": r["tool"], "state": r["state"],
                                     "call_id": r["call_id"]} for r in receipts]
            evidence["tool_started"] = unknown or any(r["state"] != "denied" for r in receipts) \
                or any(child.get("tool_started") for child in run.children)
            if unrecorded:
                evidence["unrecorded_tool_calls"] = unrecorded
            unsettled = [r["tool"] for r in receipts if r["state"] == "started"]
            evidence["memory_facts_in_context"] = self.memory_organ.context_count(turn_id)
            evidence["context"] = self._context_summary(self._context_reports.pop(turn_id, None))
            self._inputs.pop(turn_id, None)
            self._tool_history.drop(turn_id)
            self._runs.pop(turn_id, None)
            partial = None
            if cancel.is_set() and run.limit != "seconds":
                state, response = "cancelled", None
                error = "The turn was cancelled."
                if evidence["tool_started"]:
                    error += " A tool call had started, so its effects are uncertain."
            elif run.limit and state in ("succeeded", "partial", "failed") and not (
                    unsettled or unknown or unrecorded.get("not_run")):
                # A limit ended the turn: an explicit partial result, never success.
                interrupted = [call["tool"] for call in run.calls if call.get("cancelled")]
                last = (response or {}).get("response") or run.last_answer
                text = partial_text(run.limit, budget, run.calls, last, interrupted=interrupted,
                                    rejected=run.rejected)
                done = [call["tool"] for call in run.calls if call.get("ok")]
                partial = {"limit": run.limit, "text": text, "done": done,
                           "failed": [call["tool"] for call in run.calls
                                      if not call.get("ok") and not call.get("denied")],
                           "uncertain_calls": interrupted, "last_answer": last[:2000]}
                state, response, error = "partial", None, text
            elif state == "succeeded" and (unsettled or unknown):
                # A tool's outcome was never durably recorded: never report success.
                state, response = "uncertain", None
                error = (f"The outcome of {', '.join(unsettled) or 'its tool calls'} could not "
                         "be recorded, so the turn's effects are uncertain.")
            elif state == "succeeded" and unrecorded.get("not_run"):
                state, response = "failed", None
                error = ("A tool call could not be recorded, so it was not run; the turn is not "
                         "treated as success.")
            elif state not in ("succeeded", "partial") and run.dispatched and \
                    evidence["tool_started"]:
                state = "uncertain"
                error = (error or "The turn did not complete.") + \
                    " A tool call had started, so the turn's effects are uncertain."
            elif state == "partial":
                state, error = "failed", error or "The turn ended without an answer."
            evidence["long_turn"] = self._long_turn_evidence(run)
            evidence["grail_calls"] = evidence["long_turn"]["grail_requests"]
            try:
                if state != "succeeded" or self._worker is None or not self._worker.alive():
                    self._discard_worker(grace=1.0, timeout=4.0)
                crash_point("turn.finishing", self.environ)
                if journal is not None:
                    self.store.finish_step(journal, state, {
                        "state": state, "segments": run.segment, "limit": run.limit,
                        "partial": partial, "tool_calls": run.admitted,
                        "tool_started": evidence["tool_started"],
                        "seconds": evidence["long_turn"]["seconds"]})
                self.store.finish_chat(namespace, turn_id, "failed" if state == "partial"
                                       else state, response if state == "succeeded" else None)
                if state == "succeeded":  # best effort: a search repairs the index later
                    self.session_index.add(self.store, namespace, turn_id)
            except StateError:
                # The turn stays "running" in the store until recovery settles it.
                if state == "succeeded":
                    state, response = "uncertain", None
                    error = "The turn's result could not be recorded; its effects are uncertain."
            finally:
                self._turn = None
            run.emit("turn.finished", state=state, segments=run.segment,
                     seconds=evidence["long_turn"]["seconds"], limit=run.limit)
        if error:
            error = self._redact(error, secrets_)
            if partial is not None:
                partial["text"] = error
        fix = self._note_credential(credential, state, error)
        if fix and error:
            error = f"{error} {fix}"
            evidence["credential_problem"] = True
        self.events.write(
            "turn.finished", level="info" if state == "succeeded" else "warn", turn_id=turn_id,
            session_id=session, state=state, seconds=evidence["long_turn"]["seconds"],
            grail_requests=evidence.get("grail_calls"), tool_calls=len(evidence["receipts"]),
            limit=run.limit, error=error[:300] if error else None)
        return TurnResult(state == "succeeded", state, False, turn_id, session, response, error,
                          evidence, partial)

    # -- long turns: segments, budgets, the journal and helpers ---------------------------
    def _segments(self, run: _Run, history: list, worker_for_segment, secrets_: list[str], *,
                  turn: dict | None = None) -> dict | None:
        """Run the turn's segments until the model answers before Grail's round limit.

        Returns the final normalized response, or None when a limit ended the turn
        (``run.limit``). Raises ``_Cancelled``, ``WorkerError`` (Grail failed),
        ``HostError`` (a first request that cannot fit) or ``OSError``/``ValueError`` (the
        stream broke); a segment's outcome is journaled before anything else happens
        either way. An answer that narrates tool calls without receipts is never returned:
        the next segment says those calls did not run (or the turn ends partial)."""
        response: dict | None = None
        run.last_answer, run.rejected = "", []
        while True:
            if run.cancel.is_set():
                raise _Cancelled()
            number = run.segment + 1
            if number > run.budget.max_segments:
                run.limit = run.limit or "segments"
                return None
            if run.remaining() <= 1.0:
                run.limit = run.limit or "seconds"
                return None
            request = self._request_for(run, number, history)
            if request is None:
                if number == 1:
                    raise HostError("The conversation is too large for one Grail request.")
                run.limit = run.limit or "size"
                return None
            worker = worker_for_segment()
            if run.cancel.is_set():
                raise _Cancelled()
            outcome = self._segment(run, number, request, worker, secrets_, turn=turn)
            response = outcome["response"]
            if response:
                run.rejected = outcome["claims"]
                run.last_answer = "" if run.rejected else response.get("response", "")
            if run.limit:
                return None
            if run.rejected:
                run.emit("turn.continuing", segment=number + 1, reason="unreceipted-tool-log")
                continue
            if not outcome["exhausted"]:
                return response
            run.emit("turn.continuing", segment=number + 1, reason="grail-round-limit")

    def _request_for(self, run: _Run, number: int, history: list) -> dict | None:
        """Segment ``number``'s Grail request, fitted to one request, or None if it cannot
        fit. Segment 1 sends the owner's message. A continuation sends the cell's
        user-side continuation text; the model's own last words (never the cell's text,
        Grail's stand-in for an empty answer or a rejected answer) follow the owner's message
        as the assistant's turn. Whatever does not fit gives way: the session's oldest
        history first, then the repeated request and the journal's oldest results."""
        if number == 1:
            return self._fit_request(run, run.message, [], history)
        words = model_words(run.last_answer)
        tail = [{"role": "user", "content": run.message},
                {"role": "assistant", "content": words}] if words else []
        for journal_limit, message_limit in ((JOURNAL_CHARS, None), (JOURNAL_CHARS, 8_000),
                                             (8_000, 4_000), (2_000, 2_000)):
            text = continuation_input(
                run.message, run.calls, words, segment=number,
                max_segments=run.budget.max_segments, rejected=run.rejected,
                journal_limit=journal_limit, message_limit=message_limit,
                message_above=bool(tail))
            request = self._fit_request(run, text, tail, history)
            if request is not None:
                return request
        return None

    @staticmethod
    def _fit_request(run: _Run, text: str, tail: list, history: list) -> dict | None:
        """The request with as much of the session's history as fits the envelope (its
        oldest turns are left out first), or None if even the newest part cannot fit."""
        limits = DEFAULT_LIMITS
        sizes = [len(message["content"].encode("utf-8")) for message in history]
        room = limits.max_history_bytes - sum(len(m["content"].encode("utf-8")) for m in tail)
        first = max(0, len(history) - (limits.max_history_messages - len(tail)))
        total = sum(sizes[first:])
        while first < len(history) and (total > room or history[first]["role"] != "user"):
            total -= sizes[first]
            first += 1
        try:
            return build_core_request(text, run.session_id, [*history[first:], *tail])
        except CoreContractError:
            return None

    def _segment(self, run: _Run, number: int, request: dict, worker, secrets_: list[str], *,
                 turn: dict | None = None) -> dict:
        """One Grail request of a turn, journaled before it is sent and after it ends."""
        started, begun = time.monotonic(), time.time()
        step = self.store.begin_step(run.namespace, run.turn_id, "segment", number, {
            "worker_id": worker.worker_id, "generation": worker.generation,
            "continuation": number > 1})
        run.segment = number
        record = {"segment": number, "state": "running", "worker_id": worker.worker_id,
                  "started_at": begun, "continuation": number > 1}
        run.segments.append(record)
        crash_point("segment.started", self.environ)
        run.emit("segment.started", segment=number, continuation=number > 1)
        # The MCP servers this turn may reach are up (or restarted) before Grail binds.
        self.mcp_organ.prepare(run.capabilities)
        binding = RunBinding(OWNER, run.workspace, run.session_id, run.turn_id,
                             worker.worker_id, worker.generation, run.capabilities)
        # The grant outlives the turn's time limit (a limit is at most 3600 s - GRANT_MARGIN).
        grant = self.authority.issue(binding, ttl=min(3600, max(60.0, run.remaining()
                                                                + GRANT_MARGIN)))
        secrets_.append(grant)
        run.grant = grant
        if turn is not None:
            turn["grant"] = grant
        chunks: list[str] = []
        state, error, response = "failed", None, None
        claims: list[str] = []
        try:
            if run.cancel.is_set():
                raise _Cancelled()
            run.dispatched = True
            read_timeout = max(30.0, run.remaining() + 30.0)
            for line in worker.stream_chat(request, grant, read_timeout=read_timeout):
                chunks.append(line.decode("utf-8", "replace"))
                if run.cancel.is_set():
                    break
            if run.cancel.is_set():
                raise _Cancelled()
            try:
                # Grail's logs repeat every tool result: bounded before the strict adapter.
                response = normalize_sse(bounded_stream(chunks), run.session_id)
            except CoreContractError:
                reason = self._stream_error(chunks)
                raise WorkerError(f"Grail reported an error: {reason}" if reason else
                                  "Grail ended the stream without a final answer (no done event).")
            if self.broker.bind_count(grant) < 1:
                response = None
                raise WorkerError("Grail answered without attaching the cell's tools (no bind); "
                                  "the answer is not treated as success.")
            state = "succeeded"
        except _Cancelled:
            state = "cancelled"
            raise
        except BaseException as problem:
            error = type(problem).__name__
            raise
        finally:
            self.broker.cancel_grant(grant)
            revoked = False
            try:
                self.authority.revoke(grant)
                revoked = True
            except StateError:
                pass
            run.binds += self.broker.bind_count(grant)
            for kind, count in self.broker.unrecorded(grant).items():
                run.unrecorded[kind] = run.unrecorded.get(kind, 0) + count
            if revoked:  # an unrevoked grant stays refused by the broker until expiry
                self.broker.forget_grant(grant)
            run.grant = None
            rounds, _logs = segment_rounds(chunks)
            with run.lock:
                calls = [call for call in run.calls if call.get("segment") == number]
                receipted = [*run.calls, *run.child_calls]
            touched = any(not call.get("denied") for call in calls)
            if state == "failed" and touched:
                state = "uncertain"
            if state == "cancelled" and run.limit == "seconds":
                state = "partial"
            exhausted = state == "succeeded" and rounds >= 3
            if state == "succeeded":
                claims = unreceipted_claims(response["response"],
                                            self._tool_names | set(self.mcp_organ.names()),
                                            receipted)
            record.update(state=state, rounds=rounds, tool_calls=len(calls),
                          exhausted=exhausted, seconds=round(time.monotonic() - started, 3))
            if claims:
                record["imitated"] = True
            if error:
                record["error"] = error
            try:
                self.store.finish_step(step, state, {
                    "rounds": rounds, "tool_calls": len(calls), "exhausted": exhausted,
                    "seconds": record["seconds"], "error": error,
                    "imitated": bool(claims), "claims": claims[:10],
                    "answer": ((response or {}).get("response") or "")[:2000],
                    "calls": [{"tool": call["tool"], "ok": call.get("ok"),
                               "denied": bool(call.get("denied")),
                               "inner_of": call.get("inner_of"),
                               "arguments": call.get("arguments"),
                               "result": (call.get("content") or "")[:500]}
                              for call in calls[:100]]})
            except StateError:
                pass  # left running: recovery settles it from the receipts
            run.emit("segment.finished", segment=number, state=state, rounds=rounds,
                     tool_calls=len(calls), exhausted=exhausted, seconds=record["seconds"],
                     imitated=bool(claims))
            crash_point("segment.finished", self.environ)
        return {"response": response, "exhausted": exhausted, "rounds": rounds,
                "claims": claims}

    def _admit(self, binding: RunBinding, tool: str) -> str | None:
        """The broker asks before every call: a turn's tool-call budget and wall time."""
        run = self._runs.get(binding.turn_id)
        if run is None:
            return None
        with run.lock:
            if run.cancel.is_set():
                return "The turn was cancelled, so the tool was not run."
            if run.remaining() <= 0:
                run.limit = run.limit or "seconds"
            elif run.admitted >= run.budget.max_tool_calls:
                run.limit = run.limit or "tool_calls"
            else:
                run.admitted += 1
                return None
        run.emit("limit.reached", limit=run.limit)
        return (f"This request reached its limit ({run.limit.replace('_', ' ')}), so the tool "
                "was not run. Answer the owner now: say what is done and what is not.")

    def _observe(self, binding: RunBinding, call: dict) -> None:
        run = self._runs.get(binding.turn_id)
        if run is None:
            return
        with run.lock:
            entry = {**call, "segment": run.segment}
            run.calls.append(entry)
        run.emit("tool.finished", segment=run.segment, tool=call["tool"], ok=bool(call["ok"]),
                 denied=bool(call.get("denied")), inner_of=call.get("inner_of"))

    def _end_run(self, run: _Run) -> None:
        """Stop what a finished run may still hold: its grant, in-flight calls, children."""
        if run.grant is not None:
            self.broker.cancel_grant(run.grant)
            try:
                self.authority.revoke(run.grant)
            except StateError:
                pass
        self._calls.cancel(run.turn_id)

    def _interrupt(self, run: _Run) -> None:
        """Cancel a run now: its grant, its in-flight tools, its own worker (children)."""
        run.cancel.set()
        grant = run.grant
        if grant:
            self.broker.cancel_grant(grant)
            try:
                self.authority.revoke(grant)
            except StateError:
                pass
        self._calls.cancel(run.turn_id)
        worker = run.worker
        if worker is not None:
            try:
                worker.stop(grace=1.0, timeout=4.0)
            except Exception:
                pass

    def _long_turn_evidence(self, run: _Run) -> dict:
        inner = sum(1 for call in run.calls if call.get("inner_of"))
        children_requests = sum(child.get("segments", 0) for child in run.children)
        return {"budget": run.budget.to_json(), "segments": list(run.segments),
                "children": list(run.children), "tool_calls": run.admitted,
                "inner_calls": inner, "limit": run.limit,
                "grail_requests": len(run.segments) + children_requests,
                "seconds": round(time.monotonic() - run.started, 3)}

    # -- delegation: helpers on fresh workers ---------------------------------------------
    def _delegate(self, context, tasks: list[dict], max_parallel: int | None = None) -> ToolResult:
        """Run ``tasks`` as child turns in parallel, each on a fresh Grail worker with its own
        history and at most the parent's capabilities and workspace; join every result."""
        parent = self._runs.get(context.turn_id)
        transient = parent is None
        if transient:  # the owner's own `tool delegate_tasks` call
            parent = _Run(turn_id=context.turn_id, namespace=context.namespace,
                          session_id=context.session_id, workspace=context.workspace,
                          capabilities=tuple(context.capabilities), message="",
                          budget=self.budget_defaults, cancel=context.cancelled, progress=None)
        budget = parent.budget
        # ``max_depth`` counts the levels of helpers still allowed below this run (a helper's
        # budget has one less), so the configured depth is exactly the depth reached.
        if budget.max_depth < 1:
            raise OrganError("Helpers cannot delegate further (depth limit "
                             f"{parent.depth + budget.max_depth}).")
        with parent.lock:
            room = budget.max_children - parent.child_count
            if room <= 0 or len(tasks) > room:
                raise OrganError(f"This request may start at most {budget.max_children} helpers "
                                 f"({parent.child_count} already started).")
            parent.child_count += len(tasks)
            first = parent.child_count - len(tasks) + 1
        try:
            credential = resolve_github_credential(environ=self.environ)
        except CredentialUnavailable as problem:
            raise OrganError(f"No usable Copilot credential for helpers: {problem}") from None
        parallel = max(1, min(max_parallel or budget.max_parallel, budget.max_parallel))
        allowed = set(parent.capabilities)
        if budget.max_depth < 2:  # these helpers are the last level: never offered delegation
            allowed.discard("agents.delegate")
        # A helper's task was written after whatever outside content the parent had read (web,
        # MCP, earlier helpers' answers), so every helper starts tainted by it.
        inherited = [tool for tool in self._tool_history.before(parent.turn_id)
                     if tool.startswith(OUTSIDE)]
        plans = []
        for offset, task in enumerate(tasks):
            wanted = task.get("capabilities")
            caps = tuple(item for item in (wanted if wanted else parent.capabilities)
                         if item in allowed)
            dropped = sorted(set(wanted or ()) - allowed)
            child_id = "child_" + secrets.token_hex(12)
            plans.append({"index": first + offset, "child_id": child_id, "task": task["task"],
                          "inherited": inherited,
                          "name": task.get("name") or f"helper {first + offset}",
                          "capabilities": caps, "dropped": dropped})
        # Journal every child before any of them starts.
        for plan in plans:
            self.store.begin_step(parent.namespace, parent.turn_id, "child", plan["index"], {
                "task": plan["task"][:2000], "name": plan["name"],
                "capabilities": list(plan["capabilities"]), "dropped": plan["dropped"],
                "depth": parent.depth + 1, "parent_call": context.call_id},
                step_id=plan["child_id"], parent_id=context.call_id)
        started = time.monotonic()
        slots = threading.Semaphore(parallel)
        results: dict[str, dict] = {}
        runs: dict[str, _Run] = {}
        threads = []
        stop_all = threading.Event()

        def run_child(plan: dict) -> None:
            with slots:
                results[plan["child_id"]] = self._run_child(parent, plan, credential, runs,
                                                            stop_all)

        for plan in plans:
            thread = threading.Thread(target=run_child, args=(plan,), daemon=True,
                                      name="brainstem-agent-child")
            thread.start()
            threads.append(thread)
        while any(thread.is_alive() for thread in threads):
            if (context.cancelled.is_set() or parent.cancel.is_set() or self._closed) and \
                    not stop_all.is_set():
                stop_all.set()
                stoppers = [threading.Thread(target=self._interrupt, args=(child,), daemon=True)
                            for child in list(runs.values())]
                for stopper in stoppers:
                    stopper.start()
            for thread in threads:
                thread.join(0.05)
        wall = round(time.monotonic() - started, 3)
        ordered = [results.get(plan["child_id"]) or {"state": "failed", "error": "no result",
                                                     "child_id": plan["child_id"]}
                   for plan in plans]
        serial = round(sum(item.get("seconds", 0.0) for item in ordered), 3)
        lines = []
        for plan, item in zip(plans, ordered):
            head = f"Helper {plan['index']} ({plan['name']}): {item['state']}"
            if plan["dropped"]:
                head += f" (not granted: {', '.join(plan['dropped'])})"
            body = item.get("response") or item.get("error") or "(no answer)"
            lines.append(f"{head}\n{body}")
        ok = all(item["state"] == "succeeded" for item in ordered)
        summary = (f"{sum(item['state'] == 'succeeded' for item in ordered)} of {len(ordered)} "
                   f"helpers succeeded (in parallel, {wall:g}s).")
        return ToolResult(summary + "\n\n" + "\n\n".join(lines), ok=ok, evidence={
            "children": [{key: item.get(key) for key in (
                "child_id", "state", "seconds", "segments", "tool_calls", "worker_id",
                "tool_started")} for item in ordered],
            "wall_seconds": wall, "serial_seconds": serial, "parallel": parallel})

    def _run_child(self, parent: _Run, plan: dict, credential, runs: dict,
                   stop_all: threading.Event) -> dict:
        child_id = plan["child_id"]
        cancel = threading.Event()
        if stop_all.is_set() or parent.cancel.is_set():
            cancel.set()
        run = _Run(turn_id=child_id, namespace=parent.namespace,
                   session_id="session_" + secrets.token_hex(12), workspace=parent.workspace,
                   capabilities=plan["capabilities"], message=plan["task"],
                   budget=parent.budget.for_child(parent.remaining()), cancel=cancel,
                   progress=parent._progress, depth=parent.depth + 1, root=parent.root,
                   child=True)
        runs[child_id] = run
        self._runs[child_id] = run
        self._child_turns.add(child_id)
        self._inputs[child_id] = plan["task"]
        self._tool_history.inherit(child_id, plan.get("inherited", ()))
        started = time.monotonic()
        outcome: dict[str, Any] = {"child_id": child_id, "index": plan["index"],
                                   "name": plan["name"], "state": "failed", "segments": 0}
        run.emit("child.started", index=plan["index"], name=plan["name"],
                 task=plan["task"][:200], capabilities=list(plan["capabilities"]))
        worker, error = None, None
        response = None
        ended = threading.Event()
        self._watch_deadline(run, ended, None)
        try:
            if not plan["capabilities"]:
                raise OrganError("The helper would have no capability the parent holds.")
            worker = self._factory(worker_id="c" + secrets.token_hex(4),
                                   broker_url=self.broker.url, credential=credential,
                                   model=self.model, register=self.broker.register_worker)
            run.worker = worker
            if cancel.is_set():
                raise _Cancelled()
            worker.start()
            outcome["worker_id"] = worker.worker_id
            crash_point("child.started", self.environ)

            def own_worker():
                if not worker.alive():
                    raise WorkerError("The helper's Grail worker stopped.")
                return worker

            response = self._segments(run, [], own_worker, [credential.value])
            outcome["state"] = "succeeded" if response is not None else "partial"
        except _Cancelled:
            outcome["state"] = "cancelled"
        except (WorkerError, grail.GrailSourceError, sandbox.SandboxUnavailable, GrantDenied,
                OrganError, HostError, StateError) as problem:
            error = self._redact(str(problem), [credential.value])
        except (OSError, ValueError) as problem:
            error = f"The helper's worker stopped or its stream broke ({type(problem).__name__})."
        finally:
            ended.set()
            self._end_run(run)
            if worker is not None:
                try:
                    worker.stop(grace=1.0, timeout=4.0)
                finally:
                    self.broker.unregister_worker(worker.worker_id)
            try:
                receipts = self._settled_receipts(parent.namespace, child_id,
                                                  stuck=run.unrecorded.get("unfinished", 0))
            except StateError:
                receipts = []
            self._calls.release(child_id)
            self._inputs.pop(child_id, None)
            self._tool_history.drop(child_id)
            self._context_reports.pop(child_id, None)
            self._runs.pop(child_id, None)
            self._child_turns.discard(child_id)
            touched = any(r["state"] != "denied" for r in receipts)
            unsettled = any(r["state"] == "started" for r in receipts)
            if cancel.is_set() and outcome["state"] != "succeeded":
                outcome["state"] = "cancelled"
            elif unsettled:
                outcome["state"] = "uncertain"
            elif run.limit or outcome["state"] == "partial":
                outcome["state"] = "partial"
                last = (response or {}).get("response") or run.last_answer
                outcome["response"] = partial_text(run.limit or "segments", run.budget,
                                                   run.calls, last, rejected=run.rejected)
            elif outcome["state"] == "failed" and touched:
                outcome["state"] = "uncertain"
            if outcome["state"] == "succeeded":
                outcome["response"] = (response or {}).get("response", "")[:4000]
            if error:
                outcome["error"] = error
            outcome.update(segments=run.segment, tool_calls=run.admitted,
                           seconds=round(time.monotonic() - started, 3), tool_started=touched,
                           receipts=[f"{r['tool']}:{r['state']}" for r in receipts][:40],
                           segment_detail=list(run.segments))
            parent.children.append({key: outcome.get(key) for key in (
                "child_id", "index", "name", "state", "seconds", "segments", "tool_calls",
                "worker_id", "tool_started", "segment_detail")})
            with parent.lock:
                parent.child_calls.extend([*run.calls, *run.child_calls])
            try:
                self.store.finish_step(child_id, outcome["state"], {
                    key: outcome.get(key) for key in (
                        "state", "response", "error", "segments", "tool_calls", "seconds",
                        "worker_id", "tool_started", "receipts")})
            except StateError:
                pass  # left running: recovery settles it from the receipts
            run.emit("child.finished", index=plan["index"], state=outcome["state"],
                     seconds=outcome["seconds"], segments=run.segment)
            crash_point("child.finished", self.environ)
        return outcome

    @staticmethod
    def _context_summary(report: dict | None) -> dict | None:
        """What the learned-context block held (ids and sizes, never the text)."""
        if not report or "sections" not in report:
            return report
        keep = ("chars", "allowance", "need", "items", "shown", "omitted", "truncated", "keys",
                "files", "blocks", "shown_blocks")
        return {"budget": report["budget"], "used": report["used"], "sections": {
            name: {key: value for key, value in section.items() if key in keep}
            for name, section in report["sections"].items()}}

    @staticmethod
    def _stream_error(chunks: list[str]) -> str:
        for line in chunks:
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("type") == "error":
                    return str(payload.get("error") or "unknown error")[:300]
        return ""

    def _settled_receipts(self, namespace: str, turn_id: str, *, stuck: int = 0) -> list[dict]:
        """The turn's receipts once none is ``started`` any more: its tools have stopped.

        ``stuck`` receipts are known never to settle (their outcome could not be written)."""
        deadline = time.monotonic() + _SETTLE_SECONDS
        while True:
            receipts = self.store.list_receipts(namespace, turn_id=turn_id, limit=None)
            started = sum(1 for r in receipts if r["state"] == "started")
            if started <= stuck or time.monotonic() >= deadline:
                return receipts
            time.sleep(0.02)

    def _cancel_turn(self, turn: dict) -> dict | None:
        """Cancel a turn: its grant, its tools, every helper (in parallel) and its worker."""
        turn["cancel"].set()
        self._calls.cancel(turn["turn_id"])
        grant = turn.get("grant")
        if grant:
            self.broker.cancel_grant(grant)
            try:
                self.authority.revoke(grant)
            except StateError:
                pass
        helpers = [threading.Thread(target=self._interrupt, args=(run,), daemon=True)
                   for run in list(self._runs.values())
                   if run.child and run.root == turn["turn_id"]]
        for helper in helpers:
            helper.start()
        stopped = None
        if self._turn is turn:
            stopped = self._discard_worker(grace=1.0, timeout=4.0)
        for helper in helpers:
            helper.join(5.0)
        return stopped

    def _watch_deadline(self, run: _Run, ended: threading.Event, turn: dict | None) -> None:
        """At the run's wall-time limit, stop its segment (limit ``seconds``): the grant,
        its tools and its worker. The turn then ends with a partial result."""
        def watch() -> None:
            if ended.wait(max(0.0, run.remaining())):
                return
            run.limit = run.limit or "seconds"
            run.emit("limit.reached", limit="seconds")
            if run.grant:
                self.broker.cancel_grant(run.grant)
            self._calls.cancel(run.turn_id)
            if run.child:
                if run.worker is not None:
                    try:
                        run.worker.stop(grace=0.5, timeout=3.0)
                    except Exception:
                        pass
            elif turn is not None and self._turn is turn:
                self._discard_worker(grace=0.5, timeout=3.0)

        threading.Thread(target=watch, daemon=True, name="brainstem-agent-deadline").start()

    def cancel(self) -> dict:
        """Cancel the active turn and every in-flight tool call (owner-initiated ones too)."""
        started = time.monotonic()
        turn = self._turn
        self._calls.cancel()
        if turn is None:
            return {"cancelled": False, "reason": "no active turn"}
        stopped = self._cancel_turn(turn) or {}
        return {"cancelled": True, "turn_id": turn["turn_id"],
                "seconds": round(time.monotonic() - started, 3),
                "group_gone": stopped.get("group_gone", True)}

    # -- direct tools and queries ----------------------------------------------------
    def invoke_tool(self, tool: str, arguments: Mapping[str, Any],
                    capabilities: Sequence[str] | None = None, *,
                    cancel_event: threading.Event | None = None,
                    workspace: Path | str | None = None) -> dict:
        """Owner-initiated tool call; setting ``cancel_event`` cancels it like a turn's tools."""
        caps = self._capabilities(capabilities)
        turn_id = "direct_" + secrets.token_hex(8)
        binding = RunBinding(OWNER, str(self._workspace(workspace)), "direct", turn_id, "owner",
                             "direct", caps)
        if tool.startswith("mcp__"):  # start only the server this tool belongs to
            self.mcp_organ.prepare([cap for cap in caps if tool.startswith(f"mcp__{cap[4:]}__")])
        done = threading.Event()
        if cancel_event is not None:
            if cancel_event.is_set():
                self._calls.cancel(turn_id)
            _when_set(cancel_event, lambda: self._calls.cancel(turn_id), done)
        try:
            result = self.broker.invoke_direct(binding, tool, arguments)
        finally:
            done.set()
            self._calls.release(turn_id)
            self._tool_history.drop(turn_id)
        content = result.get("content") or result.get("error") or ""
        if result.get("recorded", True):
            return {"ok": bool(result.get("ok")), "content": content, "turn_id": turn_id}
        # Its receipt could not be written: never reported as success.
        return {"ok": False, "content": content, "turn_id": turn_id, "recorded": False}

    def memory(self, query: str | None = None, *, scope: str = "all") -> list[dict]:
        """Facts of this workspace and the owner's profile (``scope``), each with its scope."""
        chosen = [(name, namespace) for name, namespace in
                  (("workspace", self.namespace), ("profile", self.profile_namespace))
                  if scope in ("all", name)]
        facts = []
        for name, namespace in chosen:
            listed = self.store.list_facts(namespace)
            if query:
                ranked = rank(query, knowledge.fact_items(listed), now=time.time())
                listed = [entry.item.data for entry in ranked if entry.matched]
            facts += [{**fact, "scope": name} for fact in listed]
        return facts

    def sessions(self) -> list[dict]:
        return self.store.list_sessions(self.namespace)

    def forget_session(self, session_id: str) -> dict:
        """Forget one of this workspace's sessions (``Store.forget_session``: the owner's
        words, answers, scheduled runs' inbox answers, receipts' payloads) and drop its turns
        from the search index."""
        forgotten = self.store.forget_session(self.namespace, session_id)
        forgotten["search_index_removed"] = self.session_index.forget(forgotten["turns"])
        return forgotten

    def receipts(self, turn_id: str | None = None) -> list[dict]:
        """This workspace's receipts (newest 100), or one turn's and its helpers'. A helper's
        receipt names its parent turn (``parent_turn``)."""
        if turn_id is None:
            receipts = self.store.list_receipts(self.namespace)
        else:
            receipts = self.store.journal(self.namespace, turn_id)["receipts"]
        parents = self.store.step_parents(
            sorted({r["turn_id"] for r in receipts if r["turn_id"].startswith("child_")}))
        return [{**r, "parent_turn": parents[r["turn_id"]]} if r["turn_id"] in parents else r
                for r in receipts]

    def close(self) -> dict | None:
        """Cancel the active turn and in-flight tools, wait for them to stop, then shut down."""
        if self._closed:
            return self.last_stop
        self._closed = True
        self._calls.cancel(close=True)
        deadline = time.monotonic() + 30.0
        while True:
            turn = self._turn
            if turn is not None and not turn["cancel"].is_set():
                self._cancel_turn(turn)
            if self._turn_lock.acquire(timeout=0.05):
                self._turn_lock.release()
                break
            if time.monotonic() >= deadline:
                break
        self._calls.wait_idle(_SETTLE_SECONDS)
        # Helpers of an owner's direct delegate call, then every background process.
        for run in list(self._runs.values()):
            if run.child:
                self._interrupt(run)
        self.process_organ.close()
        self.mcp_organ.close()
        evidence = self._discard_worker()
        self.supervisor.close()
        self.broker.stop()
        self.session_index.close()
        self.store.close()
        return evidence

    # -- doctor --deep -----------------------------------------------------------------
    def diagnose(self) -> dict:
        """Start a real probe worker and prove the bridge, grants and membrane from inside Grail."""
        import socket

        try:
            credential = resolve_github_credential(environ=self.environ)
        except CredentialUnavailable:
            credential = None
        nonce = secrets.token_hex(16)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        escape = self.home / f"probe-escape-{secrets.token_hex(4)}"
        probe_file = self.workspace / f".brainstem-agent-probe-{secrets.token_hex(4)}"
        probe_file.write_text("probe")
        targets = {
            "nonce": nonce, "other_port": listener.getsockname()[1],
            "read": {
                "read_state_db": str(self.home / "state" / "agent.sqlite3"),
                "read_installed_credential": str(Path(credential.path) if credential
                                                 else installed_credential_path(self.environ)),
                "read_owner_home": str(Path(os.path.realpath(os.path.expanduser("~")))),
                "read_workspace": str(probe_file),
            },
            "write_new": {"write_outside": str(escape)},
        }
        report: dict[str, Any] = {"ok": False}
        worker = None
        try:
            worker = self._grail_worker(worker_id="doctor" + secrets.token_hex(2),
                                        broker_url=self.broker.url, credential=credential,
                                        model=self.model, register=self.broker.register_worker,
                                        probe=targets)
            started = worker.start()
            report["worker"] = {"started": True, "pid": started["pid"],
                                "generation": started["generation"],
                                "health": started["health"],
                                "start_seconds": started["start_seconds"]}
            report["integrity"] = {"before": started["integrity_before"]}

            def tools(grant: str | None) -> list[str]:
                headers = {"X-Brainstem-Agent-Grant": grant} if grant else {}
                return sorted(worker.health(headers).get("agents", []))

            def grant_for(generation: str, ttl: float = 60) -> str:
                return self.authority.issue(RunBinding(
                    OWNER, str(self.workspace), "doctor", "doctor_" + secrets.token_hex(6),
                    worker.worker_id, generation, CORE_CAPABILITIES), ttl=ttl)

            valid = grant_for(worker.generation)
            revoked = grant_for(worker.generation)
            self.authority.revoke(revoked)
            expired = grant_for(worker.generation, ttl=1)
            other = grant_for("g-other-worker")
            time.sleep(1.1)
            bridge = {"no_grant_tools": tools(None), "granted_tools": tools(valid),
                      "binds": self.broker.bind_count(valid), "refused_grants": {
                          "forged": tools("forged-" + secrets.token_urlsafe(24)),
                          "revoked": tools(revoked), "expired": tools(expired),
                          "other_worker": tools(other)}}
            self.authority.revoke(valid)
            report["bridge"] = bridge
            worker.health({"X-Brainstem-Agent-Probe": nonce})
            probe_path = worker.tree / "home" / "probe.json"
            membrane = json.loads(probe_path.read_text()) if probe_path.exists() else {
                "probe_error": "no probe result"}
            report["membrane"] = membrane
        except (WorkerError, grail.GrailSourceError, sandbox.SandboxUnavailable, OSError,
                ValueError) as error:
            report["error"] = self._redact(str(error), [credential.value] if credential else [])
        finally:
            listener.close()
            probe_file.unlink(missing_ok=True)
            if escape.exists():
                report.setdefault("membrane", {})["write_outside"] = "allowed"
                escape.unlink()
            if worker is not None:
                stopped = worker.stop()
                self.broker.unregister_worker(worker.worker_id)
                report.setdefault("integrity", {})["after"] = stopped["integrity_after"]
                report["stop"] = {k: stopped[k] for k in ("seconds", "exit_code", "group_gone")}
        expected_tools = sorted(spec.name for spec in self.broker.tool_specs(CORE_CAPABILITIES))
        membrane = report.get("membrane", {})
        bridge = report.get("bridge", {})
        report["ok"] = bool(
            "error" not in report
            and report.get("worker", {}).get("health") == "ok"
            and bridge.get("no_grant_tools") == []
            and bridge.get("granted_tools") == expected_tools
            and all(value == ["brainstem_agent_status"]
                    for value in bridge.get("refused_grants", {}).values())
            and membrane.get("connect_broker") == "allowed"
            and all(value in ("denied", "absent") for key, value in membrane.items()
                    if key != "connect_broker")
            and report.get("integrity", {}).get("before", {}).get("ok")
            and report.get("integrity", {}).get("after", {}).get("ok")
            and report.get("stop", {}).get("group_gone"))
        return report
