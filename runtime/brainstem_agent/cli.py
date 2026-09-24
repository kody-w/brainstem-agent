"""Brainstem Agent command line: setup, doctor, chat, tool, memory, profile, skills, sessions,
context, receipts, turns, processes, cancel, serve, status, stop, schedules, mcp, egress,
inbox, service, the owner surfaces: repl (also ``brainstem-agent`` with no command), open
(the web companion's one-time sign-in link) and api (any documented daemon route), and the
operations commands version, backup, restore, export, upgrade, rollback, uninstall, prune,
compact, logs and stats.

Every command supports ``--json`` so an AI agent can drive it headlessly. Human
output presents Brainstem Agent; credentials are only ever named by source.

While a daemon (``serve``) runs for the home, ``chat`` and ``tool`` go through it
(warm worker); otherwise they run in-process exactly as before. Schedule changes
are written to the store and wake the daemon's loop.

SIGINT and SIGTERM during ``chat`` or ``tool`` are cancellation requests: the
handler only sets an event, the host stops the running tool's process group
(and the worker's), and the command exits 4. The work runs on a helper thread
while the main thread waits on an Event in short slices, so handlers run
promptly (CPython runs them only on the main thread) and nothing depends on
``Thread.join()`` behaviour after KeyboardInterrupt, which is unreliable on 3.11.
If the CLI is SIGKILLed, the host's lifeline watchdog kills those groups.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import api as api_routes
from . import backup as backups
from . import (daemon, grail, health, hygiene, knowledge, lifecycle, observe, release, schedules,
               views)
from .host import (ALL_CAPABILITIES, OWNER, TURN_CAPABILITIES, AgentHost,
                   HostCancelled, HostError, TurnResult, cell_namespace)
from .longturn import TurnBudget
from .organs.mcp import configured_servers, server_specs
from .organs.skills import normalize_name, parse_skill, render_skill
from .organs.base import OrganError
from .organs.web import EgressLog, egress_policy, read_config
from .retrieval import query_text
from .paths import canonical
from .session_index import format_hits
from .credentials import redact_credentials
from .state import StateError, Store

EXIT = {"succeeded": 0, "failed": 1, "uncertain": 3, "cancelled": 4, "partial": 5}


def _home(environ) -> Path:
    return Path(environ.get("BRAINSTEM_AGENT_HOME") or os.path.expanduser("~/.brainstem-agent"))


def _cache(environ) -> Path:
    return Path(environ.get("BRAINSTEM_AGENT_CACHE") or _home(environ) / "cache")


def _emit(arguments, document: dict, human: str, *, error: bool = False) -> None:
    if arguments.json:
        print(json.dumps(document, indent=2))
    elif human:
        print(human, file=sys.stderr if error else sys.stdout)


def _host(arguments, environ) -> AgentHost:
    return AgentHost(_home(environ), workspace=getattr(arguments, "workspace", None),
                     cache=_cache(environ), environ=environ)


@contextmanager
def _signals_cancel(cancel: threading.Event):
    """SIGINT/SIGTERM set ``cancel`` (repeats are harmless) instead of raising."""
    def handler(_signum, _frame):
        cancel.set()

    previous = {signum: signal.signal(signum, handler) for signum in (signal.SIGINT,
                                                                       signal.SIGTERM)}
    try:
        yield
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


def _on_helper_thread(work):
    """Run ``work()`` on a helper thread; the main thread waits on an Event in slices."""
    finished = threading.Event()
    outcome: dict = {}

    def run():
        try:
            outcome["value"] = work()
        except BaseException as error:  # re-raised on the main thread
            outcome["error"] = error
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True, name="brainstem-agent-command").start()
    while not finished.wait(0.05):
        pass
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _workspace_arg(arguments, environ) -> str | None:
    chosen = getattr(arguments, "workspace", None) or environ.get("BRAINSTEM_AGENT_WORKSPACE")
    return os.path.realpath(chosen) if chosen else None


def _remote(client: daemon.Client, path: str, body: dict, cancel: threading.Event,
            timeout: float, on_event=None) -> dict:
    """Send one daemon request; SIGINT/SIGTERM (``cancel``) cancels it on the daemon.
    ``on_event`` receives the turn's progress events (polled from ``/v1/progress``)."""
    body = {**body, "request_id": secrets.token_hex(8)}
    done = threading.Event()
    seen = [0]

    def poll() -> None:
        try:
            answer = client.call("POST", "/v1/progress", {"request_id": body["request_id"],
                                                         "after": seen[0]}, timeout=5)
        except daemon.DaemonError:
            return
        for seq, event in answer.get("events", []):
            if seq > seen[0]:
                seen[0] = seq
                on_event(event)

    def follow() -> None:
        while not done.wait(0.3):
            poll()

    if on_event is not None:
        threading.Thread(target=follow, daemon=True, name="brainstem-agent-progress").start()

    def watch() -> None:
        while not done.is_set():
            if cancel.wait(0.05):
                if not done.is_set():
                    try:
                        client.call("POST", "/v1/cancel", {"request_id": body["request_id"]},
                                    timeout=5)
                    except daemon.DaemonError:
                        pass
                return

    threading.Thread(target=watch, daemon=True, name="brainstem-agent-remote-cancel").start()
    try:
        return _on_helper_thread(lambda: client.call("POST", path, body, timeout=timeout))
    finally:
        done.set()
        if on_event is not None:
            poll()


def cmd_setup(arguments, environ) -> int:
    cache = _cache(environ)
    seed = environ.get("BRAINSTEM_AGENT_GRAIL_SEED")
    try:
        source = grail.ensure_grail_source(cache, seed_dir=Path(seed) if seed else None, fetch=True)
        python = grail.ensure_worker_venv(cache, environ=environ)
    except grail.GrailSourceError as error:
        _emit(arguments, {"ok": False, "error": str(error)}, f"Brainstem Agent setup failed: {error}",
              error=True)
        return 1
    document = {"ok": True, "grail": {"commit": source.commit, "files": len(source.inventory),
                                      "origin": "seed" if seed else "cache-or-codeload"},
                "worker_interpreter": grail.check_worker_venv(cache, environ=environ),
                "health": health.summarize(health.installation_checks(
                    _home(environ), cache, environ, import_check=False))}
    human = f"Brainstem Agent is set up (Grail {grail.VERSION}, {python})."
    if not document["health"]["ready"]:
        human += "\n" + health.render(document["health"], "Next")
    _emit(arguments, document, human)
    return 0


def doctor_report(environ, *, deep: bool = False, workspace=None) -> dict:
    """The installation's readiness through the health model: every check with a reason and
    a fix (``checks`` keeps its earlier shape), the daemon's liveness and readiness when it
    runs, the reaching cell's configuration, and with ``deep`` a probe worker."""
    home, cache = _home(environ), _cache(environ)
    checks = health.installation_checks(home, cache, environ)
    summary = health.summarize(checks)
    by_id = {check.id: check for check in checks}
    report = {"product": "Brainstem Agent", "ready": summary["ready"],
              "checks": {check.id: check.legacy() for check in checks}, "health": summary}
    record = daemon.read_record(home)
    report["daemon"] = {"running": record is not None,
                        "pid": record["pid"] if record else None}
    if record is not None:
        try:
            live = daemon.Client(record).call("GET", "/v1/health", timeout=10)
            report["daemon"].update(live=live.get("live"), ready=live.get("ready"),
                                    failing=live.get("failing"), version=live.get("version"))
        except daemon.DaemonError as error:
            report["daemon"].update(live=False, error=str(error))
    # The reaching cell's reach.json (optional: problems here never block readiness).
    path = home / "reach.json"
    config, error = read_config(path)
    servers, problems = server_specs(config)
    try:
        egress_policy(config)
    except OrganError as invalid:
        problems.append(str(invalid))
    report["reach"] = {"config": str(path), "exists": path.exists(), "mcp_servers": sorted(servers),
                       "problems": [error] if error else problems}
    if deep:
        if all(by_id[name].ok for name in ("grail_source", "worker_interpreter", "sandbox")):
            try:  # a workspace the guard refuses is a failing check, not a traceback
                host = AgentHost(home, workspace=workspace, cache=cache, environ=environ)
            except (HostError, StateError) as error:
                host, report["deep"] = None, {"ok": False, "error": str(error)}
            if host is not None:
                try:
                    with host.exclusive():
                        report["deep"] = host.diagnose()
                except HostError as error:
                    report["deep"] = {"ok": False, "error": f"{error} (stop the daemon first)"}
                finally:
                    host.close()
        else:
            report["deep"] = {"ok": False, "error": "prerequisites are not ready"}
        report["ready"] = report["ready"] and report["deep"]["ok"]
    report["problems"] = [f"{check.id}: {check.reason}" for check in checks
                          if not check.ok and check.required]
    if deep and not report["deep"]["ok"]:
        report["problems"].append("deep: " + str(report["deep"].get("error") or
                                                 "membrane, bridge or integrity check failed"))
    return report


