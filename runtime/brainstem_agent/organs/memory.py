"""Memory organ: durable facts in the run's workspace scope or the owner's profile scope.

``remember`` saves to the workspace (default) or, with ``scope: profile``, to the owner's
profile, which every workspace is offered. ``recall`` searches both (BM25, see
``retrieval``) and counts each hit as a use; ``forget`` deletes from either scope. In the
host the facts reach the model through the budgeted learned-context block
(``knowledge.py``); standalone, ``context()`` renders the workspace block itself.

After a turn read content from outside the cell (``web_fetch``, ``web_search``, an MCP tool,
or a helper's answer), ``remember`` and ``forget`` run only when the owner's own words in
that turn ask for it (remember, memorize, memory or forget, outside quoted text; a
scheduled run's prompt counts only while the owner wrote it), so a hostile page cannot
plant or erase memories. The host's ``prior_tools`` includes what a turn inherited: a
helper's parent's outside reads, and those of the conversation that wrote a scheduled
run's prompt.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from ..knowledge import Knowledge, assemble, fact_items, profile_namespace
from ..retrieval import rank
from ..state import CredentialRefused, StateError
from .base import BindContext, FactStore, InvocationContext, OrganError, ToolResult, ToolSpec
from .skills import _SCHEDULED, _owner_words

_SCOPES = ("workspace", "profile")
OUTSIDE = ("web_fetch", "web_search", "mcp__", "delegate_tasks")
_ASKED = re.compile(r"\b(remember|memori[sz]e|memory|memories|forget)\b", re.IGNORECASE)


class MemoryOrgan:
    name = "memory"

    def __init__(self, facts: FactStore, *,
                 profile_namespace: Callable[[str], str] = profile_namespace,
                 in_context: bool = True,
                 prior_tools: Callable[[str], Sequence[str]] = lambda _turn: (),
                 turn_input: Callable[[str], str] = lambda _turn: "",
                 schedule_creator: Callable[[str], str | None] = lambda _id: None) -> None:
        self.facts = facts
        self.profile_namespace = profile_namespace
        self.in_context = in_context
        self.prior_tools, self.turn_input = prior_tools, turn_input
        self.schedule_creator = schedule_creator
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("remember", "Save a durable fact so it can be recalled in later "
                     "conversations. scope 'profile' is for facts about the owner that hold in "
                     "every workspace (name, preferences, units); 'workspace' is for facts about "
                     "this workspace's work.",
                     {"type": "object", "properties": {
                         "text": {"type": "string", "minLength": 1, "maxLength": 2000,
                                  "description": "The fact, as one short self-contained "
                                                 "sentence."},
                         "scope": {"type": "string", "enum": list(_SCOPES),
                                   "default": "workspace",
                                   "description": "'profile' (the owner, every workspace) or "
                                                  "'workspace' (this workspace only)."}},
                      "required": ["text", "scope"]}, "memory.write", "write"),
            ToolSpec("recall", "Search saved memories (this workspace's and the owner's profile) "
                     "by keywords.",
                     {"type": "object", "properties": {
                         "query": {"type": "string", "minLength": 1, "maxLength": 500},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                         "scope": {"type": "string", "enum": ["all", *_SCOPES]}},
                      "required": ["query"]}, "memory.read", "read"),
            ToolSpec("forget", "Delete one saved memory (workspace or profile) by its fact_id.",
                     {"type": "object", "properties": {"fact_id": {
                         "type": "string", "minLength": 1, "maxLength": 64}},
                      "required": ["fact_id"]}, "memory.write", "write"),
        ]

    def context_count(self, turn_id: str) -> int:
        with self._lock:
            return self._counts.pop(turn_id, 0)

    def note_context(self, turn_id: str, count: int) -> None:
        with self._lock:
            self._counts[turn_id] = count
            if len(self._counts) > 256:
                self._counts.pop(next(iter(self._counts)))

    def context(self, context: BindContext) -> str | None:
        if not self.in_context:
            return None
        facts = fact_items(self.facts.list_facts(context.namespace, limit=5000))
        text, report = assemble(Knowledge(context.user_input, time.time(), memory=facts,
                                          capabilities=context.capabilities))
        self.note_context(context.turn_id, report["sections"]["memory"]["shown"])
        return text

    def _namespaces(self, context: InvocationContext, scope: str) -> list[tuple[str, str]]:
        chosen = []
        if scope in ("all", "workspace"):
            chosen.append(("workspace", context.namespace))
        if scope in ("all", "profile"):
            chosen.append(("profile", self.profile_namespace(context.owner)))
        return chosen

    def _outside(self, context: InvocationContext) -> list[str]:
        """The outside tools this turn read, unless the owner's own words asked for memory."""
        if context.turn_id.startswith("direct_"):
            return []
        read = sorted({tool for tool in self.prior_tools(context.turn_id)
                       if tool.startswith(OUTSIDE)})
        text = self.turn_input(context.turn_id) if read else ""
        scheduled = _SCHEDULED.match(text)
        if scheduled and self.schedule_creator(scheduled.group(1)) != "owner":
            text = ""
        return [] if _ASKED.search(_owner_words(text)) else read

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        context.check()
        read = self._outside(context) if tool in ("remember", "forget") else []
        if read:
            return ToolResult(f"Not done: this turn read content from outside the cell "
                              f"({', '.join(read)}) and the owner's own words did not ask to "
                              "change memory. Tell the owner instead.", ok=False,
                              evidence={"refused": "outside_content", "read": read})
        try:
            if tool == "remember":
                scope = arguments.get("scope") or "workspace"
                namespace = dict(self._namespaces(context, scope))[scope]
                fact = self.facts.add_fact(namespace, arguments["text"],
                                           source_turn=context.turn_id)
                where = "the owner's profile" if scope == "profile" else "this workspace"
                return ToolResult(f"Remembered in {where} as {fact['fact_id']}.",
                                  evidence={"fact_id": fact["fact_id"], "scope": scope})
            if tool == "recall":
                found = []
                for scope, namespace in self._namespaces(context, arguments.get("scope") or "all"):
                    uses = getattr(self.facts, "fact_uses", None)
                    items = fact_items(self.facts.list_facts(namespace, limit=5000),
                                       uses(namespace) if uses else None)
                    found += [(entry, scope) for entry in rank(arguments["query"], items,
                                                              now=time.time()) if entry.matched]
                found.sort(key=lambda pair: -pair[0].score)
                found = found[: arguments.get("limit", 10)]
                if not found:
                    return ToolResult("No matching memories.", evidence={"matches": 0})
                note = getattr(self.facts, "note_fact_uses", None)
                if note is not None:
                    note([entry.item.key for entry, _scope in found])
                text = "\n".join(f"- [{entry.item.key}] ({scope}) {entry.item.text}"
                                 for entry, scope in found)
                return ToolResult(text, evidence={"matches": len(found)})
            if tool == "forget":
                for scope, namespace in self._namespaces(context, "all"):
                    if self.facts.delete_fact(namespace, arguments["fact_id"]):
                        return ToolResult("Forgotten.", evidence={"fact_id": arguments["fact_id"],
                                                                  "scope": scope})
                raise OrganError("No memory with that fact_id exists in this workspace or the "
                                 "owner's profile.")
        except CredentialRefused as error:  # recorded as a refusal, never with the text
            return ToolResult(f"Not saved: {error}", ok=False,
                              evidence={"refused": "credential", "kinds": error.kinds,
                                        "scope": arguments.get("scope") or "workspace"})
        except StateError as error:
            raise OrganError(f"Memory refused the request: {error}") from None
        raise OrganError(f"Unknown memory tool {tool!r}.")
