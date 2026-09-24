"""Owner views and review actions shared by the CLI and the daemon's API routes.

Every companion screen is one of these, reached through a documented route (``api.py``);
the CLI calls the same functions, so both surfaces show the same state. Store states are
shown as they are recorded; ``label`` adds only what the store cannot know: a turn the
store still records as running that no live request is running is ``stale``, and a turn
whose journal ended ``partial`` (the chat row says failed) is ``partial``. The sessions
list labels each session's last turn the same way.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import Any, Iterable

from . import schedules as schedule_module
from .host import TURN_CAPABILITIES, AgentHost
from .organs.mcp import server_specs
from .organs.skills import normalize_name, render_skill
from .organs.web import read_config
from .state import StateError

__all__ = ["egress", "find_skill", "home_busy", "inbox", "journal", "label", "live_turns",
           "mcp", "memory", "memory_edit", "memory_forget", "receipts", "schedule_change",
           "schedules", "session", "sessions", "skill", "skill_review", "skill_summary", "skills",
           "tools"]

REVIEW_ACTIONS = ("approve", "reject", "disable", "enable")
SCHEDULE_ACTIONS = ("pause", "resume", "remove")


def clip(value: Any, limit: int = 2000, depth: int = 0) -> Any:
    """A bounded copy for display: long strings cut with a marker, long lists shortened."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"... [{len(value) - limit} more]"
    if depth > 6:
        return "[...]"
    if isinstance(value, dict):
        return {str(key): clip(item, limit, depth + 1) for key, item in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        items = [clip(item, limit, depth + 1) for item in list(value)[:60]]
        return items + ([f"[{len(value) - 60} more]"] if len(value) > 60 else [])
    return value


def label(state: str, *, turn_id: str, active: Iterable[str] | None,
          journal_state: str | None) -> str:
    """A turn's honest label. ``active`` is the set of turns that may be running now, or
    None when that cannot be known here (another process holds the home): then a turn the
    store records as running stays ``running``."""
    if state in ("reserved", "running"):
        return "running" if active is None or turn_id in set(active) else "stale"
    if journal_state == "partial":
        return "partial"
    return state


def home_busy(host: AgentHost) -> bool:
    """Whether another process holds this home's turn lock now (a probe that never waits
    and never recovers anything)."""
    try:
        descriptor = os.open(Path(host.home) / "state" / "host.lock",
                             os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def live_turns(host: AgentHost) -> set[str] | None:
    """The turns an in-process host (no daemon) may see running: its own active turn;
    None (unknown) while another process holds the home's turn lock."""
    own = {(host.active_turn or {}).get("turn_id")} - {None}
    return own if own or not home_busy(host) else None


def _receipt_view(receipt: dict) -> dict:
    """A receipt as the CLI shows it, with long arguments and results shortened."""
    return {**receipt, "request": clip(receipt["request"], 600),
            "result": clip(receipt["result"], 1200)}


def _journal_state(host: AgentHost, turn_id: str) -> str | None:
    top = next((step for step in host.store.journal(host.namespace, turn_id)["steps"]
                if step["kind"] == "turn" and step["turn_id"] == turn_id), None)
    return top["state"] if top else None


def sessions(host: AgentHost, limit: int = 50, *, active: Iterable[str] | None = ()) -> dict:
    """Sessions, newest first; ``last_label`` is the last turn's honest label (``label``),
    ``last_state`` what its chat row records."""
    listed = []
    for item in host.store.list_sessions(host.namespace, limit=limit):
        last = host.store.session_turns(host.namespace, item["session_id"], limit=1)
        turn_id = last[-1]["turn_id"] if last else None
        listed.append({**item, "last_turn_id": turn_id, "last_label": label(
            item["last_state"], turn_id=turn_id, active=active,
            journal_state=_journal_state(host, turn_id) if turn_id else None)})
    return {"sessions": listed, "workspace": str(host.workspace)}


def journal(host: AgentHost, turn_id: str) -> dict:
    found = host.store.journal(host.namespace, turn_id)
    steps = [{"step_id": step["step_id"], "turn_id": step["turn_id"], "kind": step["kind"],
              "seq": step["seq"], "state": step["state"], "detail": clip(step["detail"], 400),
              "result": clip(step["result"], 800), "started_at": step["started_at"],
              "finished_at": step["finished_at"]} for step in found["steps"]]
    return {"turn_id": turn_id, "steps": steps,
            "receipts": [_receipt_view(item) for item in host.receipts(turn_id)],
            "workspace": str(host.workspace)}


def session(host: AgentHost, session_id: str, *, active: Iterable[str] | None = ()) -> dict:
    active = None if active is None else set(active)
    turns = host.store.session_turns(host.namespace, session_id)
    if not turns:
        raise StateError(f"No session {session_id} in this workspace.")
    shown = []
    for turn in turns:
        found = journal(host, turn["turn_id"])
        top = next((step for step in found["steps"] if step["kind"] == "turn"
                    and step["turn_id"] == turn["turn_id"]), None)
        journal_state = top["state"] if top else None
        result = (top or {}).get("result") or {}
        shown.append({
            **turn, "label": label(turn["state"], turn_id=turn["turn_id"], active=active,
                                   journal_state=journal_state),
            "journal_state": journal_state,
            "partial": result.get("partial") if isinstance(result, dict) else None,
            "segments": [step for step in found["steps"] if step["kind"] == "segment"],
            "helpers": [step for step in found["steps"] if step["kind"] == "child"],
            "receipts": found["receipts"]})
    return {"session_id": session_id, "turns": shown, "workspace": str(host.workspace)}


def receipts(host: AgentHost, turn: str | None = None) -> dict:
    return {"receipts": [_receipt_view(item) for item in host.receipts(turn)],
            "workspace": str(host.workspace)}


def schedules(host: AgentHost, include_removed: bool = False) -> dict:
    return {"schedules": [schedule_module.describe(item) for item in host.store.list_schedules(
        host.namespace, include_removed=include_removed)], "workspace": str(host.workspace)}


def schedule_change(host: AgentHost, schedule_id: str, action: str) -> dict:
    if action not in SCHEDULE_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(SCHEDULE_ACTIONS)}")
    record = schedule_module.change_schedule(host.store, host.namespace, schedule_id, action, {},
                                             allowed=host.known_capabilities(),
                                             now=time.time(), writer="owner")
    host.schedules_changed()
    return {"ok": True, "schedule": schedule_module.describe(record)}


def inbox(host: AgentHost, limit: int = 20) -> dict:
    names = {item["schedule_id"]: item["name"]
             for item in host.store.list_schedules(host.namespace, include_removed=True)}
    return {"inbox": [{**run, "name": names.get(run["schedule_id"])}
                      for run in host.store.list_occurrences(host.namespace, limit=limit)],
            "workspace": str(host.workspace)}


def scope_label(host: AgentHost, skill_record: dict) -> str:
    return "profile" if skill_record["scope"] == host.profile_namespace else "workspace"


def skill_summary(host: AgentHost, skill_record: dict) -> dict:
    skill_record = dict(skill_record)
    return {"name": skill_record["name"], "scope": scope_label(host, skill_record),
            "state": skill_record["state"], "review": skill_record["review"],
            "version": skill_record["version"], "pending": skill_record["pending"],
            "description": skill_record["description"],
            "when_to_use": skill_record["when_to_use"],
            "offered": skill_record["state"] == "active"
            and skill_record["review"] != "quarantined",
            "uses": skill_record["uses"], "last_used_at": skill_record["last_used_at"],
            "created_by": skill_record["created_by"], "created_at": skill_record["created_at"],
            "updated_at": skill_record["updated_at"],
            "provenance": {"author": skill_record["author"],
                           "session_id": skill_record["session_id"],
                           "turn_id": skill_record["turn_id"],
                           "workspace": skill_record["workspace"],
                           "at": skill_record["version_created_at"],
                           "tainted": skill_record["tainted"]}}


def find_skill(host: AgentHost, name: str, version: int | None = None) -> dict:
    found = host.store.get_skill([host.namespace, host.profile_namespace], normalize_name(name),
                                 version=version)
    if found is None:
        raise StateError(f"No skill named {name!r} in this workspace or the owner's profile.")
    return found


def skills(host: AgentHost, offered: bool = False) -> dict:
    items = [skill_summary(host, item) for item in
             host.store.list_skills([host.namespace, host.profile_namespace])]
    if offered:
        items = [item for item in items if item["offered"]]
    return {"skills": items, "workspace": str(host.workspace)}


def skill(host: AgentHost, name: str, version: int | None = None) -> dict:
    current = find_skill(host, name)
    shown = find_skill(host, name, version) if version else current
    markdown = render_skill(shown, scope_label=scope_label(host, shown))
    return {"skill": {**skill_summary(host, current), "shown_version": shown["shown_version"],
                      "steps": shown["steps"], "shown_description": shown["description"],
                      "shown_when_to_use": shown["when_to_use"],
                      "version_review": shown["version_review"], "note": shown["note"]},
            "history": host.store.skill_history(current["skill_id"]), "markdown": markdown}


def skill_review(host: AgentHost, name: str, action: str, version: int | None = None) -> dict:
    """The owner's review actions (the CLI's ``skills approve|reject|disable|enable``)."""
    record = find_skill(host, name)
    store = host.store
    if action == "approve":
        changed = store.approve_skill(record["skill_id"], version=version)
    elif action == "reject":
        changed = store.reject_pending_skill(record["skill_id"])
    elif action in ("disable", "enable"):
        changed = store.set_skill(record["skill_id"],
                                  state="disabled" if action == "disable" else "active")
    else:
        raise ValueError(f"action must be one of {', '.join(REVIEW_ACTIONS)}")
    return {"ok": True, "skill": skill_summary(host, changed)}


def _scope(host: AgentHost, scope: str) -> str:
    if scope == "workspace":
        return host.namespace
    if scope == "profile":
        return host.profile_namespace
    raise ValueError("scope must be workspace or profile")


def memory(host: AgentHost, scope: str = "all", search: str | None = None) -> dict:
    if scope not in ("all", "workspace", "profile"):
        raise ValueError("scope must be all, workspace or profile")
    return {"facts": host.memory(search, scope=scope), "workspace": str(host.workspace)}


def memory_edit(host: AgentHost, scope: str, fact_id: str, text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be non-empty")
    fact = host.store.update_fact(_scope(host, scope), str(fact_id), text)
    return {"ok": True, "fact": {**fact, "scope": scope}}


def memory_forget(host: AgentHost, scope: str, fact_id: str) -> dict:
    if not host.store.delete_fact(_scope(host, scope), str(fact_id)):
        raise StateError(f"No {scope} fact {fact_id}.")
    return {"ok": True, "forgotten": str(fact_id), "scope": scope}


def tools(host: AgentHost) -> dict:
    """Every tool the cell can advertise now, and whether an owner chat turn holds it."""
    turn = set(host.known_capabilities()) & (set(TURN_CAPABILITIES) | {
        cap for cap in host.known_capabilities() if cap.startswith("mcp.")})
    specs = host.broker.tool_specs(host.known_capabilities())
    return {"tools": sorted(({"name": spec.name, "capability": spec.capability,
                              "effect": spec.effect, "chat_turn": spec.capability in turn,
                              "description": spec.description[:300]} for spec in specs),
                            key=lambda item: item["name"]),
            "capabilities": list(host.known_capabilities())}


def mcp(host: AgentHost) -> dict:
    """Configured servers (names and transports only: never commands, URLs or env) and the
    state of each one this host has started: offered tools, and the tools withheld until
    the owner trusts the server (changed or new definitions) with the reason."""
    config, error = read_config(Path(host.home) / "reach.json")
    servers, problems = server_specs(config)
    running = {item["server"]: item for item in host.mcp_organ.status() if "server" in item}
    listed = []
    for name, spec in sorted(servers.items()):
        state = running.get(name, {})
        listed.append({"server": name, "capability": f"mcp.{name}",
                       "transport": "http" if "url" in spec else "stdio",
                       "state": state.get("state", "not started"),
                       "tools": state.get("tools", []), "withheld": state.get("withheld", {}),
                       "starts": state.get("starts", 0),
                       "failures": state.get("failures", 0), "error": state.get("error")})
    errors = [item["error"] for item in host.mcp_organ.status() if "server" not in item]
    return {"servers": listed, "problems": ([error] if error else []) + problems + errors}


def egress(host: AgentHost, limit: int = 100) -> dict:
    limit = max(1, min(int(limit), 1000))
    return {"entries": [clip(entry, 400) for entry in host.egress.read(limit)],
            "path": "state/egress.jsonl"}