def cmd_doctor(arguments, environ) -> int:
    report = doctor_report(environ, deep=arguments.deep, workspace=arguments.workspace)
    if report["ready"]:
        human = "Brainstem Agent is ready."
        if report["health"]["advisories"]:
            human += "\n" + health.render(report["health"], "Advisories").split("\n", 1)[-1]
    else:
        human = "Brainstem Agent is not ready:\n" + "\n".join(
            line for line in health.render(report["health"]).splitlines()[1:])
        if report.get("deep") and not report["deep"].get("ok"):
            human += "\n  - deep: " + str(report["deep"].get("error") or
                                           "membrane, bridge or integrity check failed")
    _emit(arguments, report, human, error=not report["ready"])
    return 0 if report["ready"] else 1


_PROGRESS_LOCK = threading.Lock()


def progress_line(event: dict) -> str | None:
    """One human line for a long turn's progress event (None: not shown)."""
    who = f"helper {event.get('index', '')} ".replace("  ", " ") if event.get("child") else ""
    kind = event.get("event")
    if kind == "segment.started":
        return (f"{who}step {event['segment']} started" +
                (" (continuing: Grail ran out of tool rounds)" if event.get("continuation")
                 else ""))
    if kind == "segment.finished":
        return (f"{who}step {event['segment']} {event['state']}: {event['rounds']} tool "
                f"round(s), {event['tool_calls']} call(s), {event['seconds']:.1f}s")
    if kind == "tool.finished":
        mark = "refused" if event.get("denied") else "ok" if event.get("ok") else "failed"
        inner = "  (script) " if event.get("inner_of") else ""
        return f"{who}{inner}{event['tool']} {mark}"
    if kind == "child.started":
        return f"helper {event['index']} started: {event.get('task', '')[:80]}"
    if kind == "child.finished":
        return f"helper {event['index']} {event['state']} in {event['seconds']:.1f}s"
    if kind == "limit.reached":
        return f"{who}limit reached: {event.get('limit')}"
    if kind == "turn.continuing" and event.get("reason") == "unreceipted-tool-log":
        return f"{who}answer not accepted: it wrote tool calls out with no receipt; continuing"
    if kind == "turn.finished":
        return f"turn {event['state']} after {event['segments']} step(s), {event['seconds']:.1f}s"
    return None


def _progress_sink(arguments):
    """Progress to stderr: JSON lines with --json, short text otherwise; None with --quiet."""
    if getattr(arguments, "quiet", False):
        return None

    def sink(event: dict) -> None:
        line = json.dumps(event) if arguments.json else progress_line(event)
        if line:
            with _PROGRESS_LOCK:
                print(line if arguments.json else "· " + line, file=sys.stderr, flush=True)
    return sink


def _budget(arguments, environ) -> TurnBudget:
    return TurnBudget.from_env(environ, max_segments=arguments.max_segments,
                               max_tool_calls=arguments.max_tool_calls,
                               max_seconds=arguments.max_seconds)


def cmd_chat(arguments, environ) -> int:
    capabilities = arguments.capabilities.split(",") if arguments.capabilities else None
    cancel = threading.Event()
    try:
        budget = _budget(arguments, environ)
    except ValueError as error:
        _emit(arguments, {"ok": False, "state": "failed", "error": str(error)},
              f"Brainstem Agent: {error}", error=True)
        return 2
    sink = _progress_sink(arguments)

    def turn(host: AgentHost) -> TurnResult:
        try:
            with host.exclusive(cancel=cancel):
                return host.chat(arguments.message, session_id=arguments.session,
                                 idempotency_key=arguments.idempotency_key,
                                 capabilities=capabilities, timeout=arguments.timeout,
                                 cancel_event=cancel, budget=budget, progress=sink)
        except HostCancelled as error:
            return TurnResult(False, "cancelled", False, None, arguments.session, None,
                              str(error), {"worker": None, "grail_calls": 0})

    with _signals_cancel(cancel):
        result = None
        client = daemon.connect(_home(environ))
        if client is not None:
            try:
                result = TurnResult(**_remote(client, "/v1/turn", {
                    "message": arguments.message, "session_id": arguments.session,
                    "idempotency_key": arguments.idempotency_key, "capabilities": capabilities,
                    "timeout": arguments.timeout, "workspace": _workspace_arg(arguments, environ),
                    "budget": budget.to_json()},
                    cancel, max(2 * arguments.timeout + 60, budget.max_seconds + 120),
                    on_event=sink))
            except daemon.DaemonUnavailable:
                result = None  # a stale record: nothing was sent, run in-process
            except (daemon.DaemonError, TypeError) as error:
                _emit(arguments, {"ok": False, "state": "failed", "error": str(error)},
                      f"Brainstem Agent: {error}", error=True)
                return 1
        if result is None:
            try:
                host = _host(arguments, environ)
            except HostError as error:
                _emit(arguments, {"ok": False, "state": "failed", "error": str(error)},
                      f"Brainstem Agent: {error}", error=True)
                return 1
            try:
                result = _on_helper_thread(lambda: turn(host))
            except HostError as error:
                _emit(arguments, {"ok": False, "state": "failed", "error": str(error)},
                      f"Brainstem Agent: {error}", error=True)
                return 1
            finally:
                host.close()
    document = result.to_json()
    if result.ok:
        _emit(arguments, document, result.response["response"])
    elif result.state == "partial":
        _emit(arguments, document, f"Brainstem Agent: {result.error}", error=True)
    else:
        _emit(arguments, document, f"Brainstem Agent: {result.state}: {result.error}", error=True)
    return EXIT.get(result.state, 1)


def cmd_tool(arguments, environ) -> int:
    try:
        payload = json.loads(arguments.arguments)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        _emit(arguments, {"ok": False, "error": "--arguments must be a JSON object"},
              "Brainstem Agent: --arguments must be a JSON object", error=True)
        return 2
    capabilities = arguments.capabilities.split(",") if arguments.capabilities else None
    cancel = threading.Event()

    def call(host: AgentHost) -> dict:
        with host.exclusive(cancel=cancel):
            return host.invoke_tool(arguments.name, payload, capabilities=capabilities,
                                    cancel_event=cancel)

    with _signals_cancel(cancel):
        result, host = None, None
        client = daemon.connect(_home(environ))
        if client is not None:
            try:
                result = _remote(client, "/v1/tool", {
                    "name": arguments.name, "arguments": payload, "capabilities": capabilities,
                    "workspace": _workspace_arg(arguments, environ)}, cancel, 900)
            except daemon.DaemonUnavailable:
                result = None
            except daemon.DaemonError as error:
                result = {"ok": False, "content": str(error)}
        if result is None:
            try:
                host = _host(arguments, environ)
            except HostError as error:
                host, result = None, {"ok": False, "content": str(error)}
        if host is not None:
            try:
                result = _on_helper_thread(lambda: call(host))
            except HostError as error:
                result = {"ok": False, "content": str(error)}
            finally:
                host.close()
    cancelled = cancel.is_set() and not result["ok"]
    if cancelled:
        result["cancelled"] = True
    _emit(arguments, result, result["content"], error=not result["ok"])
    return EXIT["cancelled"] if cancelled else (0 if result["ok"] else 1)


def _query(arguments, environ, key: str, fetch) -> int:
    host = _host(arguments, environ)
    try:
        items = fetch(host)
    finally:
        host.close()
    human = "\n".join(json.dumps(item) for item in items) or f"(no {key})"
    _emit(arguments, {key: items, "workspace": str(host.workspace)}, human)
    return 0


def _fail(arguments, error) -> int:
    _emit(arguments, {"ok": False, "error": str(error)}, f"Brainstem Agent: {error}", error=True)
    return 1


# One implementation for the CLI and the daemon's API routes (views.py).
_scope_label = views.scope_label
_skill_summary = views.skill_summary
_find_skill = views.find_skill


def _skill_fields(arguments, current: dict | None) -> dict:
    fields = dict(parse_skill(Path(arguments.file).read_text())) if arguments.file else {}
    if arguments.description:
        fields["description"] = arguments.description
    if arguments.when_to_use:
        fields["when_to_use"] = arguments.when_to_use
    if arguments.step:
        fields["steps"] = list(arguments.step)
    for key in ("description", "when_to_use", "steps"):
        if not fields.get(key) and current is not None:
            fields[key] = current[key]
    return fields


