"""Streaming for the companion surfaces: answer deltas and per-request event streams.

``StreamTee`` wraps the host's worker factory (the host is unchanged): each worker's
``stream_chat`` is observed line by line, and Grail's ``delta`` frames of a session that
a surface is following are handed to that surface's sink as they arrive (before the host
reads the line). Helpers stream on their own sessions, which nobody follows, so only the
root turn's answer is streamed; ``done.response`` stays authoritative.

Streamed text is display-only and is redacted before any surface sees it: the recorded
answer's own rules (bearer tokens, ``key = value`` secrets) and every credential shape
(``credentials.redact_credentials``), on a rolling buffer (``StreamRedactor``). Text is
released only once no secret can still grow across it (a word still arriving, a secret's
name waiting for its value, an open private-key block), so a secret split across Grail's
fragments never shows, not even in part.

``Requests`` is the daemon's registry of turns started through ``POST /v1/requests``:
each has a state (``queued`` until the one worker is free, ``running``, ``streaming`` once
answer text arrives, then the turn's own final state), a bounded event buffer with
sequence numbers (``request.queued``, ``request.running``, the engine's progress events,
``answer.delta``, ``request.finished``) and waiters for the SSE route. Finished requests
are kept (newest 64) so a surface can re-attach and read the end.
"""

from __future__ import annotations

import collections
import json
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from .adapter import _redact as _redact_answer  # the recorded answer's own redaction
from .credentials import _SECRET_NAMES, redact_credentials

__all__ = ["Request", "Requests", "StreamRedactor", "StreamTee", "delta_text", "follow",
           "redact_stream"]

MAX_EVENTS = 20000
KEEP_FINISHED = 64
MAX_UNFINISHED = 16  # turns waiting for (or on) the one worker; more are refused
TERMINAL = ("succeeded", "failed", "uncertain", "cancelled", "partial")


