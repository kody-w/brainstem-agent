"""Pure, offline normalization of the frozen M0 core contract.

Limits count UTF-8 bytes, not Python characters, and refuse rather than truncate.
Unknown request-message and response metadata is dropped without traversing it.
No transport, provider, credential store, core import, or fallback exists here.

Redaction is deliberately limited to response/log text: credential assignments
ending in api_key/apikey, token, secret, password/passwd, secret_access_key or
access_key_id (also hyphenated, with an optional <= 64-character name prefix),
and unquoted ``Bearer <value>`` tokens. Quoted assignment values may contain
escaped quotes but not literal newlines. Unlabelled, encoded, multiline, or
otherwise disguised secrets are not detected. Input and session IDs are not
redacted. This is not a universal secret scanner.

SSE accepts ``event: delta|agent|done|error`` with a JSON object in ``data:``, or
data-only/default-message events with that discriminator in ``type``. Grail
0.6.16 emits informational ``agent`` frames after each tool round; like deltas
they are never treated as the answer. An
explicit event and a supplied type must agree. Deltas are never concatenated
into an answer. Every event must end with a blank line, including the terminal
done event. The entire iterable is consumed: after done, only terminated
comment lines/blank frames are allowed. A final complete comment line needs no
extra blank line. Duplicate terminals, subsequent fields or events, and
incomplete trailing lines/event frames all refuse. Standard id/retry fields are
ignored before done; other fields and event names refuse.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
import json
import math
import re
from typing import Literal, NoReturn, TypedDict


class CoreContractError(ValueError):
    """The input cannot be accepted as a successful, bounded core exchange."""


@dataclass(frozen=True, slots=True)
class AdapterLimits:
    """Trusted, per-call positive-integer limits; bool is not an integer limit."""

    max_text_bytes: int = 64 * 1024
    max_session_id_bytes: int = 256
    max_history_messages: int = 128
    max_history_bytes: int = 256 * 1024
    max_log_lines: int = 256
    max_log_line_bytes: int = 8 * 1024
    max_log_bytes: int = 64 * 1024
    max_sse_frame_bytes: int = 256 * 1024
    max_sse_total_bytes: int = 1024 * 1024
    max_sse_chunks: int = 8192
    max_sse_frames: int = 1024
    max_json_depth: int = 32
    max_json_number_chars: int = 128

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise CoreContractError("adapter limits must be positive integers")


DEFAULT_LIMITS = AdapterLimits()


class ConversationMessage(TypedDict):
    role: Literal["user", "assistant", "tool"]
    content: str


class CoreRequest(TypedDict):
    user_input: str
    session_id: str
    conversation_history: list[ConversationMessage]


class CoreResponse(TypedDict):
    response: str
    agent_logs: list[str]
    session_id: str


_REDACTED = "[REDACTED]"
_ASSIGNMENT = re.compile(
    r"""(?P<prefix>(?<![\w.-])(?P<key_quote>["']?)
    (?:[a-z0-9_-]{1,64}[_-])?
    (?:api[_-]?key|token|secret|password|passwd|
       secret[_-]access[_-]key|access[_-]key[_-]id)
    (?P=key_quote)[ \t]*[:=][ \t]*)
    (?:"(?P<double>(?:\\[^\r\n]|[^"\\\r\n])*)"
       |'(?P<single>(?:\\[^\r\n]|[^'\\\r\n])*)'
       |(?P<bare>[^\s,;'"<>{}\[\]]+))""",
    re.IGNORECASE | re.ASCII | re.VERBOSE,
)
_BEARER = re.compile(
    r"""(\bBearer[ \t]+)[^\s,;'"<>(){}\[\]]+""",
    re.IGNORECASE | re.ASCII,
)
_ERROR_TAGS = frozenset(
    {
        "error", "failed", "failure", "denied", "unauthorized", "forbidden",
        "no_copilot_access",
    }
)
_MAX_ERROR_TAG_LENGTH = max(map(len, _ERROR_TAGS))


def _check_limits(limits: AdapterLimits) -> None:
    if not isinstance(limits, AdapterLimits):
        raise CoreContractError("limits must be an AdapterLimits instance")


def _text_bytes(
    value: object, label: str, maximum: int, *, nonempty: bool = False
) -> bytes:
    if not isinstance(value, str):
        raise CoreContractError(f"{label} must be a string")
    if len(value) > maximum:
        raise CoreContractError(f"{label} exceeds its byte limit")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise CoreContractError(f"{label} must be valid Unicode") from None
    if len(encoded) > maximum:
        raise CoreContractError(f"{label} exceeds its byte limit")
    if nonempty and not value.strip():
        raise CoreContractError(f"{label} must not be empty")
    return encoded


def _session(value: object, limits: AdapterLimits) -> str:
    _text_bytes(value, "session_id", limits.max_session_id_bytes, nonempty=True)
    assert isinstance(value, str)
    return value


def _bound_session(value: object, expected: str, limits: AdapterLimits) -> str:
    session_id = _session(value, limits)
    if session_id != expected:
        raise CoreContractError("core session binding does not match")
    return session_id


def _redact_assignment(match: re.Match[str]) -> str:
    quote = '"' if match.group("double") is not None else (
        "'" if match.group("single") is not None else ""
    )
    return match.group("prefix") + quote + _REDACTED + quote


def _redact(text: str) -> str:
    text = _BEARER.sub(lambda match: match.group(1) + _REDACTED, text)
    return _ASSIGNMENT.sub(_redact_assignment, text)


def _reject_error_shape(payload: Mapping[str, object]) -> None:
    # Even null error fields are ambiguous and intentionally fail closed.
    if any(
        key in payload
        for key in ("error", "errors", "error_code", "no_copilot_access")
    ):
        raise CoreContractError("core returned an error-shaped payload")
    if payload.get("success") is False or payload.get("ok") is False:
        raise CoreContractError("core reported an unsuccessful result")
    for key in ("status", "type", "code"):
        value = payload.get(key)
        if (
            isinstance(value, str)
            and len(value) <= _MAX_ERROR_TAG_LENGTH
            and value.casefold() in _ERROR_TAGS
        ):
            raise CoreContractError("core returned an error status")
    status = payload.get("status_code")
    if type(status) is int and status >= 400:
        raise CoreContractError("core returned an error status")


def build_core_request(
    user_input: str,
    session_id: str,
    history: list[ConversationMessage],
    *,
    limits: AdapterLimits = DEFAULT_LIMITS,
) -> CoreRequest:
    """Copy bounded JSON-style history; permit empty history-message content.

    History must be a list of objects. Only role/content are copied; system
    messages refuse. The aggregate history limit counts content bytes.
    """
    _check_limits(limits)
    _text_bytes(user_input, "user_input", limits.max_text_bytes, nonempty=True)
    session_id = _session(session_id, limits)
    if not isinstance(history, list):
        raise CoreContractError("history must be a list")
    if len(history) > limits.max_history_messages:
        raise CoreContractError("history exceeds its message limit")
    normalized: list[ConversationMessage] = []
    total = 0
    for message in history:
        if not isinstance(message, dict):
            raise CoreContractError("history messages must be objects")
        role = message.get("role")
        if not isinstance(role, str) or role not in ("user", "assistant", "tool"):
            raise CoreContractError("history has an unsupported role")
        content = message.get("content")
        total += len(_text_bytes(content, "history content", limits.max_text_bytes))
        if total > limits.max_history_bytes:
            raise CoreContractError("history exceeds its aggregate byte limit")
        assert isinstance(content, str)
        normalized.append({"role": role, "content": content})
    return {
        "user_input": user_input,
        "session_id": session_id,
        "conversation_history": normalized,
    }


def _logs(value: object, limits: AdapterLimits) -> list[str]:
    if isinstance(value, str):
        _text_bytes(value, "agent_logs", limits.max_log_bytes)
        lines = value.splitlines()
    elif isinstance(value, list):
        lines = value
    else:
        raise CoreContractError("agent_logs must be a string or list of strings")
    if len(lines) > limits.max_log_lines:
        raise CoreContractError("agent_logs exceeds its line limit")
    result: list[str] = []
    raw_total = 0
    redacted_total = 0
    for line in lines:
        raw_total += len(_text_bytes(line, "log line", limits.max_log_line_bytes))
        if raw_total > limits.max_log_bytes:
            raise CoreContractError("agent_logs exceeds its aggregate byte limit")
        redacted = _redact(line)
        redacted_total += len(
            _text_bytes(redacted, "redacted log line", limits.max_log_line_bytes)
        )
        if redacted_total > limits.max_log_bytes:
            raise CoreContractError("redacted logs exceed their aggregate byte limit")
        result.append(redacted)
    return result


def normalize_core_response(
    payload: object,
    expected_session_id: str,
    *,
    limits: AdapterLimits = DEFAULT_LIMITS,
) -> CoreResponse:
    """Require all three fields and strip extras; do not echo refused input.

    Flat logs use str.splitlines() (no phantom final line); list entries are
    preserved, not split. Both original and redacted retained text are bounded.
    Presence of an error/errors/error_code/no_copilot_access field refuses,
    even if null, as do explicit unsuccessful flags and known error statuses.
    """
    _check_limits(limits)
    expected = _session(expected_session_id, limits)
    if not isinstance(payload, dict):
        raise CoreContractError("core response must be an object")
    _reject_error_shape(payload)
    if not {"response", "agent_logs", "session_id"} <= payload.keys():
        raise CoreContractError("core response is missing required fields")
    session_id = _bound_session(payload["session_id"], expected, limits)
    response = payload["response"]
    _text_bytes(response, "response", limits.max_text_bytes, nonempty=True)
    response = _redact(response)
    _text_bytes(response, "redacted response", limits.max_text_bytes, nonempty=True)
    return {
        "response": response,
        "agent_logs": _logs(payload["agent_logs"], limits),
        "session_id": session_id,
    }


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CoreContractError("SSE JSON contains a duplicate member")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise CoreContractError("SSE JSON contains a non-finite number")


def _decode_json(text: str, limits: AdapterLimits) -> dict[str, object]:
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > limits.max_json_depth:
                raise CoreContractError("SSE JSON exceeds its nesting limit")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise CoreContractError("SSE data must be strict JSON")

    def integer(value: str) -> int:
        if len(value) > limits.max_json_number_chars:
            raise CoreContractError("SSE JSON exceeds its number limit")
        return int(value)

    def floating(value: str) -> float:
        if len(value) > limits.max_json_number_chars:
            raise CoreContractError("SSE JSON exceeds its number limit")
        result = float(value)
        if not math.isfinite(result):
            raise CoreContractError("SSE JSON contains a non-finite number")
        return result

    try:
        payload = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
            parse_int=integer,
            parse_float=floating,
        )
    except CoreContractError:
        raise
    except (ValueError, RecursionError):
        raise CoreContractError("SSE data must be strict JSON") from None
    if not isinstance(payload, dict):
        raise CoreContractError("SSE event data must be an object")
    return payload