def cmd_skills(arguments, environ) -> int:
    """Owner governance of learned skills: list, show, history, approve, reject, disable,
    enable, edit, import, export, share, unshare and delete (model skills are data)."""
    action = arguments.action
    if action not in ("list", "import") and not arguments.name:
        return _fail(arguments, f"skills {action} needs a skill name")
    host = _host(arguments, environ)
    try:
        store = host.store
        if action == "list":
            items = [_skill_summary(host, skill) for skill in
                     store.list_skills([host.namespace, host.profile_namespace])]
            if arguments.offered:
                items = [item for item in items if item["offered"]]
            _emit(arguments, {"skills": items, "workspace": str(host.workspace)},
                  "\n".join(f"{i['name']} ({i['scope']}, {i['state']}, {i['review']}, "
                            f"v{i['version']}{', pending v' + str(i['pending']) if i['pending'] else ''})"
                            f": {i['description']}" for i in items) or "(no skills)")
            return 0
        if action == "import":
            if not arguments.file:
                return _fail(arguments, "skills import needs --file <skill.md>")
            fields = _skill_fields(arguments, None)
            name = normalize_name(arguments.name or fields.get("name"))
            scope = host.profile_namespace if arguments.shared else host.namespace
            saved = store.save_skill(scope, name, description=fields.get("description"),
                                     when_to_use=fields.get("when_to_use"),
                                     steps=fields.get("steps"), author="owner", review="approved",
                                     note=arguments.note or "imported by the owner",
                                     workspace=str(host.workspace), allow_disabled=True)
            _emit(arguments, {"ok": True, "skill": _skill_summary(host, saved)},
                  f"Imported skill {name} (version {saved['version']}, approved).")
            return 0
        skill = _find_skill(host, arguments.name)
        if action == "show":
            shown = _find_skill(host, arguments.name, arguments.version) if arguments.version \
                else skill
            _emit(arguments, {"skill": {**_skill_summary(host, skill), "shown_version":
                                        shown["shown_version"], "steps": shown["steps"],
                                        "shown_description": shown["description"],
                                        "shown_when_to_use": shown["when_to_use"],
                                        "version_review": shown["version_review"],
                                        "note": shown["note"]},
                              "markdown": render_skill(shown, scope_label=_scope_label(host, shown))},
                  render_skill(shown, scope_label=_scope_label(host, shown)))
            return 0
        if action == "history":
            versions = store.skill_history(skill["skill_id"])
            _emit(arguments, {"name": skill["name"], "current_version": skill["version"],
                              "pending": skill["pending"], "versions": versions},
                  "\n".join(f"v{v['version']} {v['review']} by {v['author']}"
                            f"{' (tainted)' if v['tainted'] else ''}: {v['note'] or ''}"
                            for v in versions))
            return 0
        if action == "export":
            text = render_skill(skill, scope_label=_scope_label(host, skill))
            if arguments.output:
                with Path(arguments.output).open("x", encoding="utf-8") as handle:
                    handle.write(text)
            _emit(arguments, {"ok": True, "name": skill["name"], "markdown": text,
                              "output": str(arguments.output) if arguments.output else None},
                  text if not arguments.output else f"Exported to {arguments.output}.")
            return 0
        if action == "delete":
            deleted = store.delete_skill(skill["skill_id"])
            _emit(arguments, {"ok": deleted, "deleted": skill["name"]},
                  f"Deleted skill {skill['name']} and every version.")
            return 0 if deleted else 1
        if action == "edit":
            fields = _skill_fields(arguments, skill)
            changed = store.save_skill(skill["scope"], skill["name"], **{
                key: fields[key] for key in ("description", "when_to_use", "steps")},
                author="owner", review="approved", note=arguments.note or "edited by the owner",
                workspace=str(host.workspace), allow_disabled=True)
        elif action in views.REVIEW_ACTIONS:  # the companion's review buttons call the same
            changed = views.skill_review(host, skill["name"], action,
                                         arguments.version)["skill"]
            changed = _find_skill(host, changed["name"])
        elif action in ("share", "unshare"):
            changed = store.set_skill(skill["skill_id"], scope=host.profile_namespace
                                      if action == "share" else host.namespace)
        else:
            return _fail(arguments, f"unknown skills action {action}")
        summary = _skill_summary(host, changed)
        held = (f" Version {summary['pending']} is still pending review: see it with `skills "
                f"show {summary['name']} --version {summary['pending']}`, then approve it with "
                f"--version {summary['pending']} or reject it." if summary["pending"] else "")
        _emit(arguments, {"ok": True, "skill": summary},
              f"{summary['name']}: {summary['state']}, {summary['review']}, version "
              f"{summary['version']} ({summary['scope']}).{held}")
        return 0
    except (StateError, OrganError, OSError) as error:
        return _fail(arguments, error)
    finally:
        host.close()


def cmd_profile(arguments, environ) -> int:
    """The owner's profile facts: offered in every workspace, separate from workspace memory."""
    host = _host(arguments, environ)
    try:
        store, scope = host.store, host.profile_namespace
        if arguments.action == "list":
            facts = store.list_facts(scope)
            _emit(arguments, {"facts": facts}, "\n".join(
                f"[{fact['fact_id']}] {fact['text']}" for fact in facts) or "(no profile facts)")
            return 0
        if arguments.action == "add":
            if not arguments.text:
                return _fail(arguments, "profile add needs --text")
            fact = store.add_fact(scope, arguments.text, source_turn=None)
        elif not arguments.id:
            return _fail(arguments, f"profile {arguments.action} needs a fact id")
        elif arguments.action == "edit":
            if not arguments.text:
                return _fail(arguments, "profile edit needs --text")
            fact = store.update_fact(scope, arguments.id, arguments.text)
        else:
            if not store.delete_fact(scope, arguments.id):
                return _fail(arguments, f"No profile fact {arguments.id}.")
            _emit(arguments, {"ok": True, "forgotten": arguments.id}, "Forgotten.")
            return 0
        _emit(arguments, {"ok": True, "fact": fact}, f"[{fact['fact_id']}] {fact['text']}")
        return 0
    except StateError as error:
        return _fail(arguments, error)
    finally:
        host.close()


def _live_turns(host: AgentHost, environ) -> set[str] | None:
    """The turns that may be running now, to tell running from stale: the daemon's active
    turn when one runs (it holds the home), else what ``views.live_turns`` can tell."""
    client = daemon.connect(_home(environ))
    if client is not None:
        try:
            active = client.call("GET", "/v1/status", timeout=10).get("active_turn")
            return {active["turn_id"]} if active else set()
        except (daemon.DaemonError, KeyError, TypeError):
            return None
    return views.live_turns(host)


def cmd_sessions(arguments, environ) -> int:
    if arguments.action == "show":
        if not arguments.query:
            return _fail(arguments, "sessions show needs a session id")
        host = _host(arguments, environ)
        try:
            view = views.session(host, arguments.query, active=_live_turns(host, environ))
        except StateError as error:
            return _fail(arguments, error)
        finally:
            host.close()
        human = "\n".join(f"{turn['label']:<10} you: {turn['user_input'][:100]}\n"
                          f"{'':<10} agent: {(turn['response'] or '(no answer recorded)')[:300]}"
                          for turn in view["turns"])
        _emit(arguments, view, human)
        return 0
    if arguments.action == "forget":
        if not arguments.query:
            return _fail(arguments, "sessions forget needs a session id")
        host = _host(arguments, environ)
        try:
            forgotten = host.forget_session(arguments.query)
        except StateError as error:
            return _fail(arguments, error)
        finally:
            host.close()
        _emit(arguments, {"ok": True, **forgotten, "workspace": str(host.workspace)},
              f"Forgot session {arguments.query}: {len(forgotten['turns'])} turn(s), "
              f"{forgotten['scheduled_answers']} scheduled-run answer(s).")
        return 0
    if arguments.action != "search":
        return _query(arguments, environ, "sessions", lambda h: views.sessions(
            h, active=_live_turns(h, environ))["sessions"])
    if not arguments.query:
        return _fail(arguments, "sessions search needs a query")
    host = _host(arguments, environ)
    try:
        result = host.session_index.search(host.store, host.namespace, arguments.query,
                                           limit=arguments.limit)
    except StateError as error:
        return _fail(arguments, error)
    finally:
        host.close()
    _emit(arguments, {**result, "workspace": str(host.workspace)}, format_hits(result))
    return 0


def cmd_context(arguments, environ) -> int:
    """Preview the learned-context block a turn with this message would be offered."""
    capabilities = arguments.capabilities.split(",") if arguments.capabilities else \
        list(TURN_CAPABILITIES)
    host = _host(arguments, environ)
    try:
        text, report = knowledge.assemble(knowledge.gather(
            host.store, namespace=host.namespace, profile=host.profile_namespace,
            workspace_root=host.workspace, query=query_text(arguments.message),
            capabilities=capabilities, now=time.time()))
    except StateError as error:
        return _fail(arguments, error)
    finally:
        host.close()
    _emit(arguments, {"text": text, "report": report, "limits": knowledge.describe_budget()},
          text + f"\n\n[{report['used']} of {report['budget']} characters]")
    return 0


