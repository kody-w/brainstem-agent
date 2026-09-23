"""Delegation organ: hand independent sub-tasks to helpers that run in parallel.

``delegate_tasks`` (``agents.delegate``) runs each task as a child turn on its own fresh
Grail worker with its own history (only its task), in the parent's workspace and with at
most the parent's capabilities (a task may declare fewer). The host journals every child
before it starts, bounds depth, count and parallelism, cancels children with the parent,
and returns every child's result, failures included, in one joined answer. The work is
the host's (``AgentHost._delegate``); this organ is its tool surface.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec

_TASK = {"type": "object", "properties": {
    "task": {"type": "string", "minLength": 1, "maxLength": 4000,
             "description": "The complete, self-contained instruction for this helper: it "
                            "sees nothing else (no conversation, no other task)."},
    "name": {"type": "string", "maxLength": 60, "description": "A short label."},
    "capabilities": {"type": "array", "maxItems": 16,
                     "items": {"type": "string", "maxLength": 64},
                     "description": "Optional: fewer capabilities than yours, e.g. "
                                    "['files.read', 'files.write']. Default: yours."}},
         "required": ["task"]}


class DelegateOrgan:
    name = "delegate"

    def __init__(self, run: Callable[..., ToolResult]) -> None:
        self._run = run

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            "delegate_tasks", "Hand off independent sub-tasks to helper agents that work at the "
            "same time, each on its own fresh worker with its own conversation, and get all of "
            "their results back together. Use it for parts of a request that do not depend on "
            "each other. Each helper sees only its own task text and works in this workspace "
            "with your tools (or fewer).",
            {"type": "object", "properties": {
                "tasks": {"type": "array", "minItems": 1, "maxItems": 8, "items": _TASK,
                          "description": "One entry per helper."}},
             "required": ["tasks"]}, "agents.delegate", "external", timeout_seconds=1800)]

    def context(self, context: BindContext) -> str | None:
        return None

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        if tool != "delegate_tasks":
            raise OrganError(f"Unknown delegation tool {tool!r}.")
        context.check()
        tasks = [dict(task) for task in arguments["tasks"]]
        return self._run(context, tasks)
