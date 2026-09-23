"""Shared organelle contract for the Brainstem Agent cell.

Organs are the cell's bounded capabilities. The broker advertises their tools to
captured Grail workers, validates model-supplied arguments, and invokes them with
a trusted context derived from a verified run grant. Nothing here trusts text
produced by the model.
"""

from __future__ import annotations

import copy
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

__all__ = [
    "CAPABILITY",
    "EFFECTS",
    "MAX_RESULT_CHARS",
    "TOOL_NAME",
    "BindContext",
    "FactStore",
    "InvocationContext",
    "Organ",
    "OrganError",
    "ReceiptSink",
    "ToolResult",
    "ToolSpec",
    "clip",
    "validate_arguments",
]

TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
CAPABILITY = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
EFFECTS = frozenset({"read", "write", "external"})
MAX_DESCRIPTION_CHARS = 2048
MAX_RESULT_CHARS = 64_000
_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})
_KEYWORDS = frozenset({
    "type", "description", "enum", "default", "minLength", "maxLength",
    "minimum", "maximum", "items", "minItems", "maxItems", "properties",
    "required", "additionalProperties",
})


class OrganError(Exception):
    """A refused or failed organ operation. Its message is shown to the model."""


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_schema(schema: Any, path: str, *, top: bool = False) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} must be a schema object")
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise ValueError(f"{path} uses unsupported keywords {sorted(unknown)}")
    kind = schema.get("type")
    if kind not in _TYPES:
        raise ValueError(f"{path}.type must be one of {sorted(_TYPES)}")
    if top and kind != "object":
        raise ValueError("Tool parameters must be an object schema")
    if "description" in schema and not isinstance(schema["description"], str):
        raise ValueError(f"{path}.description must be a string")
    if "enum" in schema:
        values = schema["enum"]
        if (
            not isinstance(values, list)
            or not values
            or any(isinstance(item, (dict, list)) for item in values)
        ):
            raise ValueError(f"{path}.enum must be a non-empty list of scalars")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and not _count(schema[key]):
            raise ValueError(f"{path}.{key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and not _number(schema[key]):
            raise ValueError(f"{path}.{key} must be a finite number")
    for key in ("properties", "required", "additionalProperties"):
        if key in schema and kind != "object":
            raise ValueError(f"{path}.{key} is only valid for objects")
    if "items" in schema and kind != "array":
        raise ValueError(f"{path}.items is only valid for arrays")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError(f"{path}.properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{path}.properties keys must be non-empty strings")
            _check_schema(child, f"{path}.properties[{name!r}]")
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) or name not in properties for name in required)
            or len(set(required)) != len(required)
        ):
            raise ValueError(f"{path}.required must list unique declared properties")
        if "additionalProperties" in schema and not isinstance(
            schema["additionalProperties"], bool
        ):
            raise ValueError(f"{path}.additionalProperties must be a boolean")
    elif kind == "array":
        if "items" not in schema:
            raise ValueError(f"{path}.items is required for arrays")
        _check_schema(schema["items"], f"{path}.items")
    if "default" in schema:
        try:
            _coerce(schema["default"], schema, f"{path}.default")
        except OrganError as error:
            raise ValueError(f"{path}.default does not match its schema: {error}") from None


