"""Sessions organ: the model searches the owner's past turns in this workspace."""

from __future__ import annotations

from typing import Any, Mapping

from ..session_index import SessionIndex, format_hits
from ..state import StateError
from .base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec

__all__ = ["SessionOrgan"]


class SessionOrgan:
    name = "sessions"

    def __init__(self, store, index: SessionIndex) -> None:
        self.store = store
        self.index = index

    def tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            "session_search", "Search this workspace's past conversations (the owner's messages "
            "and your answers) by keywords, for example when the owner asks what they told you "
            "earlier. Returns snippets with session ids and times, best match first.",
            {"type": "object", "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500,
                          "description": "Keywords: names, topics, places, file names."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                          "description": "How many turns to return (default 5)."}},
             "required": ["query"]}, "sessions.read", "read")]

    def context(self, context: BindContext) -> str | None:
        return None  # a one-line pointer is part of the learned-context block

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        context.check()
        if tool != "session_search":
            raise OrganError(f"Unknown sessions tool {tool!r}.")
        try:
            result = self.index.search(self.store, context.namespace, arguments["query"],
                                       limit=arguments.get("limit", 5))
        except StateError as error:
            raise OrganError(f"Session search failed: {error}") from None
        return ToolResult(format_hits(result, current_session=context.session_id), evidence={
            "engine": result["engine"], "matched": result["matched"],
            "turns": [hit["turn_id"] for hit in result["hits"]]})