def cmd_memory(arguments, environ) -> int:
    """List or search facts; edit or forget one (``--scope workspace|profile``)."""
    if arguments.action == "list":
        return _query(arguments, environ, "facts",
                      lambda h: h.memory(arguments.search, scope=arguments.scope))
    if not arguments.id or arguments.scope not in ("workspace", "profile"):
        return _fail(arguments, f"memory {arguments.action} needs a fact id and --scope "
                                "workspace or profile")
    host = _host(arguments, environ)
    try:
        if arguments.action == "edit":
            if not arguments.text:
                return _fail(arguments, "memory edit needs --text")
            document = views.memory_edit(host, arguments.scope, arguments.id, arguments.text)
            human = f"[{document['fact']['fact_id']}] {document['fact']['text']}"
        else:
            document = views.memory_forget(host, arguments.scope, arguments.id)
            human = "Forgotten."
    except (StateError, ValueError) as error:
        return _fail(arguments, error)
    finally:
        host.close()
    _emit(arguments, document, human)
    return 0


def cmd_repl(arguments, environ) -> int:
    from .repl import Repl

    capabilities = arguments.capabilities.split(",") if getattr(arguments, "capabilities", None) \
        else None
    return Repl(environ, session=getattr(arguments, "session", None),
                json_mode=getattr(arguments, "json", False),
                workspace=_workspace_arg(arguments, environ), capabilities=capabilities,
                quiet=getattr(arguments, "quiet", False)).run()


def cmd_open(arguments, environ) -> int:
    """Print a one-time sign-in link for the web companion (the daemon serves it). Never
    opens a browser: a launched browser's argv would show the link to other local users."""
    client = daemon.connect(_home(environ))
    if client is None:
        return _fail(arguments, "The companion is served by the daemon: start it with "
                                "`brainstem-agent serve --detach`, then run open again.")
    try:
        if arguments.sign_out_all:
            answer = client.call("POST", "/v1/companion/revoke", {}, timeout=10)
            _emit(arguments, answer, f"Signed out {answer['revoked']} companion session(s).")
            return 0
        answer = client.call("POST", "/v1/companion/login", {}, timeout=10)
    except daemon.DaemonError as error:
        return _fail(arguments, error)
    _emit(arguments, {"ok": True, **answer},
          f"One-time sign-in link for the Brainstem Agent companion (works once, for "
          f"{answer['expires_in']} seconds, in one browser tab; it is not saved anywhere):\n"
          f"{answer['url']}")
    return 0


def cmd_api(arguments, environ) -> int:
    """Call any documented daemon route as the owner (``GET /v1/api`` lists them)."""
    method = arguments.method.upper()
    route, _params, known = api_routes.match(method, arguments.path.split("?", 1)[0])
    if route is None:
        return _fail(arguments, f"{method} {arguments.path} is not a documented route"
                                + (" (wrong method)" if known else "") + "; see api GET /v1/api")
    client = daemon.connect(_home(environ))
    if client is None:
        return _fail(arguments, "No daemon is running (start it with `brainstem-agent serve "
                                "--detach`).")
    try:
        body = json.loads(arguments.body) if arguments.body else ({} if method == "POST" else None)
        if route.name == "requests.events":
            for event in client.stream(arguments.path):
                print(json.dumps(event), flush=True)
            return 0
        answer = client.call(method, arguments.path, body, timeout=arguments.timeout)
    except ValueError:
        return _fail(arguments, "--body must be JSON")
    except daemon.DaemonError as error:
        return _fail(arguments, error)
    print(json.dumps(answer, indent=2))
    return 0


def _line(document: dict) -> None:
    print(json.dumps(document), flush=True)


def cmd_serve(arguments, environ) -> int:
    home = Path(os.path.realpath(_home(environ)))
    if arguments.detach:
        extra = ["--workspace", os.path.realpath(arguments.workspace)] if arguments.workspace else []
        try:
            running = daemon.read_record(home)
            if running is not None:
                raise daemon.DaemonError(f"Brainstem Agent is already running for this home "
                                         f"(pid {running['pid']}); a second daemon refuses.")
            status = daemon.spawn(home, environ, extra)
        except daemon.DaemonError as error:
            _emit(arguments, {"ok": False, "error": str(error)}, f"Brainstem Agent: {error}",
                  error=True)
            return 1
        _emit(arguments, status, f"Brainstem Agent daemon is running (pid {status['pid']}).")
        return 0
    try:
        cell = daemon.Daemon(home, workspace=arguments.workspace, cache=_cache(environ),
                             environ=environ)
    except (HostError, StateError) as error:
        _emit(arguments, {"ok": False, "error": str(error)}, f"Brainstem Agent: {error}",
              error=True)
        return 1

    def ready(status: dict) -> None:
        if arguments.json:
            _line(status)
        else:
            print(f"Brainstem Agent daemon is running (pid {status['pid']}).", flush=True)

    try:
        evidence = daemon.serve_foreground(cell, ready=ready)
    except daemon.DaemonError as error:
        _emit(arguments, {"ok": False, "error": str(error)}, f"Brainstem Agent: {error}",
              error=True)
        return 1
    if arguments.json:
        _line({"ok": True, "stopped": True, **evidence})
    else:
        print("Brainstem Agent daemon stopped.", flush=True)
    return 0


def cmd_status(arguments, environ) -> int:
    """The daemon's status with its liveness and readiness (``readiness``: every check with a
    reason and a fix). Exit 0 when the daemon is live, 1 when it is not running."""
    client = daemon.connect(_home(environ))
    if client is not None:
        try:
            status = client.call("GET", "/v1/status", timeout=10)
            scheduler = status["scheduler"]
            states = [item.get("state", "warm" if item.get("warm") else "busy")
                      for item in status["workers"]]
            others = "".join(f", {states.count(state)} {state}" for state in
                             ("starting", "busy", "stopped") if state in states)
            human = (f"Brainstem Agent daemon {status['health']} (pid "
                     f"{status['pid']}): {states.count('warm')} warm worker(s){others}, schedules "
                     f"{scheduler['schedules']}, next fire {scheduler['next_fire_local']}, "
                     f"{len(status['last_errors'])} recent error(s).")
            readiness = status.get("readiness")
            if isinstance(readiness, dict):
                human += "\n" + health.render(readiness, "Readiness")
            _emit(arguments, status, human)
            return 0 if not isinstance(readiness, dict) or readiness.get("live") else 1
        except daemon.DaemonError:
            pass
    host = _host(arguments, environ)
    try:
        states: dict = {}
        for item in host.store.list_schedules(None):
            states[item["state"]] = states.get(item["state"], 0) + 1
        wake = host.store.next_wake()
    finally:
        host.close()
    readiness = health.summarize(health.not_running_checks(_home(environ), _cache(environ),
                                                           environ))
    document = {"ok": True, "running": False, "product": "Brainstem Agent",
                "scheduler": {"schedules": states, "next_fire_at": wake,
                              "next_fire_local": schedules.local_iso(wake, schedules.local_zone())},
                "readiness": readiness}
    _emit(arguments, document, "Brainstem Agent daemon is not running.\n"
          + health.render(readiness, "Readiness"))
    return 1


def cmd_stop(arguments, environ) -> int:
    home = Path(os.path.realpath(_home(environ)))
    drained = None
    if arguments.drain:
        client = daemon.connect(home)
        if client is not None:
            try:
                drained = client.call("POST", "/v1/drain", {"timeout": arguments.timeout},
                                      timeout=arguments.timeout + 30)
            except daemon.DaemonError as error:
                drained = {"drained": False, "error": str(error)}
    result = daemon.stop(home)
    if drained is not None:
        result["drain"] = drained
    if not result.get("running", True):
        human = "Brainstem Agent daemon is not running."
    else:
        human = (f"Brainstem Agent daemon stopped (pid {result['pid']}, {result['seconds']}s)."
                 if result["ok"] else "Brainstem Agent daemon did not stop cleanly.")
    _emit(arguments, result, human, error=not result["ok"])
    return 0 if result["ok"] else 1


def _await_run(host: AgentHost, record: dict, client, timeout: float) -> dict:
    """The run-now occurrence's outcome: in-process without a daemon, else as the daemon runs it."""
    occurrence_id = f"occ_{record['schedule_id']}_{int(record['run_now_at'])}_m"
    if client is None:
        with host.exclusive():
            scheduler = schedules.Scheduler(
                host.store, lambda occurrence: schedules.run_occurrence(host, occurrence))
            scheduler.tick(schedule_id=record["schedule_id"], limit=1)
    deadline = time.monotonic() + timeout
    while True:
        run = host.store.get_occurrence(occurrence_id)
        if (run and run["state"] != "running") or time.monotonic() >= deadline:
            return run or {"occurrence_id": occurrence_id, "state": "pending"}
        time.sleep(0.2)