def delta_text(line: bytes | str) -> str | None:
    """The text of one Grail SSE ``data:`` line when it is a ``delta`` frame, else None."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line[5:].strip())
    except ValueError:
        return None
    if isinstance(payload, dict) and payload.get("type") == "delta" and isinstance(
            payload.get("text"), str):
        return payload["text"]
    return None


def redact_stream(text: str) -> str:
    """Streamed text as a surface may show it: redacted as the recorded answer is, and for
    every credential shape besides."""
    return redact_credentials(_redact_answer(text))


# The end of a text where a secret may be under way: "Bearer" or a secret's name (and its
# separator, and an open quoted value) with the value still to come.
_OPEN_SECRET = re.compile(
    rf"""(?:\bbearer
          |(?<![A-Za-z0-9])(?:[a-z0-9_-]{{1,64}}[_-])?(?:{_SECRET_NAMES})(?:[_-]id)?["']?\s*
           (?:(?:[:=]|\bis\b)\s*(?:["'][^"'\r\n]*)?)?
        )\s*\Z""", re.IGNORECASE | re.VERBOSE)
_PEM_START = re.compile(r"-----BEGIN[A-Z0-9 ]*")
_KEY_HEADER = re.compile(r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_KEY_END = re.compile(r"-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_WINDOW = 2048  # how far back a secret's name may start before its value


def _word_start(text: str, end: int) -> int:
    """The position after the last whitespace before ``end`` (0 when there is none)."""
    index = end
    while index > 0 and not text[index - 1].isspace():
        index -= 1
    return index


def _open_key_block(text: str, end: int) -> int | None:
    """Where a private-key block that ``end`` would split starts (or a PEM header that is
    still arriving and may name one), else None."""
    position = text.find("-----BEGIN", 0, end)
    while position >= 0:
        header = _PEM_START.match(text, position)
        rest = text[header.end():header.end() + 5]
        if "-----".startswith(rest) and header.end() + len(rest) == len(text):
            return position  # the header has not ended yet
        key = _KEY_HEADER.match(text, position)
        if key is not None:
            closing = _KEY_END.search(text, key.end())
            if closing is None or closing.end() > end:
                return position
        position = text.find("-----BEGIN", position + 1, end)
    return None


def _safe_cut(text: str) -> int:
    """How much of ``text`` can be released now: no secret can grow across the cut."""
    cut = len(text)
    while True:
        new = _word_start(text, cut)
        block = _open_key_block(text, new)
        if block is not None:
            new = _word_start(text, block)
        name = _OPEN_SECRET.search(text, max(0, new - _WINDOW), new)
        if name is not None:
            new = _word_start(text, name.start())
        if new == cut:
            return cut
        cut = new


class StreamRedactor:
    """A rolling redaction buffer for one streamed answer: ``feed`` returns the text that
    is now safe to show (redacted), ``flush`` the rest once the stream has ended."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        self._pending += text
        cut = _safe_cut(self._pending)
        released, self._pending = self._pending[:cut], self._pending[cut:]
        return redact_stream(released) if released else ""

    def flush(self) -> str:
        released, self._pending = self._pending, ""
        return redact_stream(released) if released else ""


class _TeeWorker:
    """A worker whose answer stream is observed; everything else is the real worker's."""

    def __init__(self, inner: Any, tee: "StreamTee") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_tee", tee)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def stream_chat(self, request: Mapping, grant: str, **options: Any) -> Iterator[bytes]:
        sink = self._tee.route(request.get("session_id"))
        for line in self._inner.stream_chat(request, grant, **options):
            if sink is not None:
                text = delta_text(line)
                if text:
                    try:
                        sink(text)
                    except Exception:
                        pass  # a surface never breaks the turn
            yield line


class StreamTee:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[str, Callable[[str], None]] = {}

    def wrap(self, base: Callable[..., Any] | None, default: Callable[[], Callable[..., Any]]):
        """A worker factory producing observed workers (``default()`` gives the host's own
        factory when ``base`` is None; it is looked up at call time)."""
        def factory(**options: Any) -> _TeeWorker:
            return _TeeWorker((base or default())(**options), self)
        return factory

    def attach(self, session_id: str, sink: Callable[[str], None]) -> None:
        with self._lock:
            self._routes[session_id] = sink

    def detach(self, session_id: str | None) -> None:
        with self._lock:
            self._routes.pop(session_id, None)

    def route(self, session_id: Any) -> Callable[[str], None] | None:
        with self._lock:
            return self._routes.get(session_id) if isinstance(session_id, str) else None


def follow(tee: StreamTee, emit: Callable[[dict], None]) -> Callable[[dict], None]:
    """A progress sink that also streams the root turn's answer: when the engine reports
    ``turn.started`` (with its session), that session's deltas become ``answer.delta``
    events (through a ``StreamRedactor``: a step's held-back tail is released when that
    step's stream has ended, before its ``segment.finished``); ``turn.finished`` stops
    them."""
    state: dict[str, Any] = {"session": None, "segment": 0}
    lock = threading.Lock()  # deltas (the worker's stream) and progress (any thread)
    redactor = StreamRedactor()

    def release(text: str) -> None:
        if text:
            emit({"event": "answer.delta", "text": text, "segment": state["segment"]})

    def delta(text: str) -> None:
        with lock:
            release(redactor.feed(text))

    def sink(event: dict) -> None:
        kind = event.get("event")
        with lock:
            if not event.get("child"):
                if kind == "turn.started" and isinstance(event.get("session_id"), str):
                    state["session"] = event["session_id"]
                    tee.attach(event["session_id"], delta)
                elif kind == "segment.started":
                    release(redactor.flush())
                    state["segment"] = event.get("segment", 0)
                elif kind in ("segment.finished", "turn.finished"):
                    release(redactor.flush())  # that step's stream has ended
            emit(event)
        if kind == "turn.finished" and not event.get("child"):
            tee.detach(state["session"])

    def close() -> None:
        tee.detach(state["session"])
        with lock:
            release(redactor.flush())
    sink.close = close  # type: ignore[attr-defined]
    return sink


class Request:
    def __init__(self, request_id: str, message: str) -> None:
        self.request_id = request_id
        self.message_chars = len(message)
        self.created_at = time.time()
        self.state = "queued"
        self.session_id: str | None = None
        self.turn_id: str | None = None
        self.result: dict | None = None
        self.events: collections.deque = collections.deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.dropped = 0
        self.condition = threading.Condition()
        self.cancel = threading.Event()
        self.first_delta_at: float | None = None

    @property
    def finished(self) -> bool:
        return self.result is not None

    def emit(self, event: Mapping[str, Any]) -> None:
        with self.condition:
            document = {**event, "request_id": self.request_id}
            kind = document.get("event")
            if kind == "turn.started" and not document.get("child"):
                self.session_id = document.get("session_id")
                self.turn_id = document.get("turn_id")
            elif kind == "request.running":
                self.state = "running"
            elif kind == "answer.delta" and self.state == "running":
                self.state = "streaming"
                self.first_delta_at = time.time()
            elif kind == "request.finished":
                self.result = document.get("result")
                self.state = (self.result or {}).get("state", "failed")
                self.session_id = (self.result or {}).get("session_id") or self.session_id
                self.turn_id = (self.result or {}).get("turn_id") or self.turn_id
            self.seq += 1
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            self.events.append((self.seq, {**document, "seq": self.seq, "t": time.time()}))
            self.condition.notify_all()

    def after(self, seq: int, timeout: float) -> tuple[list[tuple[int, dict]], bool]:
        """Events after ``seq`` (waiting up to ``timeout`` for one); and whether the
        request has finished with nothing further to send."""
        with self.condition:
            if not any(number > seq for number, _ in self.events) and not self.finished:
                self.condition.wait(timeout)
            events = [(number, event) for number, event in self.events if number > seq]
            return events, self.finished and (not events or events[-1][0] == self.seq)

    def describe(self) -> dict:
        with self.condition:
            return {"request_id": self.request_id, "state": self.state,
                    "session_id": self.session_id, "turn_id": self.turn_id,
                    "created_at": self.created_at, "events": self.seq,
                    "dropped_events": self.dropped, "result": self.result}


class Requests:
    """The daemon's streamed turn requests, newest last (bounded)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: collections.OrderedDict[str, Request] = collections.OrderedDict()

    def create(self, message: str) -> Request:
        request = Request("req_" + secrets.token_hex(12), message)
        with self._lock:
            if sum(not item.finished for item in self._items.values()) >= MAX_UNFINISHED:
                raise ValueError(f"{MAX_UNFINISHED} turns are already waiting or running; "
                                 "wait for one to finish.")
            self._items[request.request_id] = request
            finished = [key for key, item in self._items.items() if item.finished]
            while len(finished) > KEEP_FINISHED:
                self._items.pop(finished.pop(0), None)
        return request

    def get(self, request_id: Any) -> Request | None:
        with self._lock:
            return self._items.get(request_id) if isinstance(request_id, str) else None

    def active_turns(self) -> set[str]:
        with self._lock:
            return {item.turn_id for item in self._items.values()
                    if not item.finished and item.turn_id}

    def count(self) -> int:
        with self._lock:
            return len(self._items)