def _coerce(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    kind = schema["type"]
    if kind == "string":
        if not isinstance(value, str):
            raise OrganError(f"{path} must be a string")
        if len(value) < schema.get("minLength", 0):
            raise OrganError(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise OrganError(f"{path} is longer than {schema['maxLength']} characters")
    elif kind == "integer":
        if isinstance(value, float) and _number(value) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise OrganError(f"{path} must be an integer")
    elif kind == "number":
        if not _number(value):
            raise OrganError(f"{path} must be a finite number")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise OrganError(f"{path} must be true or false")
    elif kind == "array":
        if not isinstance(value, list):
            raise OrganError(f"{path} must be an array")
        if len(value) < schema.get("minItems", 0):
            raise OrganError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise OrganError(f"{path} has more than {schema['maxItems']} items")
        value = [
            _coerce(item, schema["items"], f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    else:
        value = _object(value, schema, path)
    if kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise OrganError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise OrganError(f"{path} must be at most {schema['maximum']}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        raise OrganError(f"{path} must be one of {allowed}")
    return value


def _object(value: Any, schema: Mapping[str, Any], path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise OrganError(f"{path} must be an object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    # Models send null for optional properties they do not use: that means "not given".
    given = {name: item for name, item in value.items()
             if item is not None or name not in properties or name in required}
    for name, child in properties.items():
        if name not in given and "default" in child:
            given[name] = copy.deepcopy(child["default"])
    missing = [name for name in required if name not in given]
    if missing:
        raise OrganError(f"{path} is missing required {', '.join(missing)}")
    result: dict[str, Any] = {}
    for name, item in given.items():
        if name in properties:
            result[name] = _coerce(item, properties[name], f"{path}.{name}")
        elif schema.get("additionalProperties") is True:
            result[name] = copy.deepcopy(item)
    return result


def validate_arguments(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """Validate model-supplied arguments against a declared ToolSpec schema.

    Unknown properties are dropped unless the schema explicitly allows them. A null
    optional property counts as not given, and a missing property takes its declared
    ``default`` (so a required property with a default is always sent by models but
    may be omitted by the owner). Raises OrganError with a model-safe message.
    """
    return _object(arguments, schema, "arguments")


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Bound text for the model, keeping the beginning and the end."""
    if not isinstance(text, str):
        raise TypeError("clip() requires text")
    if limit < 64:
        raise ValueError("clip() limit is too small")
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    marker = f"\n...[{omitted} characters omitted]...\n"
    keep = max(0, limit - len(marker))
    head = keep * 2 // 3
    return text[:head] + marker + text[len(text) - (keep - head):]


@dataclass(frozen=True)
class ToolSpec:
    """One model-callable tool exported by an organ."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    capability: str
    effect: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("Tool names must match [a-z][a-z0-9_]{0,47}")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description) > MAX_DESCRIPTION_CHARS
        ):
            raise ValueError("Tool descriptions must be bounded, non-empty text")
        _check_schema(self.parameters, f"{self.name}.parameters", top=True)
        object.__setattr__(self, "parameters", copy.deepcopy(dict(self.parameters)))
        if not isinstance(self.capability, str) or CAPABILITY.fullmatch(self.capability) is None:
            raise ValueError("Capabilities must match [a-z][a-z0-9_.-]{0,63}")
        if self.effect not in EFFECTS:
            raise ValueError(f"Tool effect must be one of {sorted(EFFECTS)}")
        if not _number(self.timeout_seconds) or not 0 < self.timeout_seconds <= 3600:
            raise ValueError("Tool timeouts must be between 0 and 3600 seconds")

    def to_wire(self) -> dict[str, Any]:
        """JSON object sent to the bridge; never includes capability internals."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": copy.deepcopy(dict(self.parameters)),
            "timeout_seconds": float(self.timeout_seconds),
        }


@dataclass(frozen=True)
class BindContext:
    """Trusted facts about the run, supplied when a worker binds its grant."""

    owner: str
    workspace: str
    namespace: str
    session_id: str
    turn_id: str
    workspace_root: Path
    user_input: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class InvocationContext:
    """Trusted facts for one tool call; model arguments are passed separately.

    ``call_tool(tool, arguments, allowed=...)`` (set by the broker) makes an *inner* call
    on behalf of this call: its own receipt, the same grant and capability checks, and
    only tools in ``allowed``; it returns ``{"ok": bool, "content": str}``."""

    owner: str
    workspace: str
    namespace: str
    session_id: str
    turn_id: str
    call_id: str
    workspace_root: Path
    capabilities: tuple[str, ...]
    deadline: float
    cancelled: threading.Event = field(
        default_factory=threading.Event, compare=False, repr=False
    )
    call_tool: Callable[..., dict] | None = field(default=None, compare=False, repr=False)

    def remaining(self) -> float:
        """Seconds until the time.monotonic() deadline, never negative."""
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        """Refuse further work after cancellation or deadline."""
        if self.cancelled.is_set():
            raise OrganError("The run was cancelled.")
        if self.remaining() <= 0:
            raise OrganError("The tool call ran out of time.")


@dataclass(frozen=True)
class ToolResult:
    """Model-visible content plus private receipt evidence."""

    content: str
    ok: bool = True
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("ToolResult.content must be text")
        if not isinstance(self.ok, bool):
            raise TypeError("ToolResult.ok must be a boolean")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("ToolResult.evidence must be a mapping")


@runtime_checkable
class Organ(Protocol):
    """A bounded capability of the cell."""

    name: str

    def tools(self) -> Sequence[ToolSpec]: ...

    def invoke(
        self, context: InvocationContext, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult: ...

    def context(self, context: BindContext) -> str | None: ...


class ReceiptSink(Protocol):
    """Durable evidence of every authorized tool invocation (Store v2)."""

    def begin_receipt(
        self, namespace: str, turn_id: str, call_id: str, tool: str,
        capability: str, request: Mapping[str, Any],
    ) -> str: ...

    def finish_receipt(
        self, receipt_id: str, state: str, result: Mapping[str, Any]
    ) -> None: ...


class FactStore(Protocol):
    """Scoped durable memory facts (Store v2)."""

    def add_fact(
        self, namespace: str, text: str, *, source_turn: str | None = None
    ) -> dict[str, Any]: ...

    def update_fact(self, namespace: str, fact_id: str, text: str) -> dict[str, Any]: ...

    def delete_fact(self, namespace: str, fact_id: str) -> bool: ...

    def list_facts(self, namespace: str, *, limit: int = 200) -> list[dict[str, Any]]: ...

    def search_facts(
        self, namespace: str, query: str, *, limit: int = 20
    ) -> list[dict[str, Any]]: ...