def cmd_schedules(arguments, environ) -> int:
    action = arguments.action
    if action not in ("list", "create", "runs") and not arguments.id:
        _emit(arguments, {"ok": False, "error": f"{action} needs a schedule id"},
              f"Brainstem Agent: {action} needs a schedule id", error=True)
        return 2
    capabilities = arguments.capabilities.split(",") if arguments.capabilities else None
    fields = {"in_seconds": arguments.in_seconds, "at": arguments.at,
              "every_seconds": arguments.every, "cron": arguments.cron, "prompt": arguments.prompt,
              "name": arguments.name, "timezone": arguments.timezone,
              "capabilities": capabilities, "missed_policy": arguments.missed}
    host = _host(arguments, environ)
    try:
        store, now = host.store, time.time()
        if arguments.id and action in ("show", "runs"):
            arguments.id = schedules.find_schedule(store, host.namespace, arguments.id)[
                "schedule_id"]
        if action in ("list", "runs"):
            if action == "list":
                items = [schedules.describe(item) for item in
                         store.list_schedules(host.namespace, include_removed=arguments.all)]
            else:
                items = store.list_occurrences(host.namespace, schedule_id=arguments.id)
            key = "schedules" if action == "list" else "runs"
            _emit(arguments, {key: items, "workspace": str(host.workspace)},
                  "\n".join(json.dumps(item) for item in items) or f"(no {key})")
            return 0
        if action == "show":
            document = {"schedule": schedules.describe(store.get_schedule(host.namespace,
                                                                          arguments.id)),
                        "runs": store.list_occurrences(host.namespace, schedule_id=arguments.id,
                                                       limit=10)}
            _emit(arguments, document, json.dumps(document, indent=2))
            return 0
        if action == "create":
            # The owner's schedule gets what an owner chat turn gets (its web and long-turn
            # tools and every configured MCP server included) unless --capabilities narrows it.
            record = schedules.create_schedule(
                store, namespace=host.namespace, workspace=str(host.workspace),
                prompt=arguments.prompt, when=fields, capabilities=capabilities,
                allowed=host.known_capabilities(), default=host.turn_capabilities(),
                created_by="owner",
                now=now, timezone=arguments.timezone, name=arguments.name,
                missed_policy=arguments.missed)
        else:
            record = schedules.change_schedule(store, host.namespace, arguments.id,
                                               action.replace("-", "_"), fields,
                                               allowed=host.known_capabilities(), now=now,
                                               writer="owner")
        client = daemon.connect(_home(environ))
        if client is not None:
            try:
                client.call("POST", "/v1/wake", {}, timeout=5)
            except daemon.DaemonError:
                client = None
        document = {"ok": True, "schedule": schedules.describe(record),
                    "daemon_running": client is not None}
        code = 0
        if action == "run-now":
            run = document["run"] = _await_run(host, record, client, arguments.timeout)
            code = EXIT.get(run["state"], 1)
            document["ok"] = code == 0
    except (schedules.ScheduleError, StateError, HostError) as error:
        _emit(arguments, {"ok": False, "error": str(error)}, f"Brainstem Agent: {error}",
              error=True)
        return 1
    finally:
        host.close()
    view = document["schedule"]
    _emit(arguments, document, f"{view['schedule_id']} '{view['name']}': {view['state']}, "
          f"{view['when']}, next {view['next_fire_local']}"
          + (f"; run {document['run']['state']}" if "run" in document else ""))
    return code


def cmd_inbox(arguments, environ) -> int:
    def fetch(host: AgentHost) -> list[dict]:
        names = {item["schedule_id"]: item["name"]
                 for item in host.store.list_schedules(host.namespace, include_removed=True)}
        return [{**run, "name": names.get(run["schedule_id"])}
                for run in host.store.list_occurrences(host.namespace, limit=arguments.limit)]

    return _query(arguments, environ, "inbox", fetch)


def cmd_service(arguments, environ) -> int:
    home = Path(os.path.realpath(_home(environ)))
    document = daemon.service(arguments.action, home, environ, dry_run=arguments.dry_run)
    human = document.get("plist") if arguments.dry_run and arguments.action == "install" else \
        f"{arguments.action}: {document['path']}" + (" (dry run)" if arguments.dry_run else "")
    _emit(arguments, document, human, error=not document["ok"])
    return 0 if document["ok"] else 1


def _step_line(step: dict) -> str:
    detail, result = step.get("detail") or {}, step.get("result") or {}
    if step["kind"] == "segment":
        return (f"  step {step['seq']}: {step['state']}, {result.get('rounds', '?')} round(s), "
                f"{result.get('tool_calls', '?')} call(s), {result.get('seconds', '?')}s"
                + (" (continuation)" if detail.get("continuation") else ""))
    if step["kind"] == "child":
        return (f"  helper {step['seq']} {step['step_id']}: {step['state']}, "
                f"{result.get('segments', '?')} step(s): {str(detail.get('task', ''))[:80]}")
    return f"turn {step['turn_id']}: {step['state']}"


def cmd_turns(arguments, environ) -> int:
    """The journal of long turns: every segment (Grail request), helper and receipt."""
    host = _host(arguments, environ)
    try:
        if arguments.action == "show":
            if not arguments.turn_id:
                return _fail(arguments, "turns show needs a turn id")
            journal = host.store.journal(host.namespace, arguments.turn_id)
            if not journal["steps"] and not journal["receipts"]:
                return _fail(arguments, f"No journal for {arguments.turn_id} in this workspace.")
            human = "\n".join([_step_line(step) for step in journal["steps"]] + [
                f"  receipt {r['turn_id'][:14]} {r['call_id']}: {r['tool']} {r['state']}"
                for r in journal["receipts"]])
            _emit(arguments, {**journal, "workspace": str(host.workspace)}, human)
            return 0
        journals = host.store.list_journals(host.namespace, limit=arguments.limit)
        _emit(arguments, {"turns": journals, "workspace": str(host.workspace)},
              "\n".join(f"{j['turn_id']}: {j['state']}, "
                        f"{(j.get('result') or {}).get('segments', '?')} step(s)"
                        for j in journals) or "(no turns)")
        return 0
    except StateError as error:
        return _fail(arguments, error)
    finally:
        host.close()


def cmd_processes(arguments, environ) -> int:
    """Background processes of this workspace (the daemon runs them); stop one."""
    if arguments.action == "stop":
        if not arguments.process_id:
            return _fail(arguments, "processes stop needs a process id")
        client = daemon.connect(_home(environ))
        if client is None:
            return _fail(arguments, "No daemon is running, so no background process is either.")
        try:
            result = client.call("POST", "/v1/tool", {
                "name": "process_stop", "arguments": {"process_id": arguments.process_id},
                "capabilities": ["processes.run"],
                "workspace": _workspace_arg(arguments, environ)}, timeout=30)
        except daemon.DaemonError as error:
            return _fail(arguments, error)
        _emit(arguments, result, result.get("content", ""), error=not result.get("ok"))
        return 0 if result.get("ok") else 1
    return _query(arguments, environ, "processes",
                  lambda h: h.store.list_processes(h.namespace))


def cmd_mcp(arguments, environ) -> int:
    """The owner's MCP servers (reach.json): ``list`` the configuration (nothing starts),
    ``status`` (the daemon's servers when it runs, else started, checked and stopped here),
    ``trust`` a server's current tool definitions (offering the tools withheld because they
    changed or are new since the owner first saw the server)."""
    home = _home(environ)
    config, error = read_config(home / "reach.json")
    wanted = arguments.server
    if arguments.action == "list":
        servers, problems = configured_servers(config)
        document = {"ok": error is None, "config": str(home / "reach.json"),
                    "exists": (home / "reach.json").exists(), "servers": servers,
                    "problems": [error] if error else problems}
        human = "\n".join(f"{item['server']}: {item['transport']}, {item['capability']}"
                          + (" (disabled)" if item["disabled"] else "") for item in servers)
        _emit(arguments, document, (human or "(no MCP servers in reach.json)") + "".join(
            f"\nproblem: {item}" for item in document["problems"]))
        return 0 if error is None else 1
    if arguments.action == "trust":
        if not wanted:
            return _fail(arguments, "mcp trust needs a server name")
        host = _host(arguments, environ)
        try:
            result = host.mcp_organ.trust(wanted)
        except OrganError as problem:
            return _fail(arguments, problem)
        finally:
            host.close()
        _emit(arguments, {"ok": True, **result},
              f"Trusted {len(result['trusted'])} tool definition(s) of {wanted}"
              + (f"; now offered: {', '.join(result['offered_now'])}" if result["offered_now"]
                 else "") + ".")
        return 0
    enabled = sorted(server_specs(config)[0])
    if wanted and wanted not in enabled:
        return _fail(arguments, f"No enabled MCP server is named {wanted!r} in reach.json.")
    chosen = [wanted] if wanted else enabled
    client = daemon.connect(home)
    servers, running = None, client is not None
    if client is not None:
        try:
            reported = client.call("GET", "/v1/status", timeout=10).get("mcp", [])
            known = {item["server"]: item for item in reported if "server" in item}
            servers = [known.get(key, {"server": key, "capability": "mcp." + key,
                                       "state": "not started", "starts": 0})
                       for key in chosen]
        except daemon.DaemonError:
            running = False
    if servers is None:
        host = _host(arguments, environ)
        try:
            host.mcp_organ.prepare(["mcp." + key for key in chosen])
            servers = [item for item in host.mcp_organ.status() if item.get("server") in chosen]
        finally:
            host.close()
    document = {"ok": error is None, "daemon_running": running, "servers": servers,
                "problems": [error] if error else server_specs(config)[1]}
    _emit(arguments, document, "\n".join(
        f"{item['server']}: {item['state']}, {len(item.get('tools') or [])} tool(s)"
        + (f", {len(item['withheld'])} withheld" if item.get("withheld") else "")
        + (f" ({item['error']})" if item.get("error") else "") for item in servers)
        or "(no MCP servers in reach.json)")
    return 0 if error is None else 1