class _SSEParser:
    def __init__(self, expected: str, limits: AdapterLimits) -> None:
        self.expected = expected
        self.limits = limits
        self.line = bytearray()
        self.pending_cr = False
        self.frame_bytes = 0
        self.frame_count = 0
        self.event: str | None = None
        self.data: list[str] = []
        self.terminal: CoreResponse | None = None

    def _count_byte(self) -> None:
        self.frame_bytes += 1
        if self.frame_bytes > self.limits.max_sse_frame_bytes:
            raise CoreContractError("SSE frame exceeds its byte limit")

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self.pending_cr:
                self.pending_cr = False
                if byte == 10:
                    self._count_byte()
                    self._line_end()
                    continue
                self._line_end()
            self._count_byte()
            if byte == 13:
                self.pending_cr = True
            elif byte == 10:
                self._line_end()
            else:
                self.line.append(byte)

    def _line_end(self) -> None:
        line = self.line.decode("utf-8")
        self.line.clear()
        if not line:
            self._dispatch()
            self.frame_bytes = 0
            return
        if line.startswith(":"):
            return
        if self.terminal is not None:
            raise CoreContractError("SSE fields or events after done are forbidden")
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            if self.event is not None:
                raise CoreContractError("SSE frame has duplicate event fields")
            self.event = value
        elif field == "data":
            self.data.append(value)
        elif field not in ("id", "retry"):
            raise CoreContractError("SSE frame has an unsupported field")

    def _dispatch(self) -> None:
        self.frame_count += 1
        if self.frame_count > self.limits.max_sse_frames:
            raise CoreContractError("SSE stream exceeds its frame limit")
        event, data = self.event, self.data
        self.event, self.data = None, []
        if event is None and not data:
            return
        if event == "error":
            raise CoreContractError("core returned an SSE error event")
        if not data:
            raise CoreContractError("SSE event is missing JSON data")
        payload = _decode_json("\n".join(data), self.limits)
        if event in (None, "", "message"):
            discriminator = payload.get("type")
            if not isinstance(discriminator, str):
                raise CoreContractError("SSE event type must be a string")
            event = discriminator
        elif "type" in payload and payload["type"] != event:
            raise CoreContractError("SSE event and payload type disagree")
        if event not in ("delta", "agent", "done", "error"):
            raise CoreContractError("SSE event has an unsupported type")
        _reject_error_shape(payload)
        if "session_id" in payload:
            _bound_session(payload["session_id"], self.expected, self.limits)
        if event == "done":
            self.terminal = normalize_core_response(
                payload, self.expected, limits=self.limits
            )

    def finish(self) -> CoreResponse:
        if self.pending_cr:
            self.pending_cr = False
            self._line_end()
        if self.line or self.event is not None or self.data:
            raise CoreContractError("SSE stream ends with an incomplete frame")
        if self.terminal is None:
            raise CoreContractError("SSE stream ended without done")
        return self.terminal