def cmd_egress(arguments, environ) -> int:
    """The outbound request log (``state/egress.jsonl``), oldest first: host, address, path
    without its query, status, bytes and seconds; never a header."""
    log = EgressLog(_home(environ) / "state" / "egress.jsonl")
    entries = log.read(max(1, arguments.limit))
    _emit(arguments, {"ok": True, "log": str(log.path), "requests": entries}, "\n".join(
        f"{item.get('at')} {item.get('tool')} {item.get('method')} {item.get('host')}"
        f"{item.get('path') or ''} -> {item.get('status')} ({item.get('bytes', 0)} bytes, "
        f"{item.get('seconds')}s)" for item in entries) or "(no outbound requests)")
    return 0


def cmd_cancel(arguments, environ) -> int:
    client = daemon.connect(_home(environ))
    if client is None:
        _emit(arguments, {"ok": True, "cancelled": False, "reason": "no daemon is running"},
              "No daemon is running (an in-process turn stops with Ctrl-C).")
        return 0
    try:
        result = client.call("POST", "/v1/cancel", {"active": True}, timeout=30)
    except daemon.DaemonError as error:
        return _fail(arguments, error)
    _emit(arguments, {"ok": True, **result},
          f"Cancelled turn {result.get('turn_id')} in {result.get('seconds')}s."
          if result.get("cancelled") else "No turn was running.")
    return 0


# -- operations: version, backup, restore, export, upgrade, rollback, uninstall, hygiene -------
def _events(environ) -> observe.EventLog:
    return observe.EventLog(Path(os.path.realpath(_home(environ))))


def cmd_version(arguments, environ) -> int:
    """The product version, the Grail pin, store schemas, digests, capabilities and the release
    manifest check (and the running daemon's version)."""
    home = _home(environ)
    document = release.version_info(home if home.exists() else None)
    client = daemon.connect(home)
    if client is not None:
        try:
            document["daemon"] = {"pid": client.pid, "version_id": client.call(
                "GET", "/v1/version", timeout=10).get("version_id")}
        except daemon.DaemonError as error:
            document["daemon"] = {"pid": client.pid, "error": str(error)}
    manifest = document["release_manifest"]
    human = (f"Brainstem Agent {document['version']} ({document['version_id']}), "
             f"{document['install']['kind']} install, Python {document['python']}\n"
             f"Grail {document['grail']['version']} @ {document['grail']['commit'][:12]} "
             f"(kernel {document['grail']['kernel_sha256'][:12]}), store schema "
             f"{document['store']['schema_version']}, release manifest "
             + ("verified" if manifest.get("verified") else "NOT verified: "
                + "; ".join(manifest.get("problems", [])[:3])))
    _emit(arguments, document, human)
    return 0 if manifest.get("verified") else 1


def cmd_backup(arguments, environ) -> int:
    try:
        result = backups.create_backup(_home(environ), arguments.output,
                                       include_secrets=arguments.include_secrets,
                                       events=_events(environ))
    except (backups.BackupError, StateError, OSError) as error:
        return _fail(arguments, error)
    excluded = [item["item"] for item in result["excluded"]]
    _emit(arguments, result, f"Backup written to {result['path']} ({len(result['files'])} "
          f"file(s), {result['seconds']}s). Excluded: {'; '.join(excluded)}.")
    return 0


def cmd_restore(arguments, environ) -> int:
    home = _home(environ)
    try:
        result = backups.restore_backup(arguments.backup, home, replace=arguments.replace,
                                        events=_events(environ))
    except (backups.BackupError, StateError, OSError) as error:
        return _fail(arguments, error)
    result["health"] = health.summarize(health.installation_checks(
        home, _cache(environ), environ, import_check=False))
    lines = [f"Restored {', '.join(result['restored'])} into {result['home']}."]
    lines += [f"Re-enter: {item['item']} ({item['reason']})" for item in result["needs_attention"]]
    lines.append(health.render(result["health"]))
    _emit(arguments, result, "\n".join(lines))
    return 0


def cmd_export(arguments, environ) -> int:
    host = _host(arguments, environ)
    try:
        result = backups.export_data(host, arguments.output)
    except (backups.BackupError, StateError, OSError) as error:
        return _fail(arguments, error)
    finally:
        host.close()
    counts = result["counts"]
    _emit(arguments, result, f"Exported {counts['skills']} skill(s), {counts['memory']} memory "
          f"fact(s), {counts['profile']} profile fact(s) and {counts['sessions']} session(s) to "
          f"{result['path']}.")
    return 0


def cmd_upgrade(arguments, environ) -> int:
    try:
        result = lifecycle.upgrade(_home(environ), environ, arguments.source,
                                   dry_run=arguments.dry_run,
                                   drain_timeout=arguments.drain_timeout, force=arguments.force)
    except (lifecycle.LifecycleError, backups.BackupError, StateError, OSError) as error:
        return _fail(arguments, error)
    if not result.get("ok"):
        return _fail(arguments, result.get("error"))
    if arguments.dry_run:
        human = (f"Would upgrade {result['from']} -> {result['to']} (manifest verified; store "
                 f"{result.get('store', {}).get('verdict', '-')}; daemon running: "
                 f"{result.get('daemon_running')}).")
    elif not result.get("changed", True):
        human = f"Nothing to do: {result['reason']}."
    else:
        human = (f"Upgraded {result['from']} -> {result['to']}; the previous version stays "
                 f"installed (brainstem-agent rollback). Pre-upgrade backup: "
                 f"{result.get('pre_upgrade_backup')}. Now: {result.get('health')}")
    _emit(arguments, result, human)
    return 0


def cmd_rollback(arguments, environ) -> int:
    try:
        result = lifecycle.rollback(_home(environ), environ, dry_run=arguments.dry_run,
                                    drain_timeout=arguments.drain_timeout, force=arguments.force,
                                    restore=arguments.restore)
    except (lifecycle.LifecycleError, backups.BackupError, StateError, OSError) as error:
        return _fail(arguments, error)
    verb = "Would roll back" if arguments.dry_run else "Rolled back"
    _emit(arguments, result, f"{verb} {result['from']} -> {result['to']}."
          + ("" if arguments.dry_run else f" Now: {result.get('health')}"))
    return 0


def cmd_uninstall(arguments, environ) -> int:
    result = lifecycle.uninstall(_home(environ), environ, dry_run=arguments.dry_run,
                                 remove_home=arguments.remove_home, confirm=arguments.confirm)
    lines = [("Would remove:" if arguments.dry_run else "Removed:" if result["ok"] else
              "Not removed:")]
    lines += [f"  {item['kind']}: {item.get('path') or item.get('pid')}" for item in
              result.get("removed", result["remove"])]
    lines += ["Keeps:"] + [f"  {item['path']} ({item['reason']})" for item in result["keep"]]
    lines += [f"Refused: {item}" for item in result["refused"]]
    _emit(arguments, result, "\n".join(lines), error=not result["ok"])
    if result.get("needs_confirmation"):
        return 2
    return 0 if result["ok"] else 1


def _open_store(environ) -> Store | None:
    path = _home(environ) / "state" / "agent.sqlite3"
    return Store(path) if path.exists() else None


def cmd_prune(arguments, environ) -> int:
    home = _home(environ)
    policy, problems = hygiene.load_policy(home, environ)
    try:
        store = _open_store(environ)
        if store is None:
            result = {"ok": True, "dry_run": arguments.dry_run, "categories": {}, "total": 0,
                      "policy": policy["retention"]}
        else:
            with store:
                result = hygiene.prune(home, store, dry_run=arguments.dry_run, policy=policy)
    except (StateError, OSError) as error:
        return _fail(arguments, error)
    result["policy_problems"] = problems
    if not arguments.dry_run:
        _events(environ).write("prune.completed", total=result["total"], categories={
            name: entry.get("deleted", 0) for name, entry in result["categories"].items()})
    lines = [f"{'Would remove' if arguments.dry_run else 'Removed'} {result['total']} record(s):"]
    lines += [f"  {name}: {entry.get('count', 0)}" for name, entry in result["categories"].items()]
    _emit(arguments, result, "\n".join(lines))
    return 0


def cmd_compact(arguments, environ) -> int:
    """VACUUM the store; needs the home to itself (stop the daemon first)."""
    import fcntl

    home = _home(environ)
    lock_path = home / "state" / "host.lock"
    try:
        store = _open_store(environ)
    except StateError as error:
        return _fail(arguments, error)
    if store is None:
        _emit(arguments, {"ok": True, "compacted": False, "reason": "no store yet"},
              "Nothing to compact: this home has no store yet.")
        return 0
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _fail(arguments, "The daemon (or another command) is using this home; stop it "
                                    "first (brainstem-agent stop), then compact.")
        with store:
            result = hygiene.compact(home, store, environ=environ)
    except (StateError, OSError) as error:
        return _fail(arguments, error)
    finally:
        os.close(descriptor)
    if not result["ok"]:
        return _fail(arguments, result["error"])
    result["health"] = health.summarize(health.installation_checks(
        home, _cache(environ), environ, import_check=False))
    _events(environ).write("compact.completed", reclaimed_bytes=result["reclaimed_bytes"],
                           seconds=result["seconds"])
    _emit(arguments, result, f"Compacted the store: {result['bytes_before']} -> "
          f"{result['bytes_after']} bytes ({result['seconds']}s, check {result['quick_check']}).")
    return 0


def cmd_logs(arguments, environ) -> int:
    """The local logs with filters: the cell's event log (default), the daemon's output, the
    workers' logs or the outbound request log."""
    home = _home(environ)
    try:
        since, until = observe.parse_when(arguments.since), observe.parse_when(arguments.until)
    except ValueError as error:
        return _fail(arguments, error)
    source = arguments.source
    if source == "events":
        entries = observe.read_events(home, since=since, until=until, level=arguments.level,
                                      event=arguments.event, turn=arguments.turn,
                                      grep=arguments.grep, limit=arguments.limit)
        human = "\n".join(f"{item['at']} {item['level']:5} {item['event']} " + " ".join(
            f"{key}={item[key]}" for key in item if key not in ("at", "ts", "level", "event",
                                                                "pid")) for item in entries)
    elif source == "egress":
        entries = observe.read_egress(home, since=since, grep=arguments.grep,
                                      limit=arguments.limit)
        human = "\n".join(json.dumps(item) for item in entries)
    else:
        paths = ([home / "logs" / "daemon.log.1", home / "logs" / "daemon.log"]
                 if source == "daemon" else list(reversed(hygiene.worker_logs(home))))
        entries = observe.read_text_log([item for item in paths if item.exists()],
                                        grep=arguments.grep, limit=arguments.limit, since=since)
        human = "\n".join(f"{item['file']}: {item['line']}" for item in entries)
    _emit(arguments, {"ok": True, "source": source, "entries": entries},
          human or f"(no {source} entries)")
    return 0


def cmd_stats(arguments, environ) -> int:
    """Turns, outcomes, uncertainty, latency percentiles, tool usage and fire delays from the
    local store (the whole home, or one --workspace)."""
    try:
        since, until = observe.parse_when(arguments.since), observe.parse_when(arguments.until)
    except ValueError as error:
        return _fail(arguments, error)
    namespace = (cell_namespace(OWNER, str(canonical(arguments.workspace)))
                 if arguments.workspace else None)
    try:
        store = _open_store(environ)
        if store is None:
            _emit(arguments, {"ok": True, "turns": {"total": 0}}, "No turns yet.")
            return 0
        with store:
            stats = observe.compute_stats(store, since=since, until=until, namespace=namespace,
                                          home=_home(environ))
    except (StateError, OSError) as error:
        return _fail(arguments, error)
    _emit(arguments, {"ok": True, **stats}, observe.summarize(stats))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brainstem-agent",
                                     description="Brainstem Agent: a cell around Brainstem Grail. "
                                                 "With no command: the interactive terminal.")
    commands = parser.add_subparsers(dest="command")

    def command(name, help_text, *, workspace=True):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true", help="machine-readable output")
        if workspace:
            sub.add_argument("--workspace", type=Path, help="workspace directory")
        return sub

    command("setup", "verify/fetch pinned Grail and build the worker venv", workspace=False)
    doctor = command("doctor", "report readiness")
    doctor.add_argument("--deep", action="store_true", help="start a probe worker")
    chat = command("chat", "run one turn")
    chat.add_argument("message")
    chat.add_argument("--session")
    chat.add_argument("--idempotency-key")
    chat.add_argument("--capabilities", help=f"comma list of {','.join(ALL_CAPABILITIES)} "
                                             "and mcp.<server> for each server in reach.json")
    chat.add_argument("--timeout", type=float, default=300.0,
                      help="seconds to wait for one Grail request")
    chat.add_argument("--max-segments", type=int,
                      help="Grail requests this turn may use (default 8)")
    chat.add_argument("--max-tool-calls", type=int,
                      help="tool calls, inner script calls included (default 100)")
    chat.add_argument("--max-seconds", type=float,
                      help="wall time of the whole turn, helpers included (default 900)")
    chat.add_argument("--quiet", action="store_true", help="no progress on stderr")
    tool = command("tool", "invoke one cell tool directly as the owner")
    tool.add_argument("name")
    tool.add_argument("--arguments", default="{}")
    tool.add_argument("--capabilities")
    memory = command("memory", "list or search memory (this workspace and the owner's profile), "
                               "or edit or forget one fact")
    memory.add_argument("action", nargs="?", choices=["list", "edit", "forget"], default="list")
    memory.add_argument("id", nargs="?", help="fact id (edit, forget)")
    memory.add_argument("--search")
    memory.add_argument("--scope", choices=["all", "workspace", "profile"], default="all")
    memory.add_argument("--text", help="edit: the new text")
    profile = command("profile", "list, add, edit or forget the owner's profile facts")
    profile.add_argument("action", choices=["list", "add", "edit", "forget"])
    profile.add_argument("id", nargs="?", help="fact id (edit, forget)")
    profile.add_argument("--text")
    skills = command("skills", "list, show, history, approve, reject, disable, enable, edit, "
                               "import, export, share, unshare or delete learned skills")
    skills.add_argument("action", choices=["list", "show", "history", "approve", "reject",
                                           "disable", "enable", "edit", "import", "export",
                                           "share", "unshare", "delete"])
    skills.add_argument("name", nargs="?")
    skills.add_argument("--version", type=int,
                        help="show: this version; approve: exactly this version (the current "
                             "or the pending one)")
    skills.add_argument("--offered", action="store_true",
                        help="list: only what turns are offered (active, not quarantined)")
    skills.add_argument("--file", help="edit/import: a skill markdown file")
    skills.add_argument("--description")
    skills.add_argument("--when-to-use", dest="when_to_use")
    skills.add_argument("--step", action="append", help="edit: a step (repeat, in order)")
    skills.add_argument("--note", help="edit/import: change note")
    skills.add_argument("--shared", action="store_true", help="import: offer in every workspace")
    skills.add_argument("--output", help="export: write to this new file")
    sessions = command("sessions", "list sessions, show one (every turn and its state), search "
                                   "past turns in this workspace, or forget one session")
    sessions.add_argument("action", nargs="?", choices=["list", "show", "search", "forget"],
                          default="list")
    sessions.add_argument("query", nargs="?",
                          help="search: keywords; show, forget: a session id")
    sessions.add_argument("--limit", type=int, default=5)
    context = command("context", "preview the learned context (budgeted memory, profile, skill "
                                 "index and AGENTS.md) a message would be offered")
    context.add_argument("message")
    context.add_argument("--capabilities")
    receipts = command("receipts", "list tool receipts (--turn: that turn and its helpers)")
    receipts.add_argument("--turn")
    turns = command("turns", "list recent turn journals, or show one turn's journal (segments, "
                             "helpers, inner script calls, receipts)")
    turns.add_argument("action", nargs="?", choices=["list", "show"], default="list")
    turns.add_argument("turn_id", nargs="?")
    turns.add_argument("--limit", type=int, default=20)
    processes = command("processes", "list this workspace's background processes or stop one")
    processes.add_argument("action", nargs="?", choices=["list", "stop"], default="list")
    processes.add_argument("process_id", nargs="?")
    command("cancel", "cancel the daemon's active turn (its segments, helpers and scripts)")
    serve = command("serve", "run the always-on daemon (foreground; --detach to background)")
    serve.add_argument("--detach", action="store_true")
    command("status", "report the daemon's health, warm workers, schedules and last errors")
    stop = command("stop", "stop the daemon and its workers", workspace=False)
    stop.add_argument("--drain", action="store_true",
                      help="let the running turn and scheduled run finish first")
    stop.add_argument("--timeout", type=float, default=300.0,
                      help="seconds to wait for --drain (default 300)")
    plan = command("schedules", "list, show, create, edit, pause, resume, run-now, remove, runs")
    plan.add_argument("action", choices=["list", "show", "create", "edit", "pause", "resume",
                                         "run-now", "remove", "runs"])
    plan.add_argument("id", nargs="?", help="schedule id")
    plan.add_argument("--prompt")
    plan.add_argument("--name")
    plan.add_argument("--in", dest="in_seconds", type=int, help="once, this many seconds from now")
    plan.add_argument("--at", help="once, at this ISO 8601 local date-time")
    plan.add_argument("--every", type=int, help="repeat every N seconds (>= 60)")
    plan.add_argument("--cron", help="5-field cron on the local wall clock")
    plan.add_argument("--timezone", help="IANA timezone (default: the owner's)")
    plan.add_argument("--capabilities", help=f"comma list of {','.join(ALL_CAPABILITIES)} "
                                             "and mcp.<server> (default: what an owner chat "
                                             "turn gets, every configured mcp.<server> too)")
    plan.add_argument("--missed", choices=list(schedules.MISSED_POLICIES))
    plan.add_argument("--all", action="store_true", help="list: include removed schedules")
    plan.add_argument("--timeout", type=float, default=300.0, help="run-now: seconds to wait")
    reach = command("mcp", "list the owner's MCP servers (reach.json), show their status, or "
                           "trust a server's current tool definitions")
    reach.add_argument("action", choices=["list", "status", "trust"])
    reach.add_argument("server", nargs="?")
    egress = command("egress", "show the outbound request log (web and MCP HTTP)",
                     workspace=False)
    egress.add_argument("action", choices=["log"])
    egress.add_argument("--limit", type=int, default=50, help="the newest N requests")
    inbox = command("inbox", "read the results of scheduled runs, newest first")
    inbox.add_argument("--limit", type=int, default=20)
    service = command("service", "install or uninstall the launchd LaunchAgent", workspace=False)
    service.add_argument("action", choices=["install", "uninstall"])
    service.add_argument("--dry-run", action="store_true")
    repl = command("repl", "the interactive terminal (also: brainstem-agent with no command); "
                           "--json is a line protocol for agents")
    repl.add_argument("--session", help="resume this session")
    repl.add_argument("--capabilities")
    repl.add_argument("--quiet", action="store_true", help="no progress lines")
    opener = command("open", "print a one-time sign-in link for the web companion (the daemon "
                             "serves it on 127.0.0.1; never opens a browser)", workspace=False)
    opener.add_argument("--sign-out-all", action="store_true",
                        help="end every companion session and unused link")
    call = command("api", "call a documented daemon route as the owner (GET /v1/api lists "
                          "them; event streams print one JSON line per event)", workspace=False)
    call.add_argument("method", help="GET or POST")
    call.add_argument("path", help="for example /v1/sessions")
    call.add_argument("--body", help="JSON object for POST")
    call.add_argument("--timeout", type=float, default=60.0)
    command("version", "show the version, Grail pin, store schema, digests and capabilities, and "
                       "verify the release manifest", workspace=False)
    saving = command("backup", "write a verified backup of the store, learned knowledge, "
                               "schedules, inbox and owner config (secrets excluded)",
                     workspace=False)
    saving.add_argument("--output", type=Path, help="a new directory (default: "
                                                    "<home>/backups/<UTC time>)")
    saving.add_argument("--include-secrets", action="store_true",
                        help="keep reach.json as is (MCP environment values, URL keys)")
    restoring = command("restore", "verify a backup and restore it into this home",
                        workspace=False)
    restoring.add_argument("backup", type=Path)
    restoring.add_argument("--replace", action="store_true",
                           help="replace an existing store (a safety backup is taken first)")
    exporting = command("export", "write skills, memory, profile and sessions in portable "
                                  "formats (markdown and JSON lines)")
    exporting.add_argument("--output", type=Path, required=True, help="a new directory")
    upgrading = command("upgrade", "install a local release side by side, drain the daemon and "
                                   "switch to it (never downloads)", workspace=False)
    upgrading.add_argument("--from", dest="source", type=Path, required=True,
                           help="a repository checkout, its runtime/ directory, a zipapp or a "
                                "wheel")
    upgrading.add_argument("--dry-run", action="store_true")
    upgrading.add_argument("--drain-timeout", type=float, default=300.0,
                           help="seconds to let running work finish (default 300)")
    upgrading.add_argument("--force", action="store_true",
                           help="cancel running work if it does not finish in time")
    rolling = command("rollback", "switch back to the previous installed version",
                      workspace=False)
    rolling.add_argument("--dry-run", action="store_true")
    rolling.add_argument("--drain-timeout", type=float, default=300.0)
    rolling.add_argument("--force", action="store_true")
    rolling.add_argument("--restore", type=Path,
                         help="first put back the store of this pre-upgrade backup (the current "
                              "store is kept in a safety backup)")
    removing = command("uninstall", "remove the service, daemon, caches, worker trees, logs and "
                                    "versions (the home only with --remove-home and --confirm)",
                       workspace=False)
    removing.add_argument("--dry-run", action="store_true")
    removing.add_argument("--remove-home", action="store_true")
    removing.add_argument("--confirm", help="the home's path, exactly, to confirm --remove-home")
    pruning = command("prune", "apply the retention policy (receipts, run events, inbox, egress "
                               "log)", workspace=False)
    pruning.add_argument("--dry-run", action="store_true", help="only report what would go")
    command("compact", "compact the store (VACUUM); stop the daemon first", workspace=False)
    logs = command("logs", "read the local logs with filters", workspace=False)
    logs.add_argument("--source", choices=["events", "daemon", "worker", "egress"],
                      default="events")
    logs.add_argument("--since", help="30m, 2h, 7d or an ISO date-time")
    logs.add_argument("--until", help="30m, 2h, 7d or an ISO date-time")
    logs.add_argument("--level", choices=list(observe.LEVELS))
    logs.add_argument("--event", help="event name or pattern, for example 'turn.*'")
    logs.add_argument("--turn", help="a turn id")
    logs.add_argument("--grep", help="case-insensitive text")
    logs.add_argument("--limit", type=int, default=100)
    stats = command("stats", "turns, outcomes, uncertainty, latency percentiles, tool usage and "
                             "fire delays from the local store")
    stats.add_argument("--since", help="30m, 2h, 7d or an ISO date-time")
    stats.add_argument("--until", help="30m, 2h, 7d or an ISO date-time")
    return parser