def normalize_sse(
    chunks: Iterable[str],
    expected_session_id: str,
    *,
    limits: AdapterLimits = DEFAULT_LIMITS,
) -> CoreResponse:
    """Normalize a finite iterable of text chunks, never a byte/string stream.

    Source iteration failures become CoreContractError, without source error
    text. Chunk count bounds empty-chunk floods; frame/total byte bounds include
    comments and line endings. The caller must supply a nonblocking iterable;
    this pure synchronous function cannot impose transport/iterator timeouts.
    """
    _check_limits(limits)
    expected = _session(expected_session_id, limits)
    if isinstance(chunks, (str, bytes, bytearray, dict)):
        raise CoreContractError("SSE chunks must be an iterable of text chunks")
    try:
        iterator = iter(chunks)
    except Exception:
        raise CoreContractError("SSE chunks must be iterable") from None
    parser = _SSEParser(expected, limits)
    count = 0
    total = 0
    while True:
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        except Exception:
            raise CoreContractError("SSE chunk source failed") from None
        count += 1
        if count > limits.max_sse_chunks:
            raise CoreContractError("SSE stream exceeds its chunk limit")
        encoded = _text_bytes(
            chunk, "SSE chunk", limits.max_sse_total_bytes - total
        )
        total += len(encoded)
        parser.feed(encoded)
    return parser.finish()