_NO_REDIRECT = frozenset({"upgrade", "rollback", "uninstall"})


def main(argv=None, environ=None, *, redirect: bool | None = None) -> int:
    """Run one command. A real invocation (the console script, ``python -m brainstem_agent``
    or the zipapp) continues in the home's active version when this code is one the home has
    moved away from (``release.redirect_target``); upgrade, rollback and uninstall never do."""
    real = argv is None if redirect is None else redirect
    environ = dict(os.environ if environ is None else environ)
    argv = list(sys.argv[1:] if argv is None else argv)
    if real and (argv[:1] or [""])[0] not in _NO_REDIRECT:
        target, note = release.redirect_target(_home(environ), environ)
        if note:
            print(f"Brainstem Agent: {note}", file=sys.stderr)
        if target is not None:
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(target), *[item for item in env.get(
                "PYTHONPATH", "").split(os.pathsep) if item]])
            env["BRAINSTEM_AGENT_REDIRECTED"] = target.name
            sys.stdout.flush()
            sys.stderr.flush()
            os.execve(sys.executable, [sys.executable, "-m", "brainstem_agent", *argv], env)
    arguments = build_parser().parse_args(argv)
    if arguments.command is None:  # `brainstem-agent` alone: the interactive terminal
        arguments = build_parser().parse_args(["repl"])
    handlers = {
        "setup": cmd_setup, "doctor": cmd_doctor, "chat": cmd_chat, "tool": cmd_tool,
        "memory": cmd_memory,
        "profile": cmd_profile, "skills": cmd_skills, "sessions": cmd_sessions,
        "context": cmd_context,
        "receipts": lambda a, e: _query(a, e, "receipts", lambda h: h.receipts(a.turn)),
        "turns": cmd_turns, "processes": cmd_processes, "cancel": cmd_cancel,
        "serve": cmd_serve, "status": cmd_status, "stop": cmd_stop, "schedules": cmd_schedules,
        "inbox": cmd_inbox, "service": cmd_service, "mcp": cmd_mcp, "egress": cmd_egress,
        "repl": cmd_repl, "open": cmd_open, "api": cmd_api,
        "version": cmd_version, "backup": cmd_backup, "restore": cmd_restore,
        "export": cmd_export, "upgrade": cmd_upgrade, "rollback": cmd_rollback,
        "uninstall": cmd_uninstall, "prune": cmd_prune, "compact": cmd_compact,
        "logs": cmd_logs, "stats": cmd_stats,
    }
    try:
        return handlers[arguments.command](arguments, environ)
    except (StateError, HostError) as error:  # the store or host refused: JSON, no traceback
        cause = error.__cause__
        text = str(error) if cause is None or str(cause) in str(error) else f"{error} ({cause})"
        if isinstance(error, StateError) and "store" not in text.lower():
            text = (f"The cell's store could not be used: {text}. See brainstem-agent doctor; "
                    "if it persists, restore the latest backup (brainstem-agent restore).")
        return _fail(arguments, redact_credentials(text))
