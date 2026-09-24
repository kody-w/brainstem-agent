"""Local observability: the cell's event log, log reading with filters, and statistics
computed from the store. Nothing here sends anything off the machine.

The event log (``logs/events.jsonl``, 0600) holds one JSON object per operational event:
turns started, finished and refused, credential and health changes, daemon lifecycle,
maintenance (backup, restore, upgrade, prune, compaction). It never holds the owner's
words, answers or credentials (text fields are redacted), and it is rotated by size, so it
stays bounded (``max_bytes`` times ``keep + 1``).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import fnmatch
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .credentials import redact_credentials

__all__ = ["EventLog", "LEVELS", "compute_stats", "parse_when", "percentiles", "read_events",
           "read_text_log", "rotate_file"]

EVENTS = "events.jsonl"
DEFAULT_MAX_BYTES = 1 << 20
DEFAULT_KEEP = 3
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
_LOCKS: dict[str, threading.Lock] = {}


def rotate_file(path: Path, *, max_bytes: int, keep: int, truncate: bool = False) -> bool:
    """Rotate ``path`` once it is over ``max_bytes``: ``path.N`` (N <= ``keep``) hold older
    parts. ``truncate`` copies and truncates in place (for a file another process holds open
    for appending, such as the daemon's output); otherwise the file is renamed."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    keep = max(1, int(keep))
    try:
        oldest = path.with_name(f"{path.name}.{keep}")
        oldest.unlink(missing_ok=True)
        for number in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{number}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{number + 1}"))
        first = path.with_name(f"{path.name}.1")
        if truncate:
            with open(path, "rb") as source:
                descriptor = os.open(first, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                                     0o600)
                with os.fdopen(descriptor, "wb") as target:
                    for block in iter(lambda: source.read(1 << 16), b""):
                        target.write(block)
            os.truncate(path, 0)
        else:
            os.replace(path, first)
        return True
    except OSError:
        return False


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return _TOKEN_SHAPES.sub(r"\1[REDACTED]", redact_credentials(value))[:1000]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in list(value)[:50]]
    return value


def _iso(moment: float) -> str:
    return dt.datetime.fromtimestamp(moment, dt.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class EventLog:
    """Append-only, size-rotated JSON lines; writing never fails the caller."""

    def __init__(self, home: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 keep: int = DEFAULT_KEEP) -> None:
        self.path = Path(home) / "logs" / EVENTS
        self.max_bytes, self.keep = max_bytes, keep
        self._lock = _LOCKS.setdefault(str(self.path), threading.Lock())

    def write(self, event: str, *, level: str = "info", **fields: Any) -> None:
        now = time.time()
        record = {"at": _iso(now), "ts": round(now, 3), "level": level, "event": event,
                  "pid": os.getpid(), **_clean(fields)}
        line = json.dumps(record, default=str)
        if len(line) > 8000:
            line = json.dumps({key: record[key] for key in ("at", "ts", "level", "event", "pid")}
                              | {"truncated": True})
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                guard = os.open(self.path.parent / ".events.lock",
                                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(guard, fcntl.LOCK_EX)
                    rotate_file(self.path, max_bytes=self.max_bytes, keep=self.keep)
                    descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                         | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                finally:
                    os.close(guard)
            except OSError:
                pass


def parse_when(text: str | None, *, now: float | None = None) -> float | None:
    """``30s``, ``15m``, ``2h`` or ``7d`` ago, or an ISO 8601 date(-time) (local time when it
    has no offset), as epoch seconds."""
    if text is None or text == "":
        return None
    now = time.time() if now is None else now
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*", text)
    if match:
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        return now - float(match.group(1)) * unit
    try:
        moment = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not a time: {text!r} (use 30m, 2h, 7d or an ISO date-time)") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


def _lines(paths: Iterable[Path]) -> Iterable[str]:
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                yield from handle
        except OSError:
            continue


def _rotated(path: Path, keep: int = 9) -> list[Path]:
    """``path`` and its rotated parts, oldest first."""
    older = [path.with_name(f"{path.name}.{number}") for number in range(keep, 0, -1)]
    return [item for item in [*older, path] if item.exists()]


def read_events(home: Path | str, *, since: float | None = None, until: float | None = None,
                level: str | None = None, event: str | None = None, turn: str | None = None,
                grep: str | None = None, limit: int = 100) -> list[dict]:
    """Events from the log (rotated parts included), oldest first, the newest ``limit``."""
    floor = LEVELS.get(level or "debug", 0)
    found: list[dict] = []
    for line in _lines(_rotated(Path(home) / "logs" / EVENTS)):
        if grep and grep.lower() not in line.lower():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        moment = record.get("ts") or 0
        if (since is not None and moment < since) or (until is not None and moment >= until):
            continue
        if LEVELS.get(record.get("level"), 20) < floor:
            continue
        if event and not fnmatch.fnmatchcase(str(record.get("event")), event):
            continue
        if turn and turn not in (record.get("turn_id"), record.get("parent_turn")):
            continue
        found.append(record)
    return found[-max(1, limit):]


def read_text_log(paths: Iterable[Path], *, grep: str | None = None, limit: int = 100,
                  since: float | None = None) -> list[dict]:
    """Lines of plain text logs (whole files older than ``since`` are skipped)."""
    found: list[dict] = []
    for path in paths:
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        for line in _lines([path]):
            text = _TOKEN_SHAPES.sub(r"\1[REDACTED]", line.rstrip("\n"))
            if grep and grep.lower() not in text.lower():
                continue
            found.append({"file": path.name, "line": text[:2000]})
    return found[-max(1, limit):]


def read_egress(home: Path | str, *, since: float | None = None, grep: str | None = None,
                limit: int = 100) -> list[dict]:
    found = []
    for line in _lines(_rotated(Path(home) / "state" / "egress.jsonl", keep=1)):
        if grep and grep.lower() not in line.lower():
            continue
        try:
            record = json.loads(line)
            moment = parse_when(record.get("at")) if record.get("at") else None
        except ValueError:
            continue
        if since is not None and (moment is None or moment < since):
            continue
        found.append(record)
    return found[-max(1, limit):]


def percentiles(values: Iterable[float], points: Iterable[int] = (50, 90, 95, 99)) -> dict:
    """Nearest-rank percentiles (``p50`` ...), ``max``, ``mean`` and ``count``."""
    ordered = sorted(value for value in values if value is not None and math.isfinite(value))
    if not ordered:
        return {"count": 0}
    result: dict[str, Any] = {"count": len(ordered)}
    for point in points:
        index = max(0, math.ceil(point / 100 * len(ordered)) - 1)
        result[f"p{point}"] = round(ordered[index], 3)
    result["max"] = round(ordered[-1], 3)
    result["mean"] = round(sum(ordered) / len(ordered), 3)
    return result


def compute_stats(store, *, since: float | None = None, until: float | None = None,
                  namespace: str | None = None, home: Path | str | None = None) -> dict:
    """Turns, outcomes, uncertainty, latency percentiles, tool usage and scheduled-run fire
    delays in a window, from the local store (and refusals from the local event log)."""
    rows = store.operations_rows(since=since, until=until, namespace=namespace)
    outcomes: dict[str, int] = {}
    latency, succeeded_latency, segments = [], [], 0
    for turn in rows["turns"]:
        outcome = "partial" if turn.get("journal_state") == "partial" else turn["state"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if turn.get("started_at") and turn.get("finished_at"):
            seconds = turn["finished_at"] - turn["started_at"]
            latency.append(seconds)
            if outcome == "succeeded":
                succeeded_latency.append(seconds)
        segments += turn.get("segments") or 0
    tools: dict[str, dict] = {}
    durations: dict[str, list] = {}
    for receipt in rows["receipts"]:
        entry = tools.setdefault(receipt["tool"], {"calls": 0, "states": {}})
        entry["calls"] += 1
        entry["states"][receipt["state"]] = entry["states"].get(receipt["state"], 0) + 1
        if receipt.get("finished_at"):
            durations.setdefault(receipt["tool"], []).append(
                receipt["finished_at"] - receipt["created_at"])
    for name, entry in tools.items():
        entry["seconds"] = percentiles(durations.get(name, []), (50, 90))
    runs: dict[str, int] = {}
    delays, late, missed, skipped = [], 0, 0, {}
    for run in rows["runs"]:
        runs[run["state"]] = runs.get(run["state"], 0) + 1
        if run["state"] == "skipped":
            skipped[run.get("reason") or "unknown"] = skipped.get(run.get("reason") or
                                                                  "unknown", 0) + 1
        else:
            delays.append(max(0.0, run["claimed_at"] - run["scheduled_at"]))
        late += 1 if run.get("late_seconds") else 0
        missed += run.get("missed_count") or 0
    uncertain_receipts = sum(entry["states"].get("uncertain", 0) for entry in tools.values())
    document = {
        "window": {"since": since, "until": until, "namespace": namespace},
        "source": "the local store; nothing leaves this machine",
        "turns": {"total": len(rows["turns"]), "outcomes": outcomes,
                  "grail_requests": segments, "helpers": rows["helpers"]},
        "uncertain": {"turns": outcomes.get("uncertain", 0), "receipts": uncertain_receipts,
                      "total": outcomes.get("uncertain", 0) + uncertain_receipts},
        "latency_seconds": {"all": percentiles(latency), "succeeded": percentiles(
            succeeded_latency)},
        "tools": dict(sorted(tools.items(), key=lambda item: -item[1]["calls"])),
        "tool_calls": len(rows["receipts"]),
        "scheduled_runs": {"total": len(rows["runs"]), "states": runs,
                           "fire_delay_seconds": percentiles(delays), "late": late,
                           "missed_instants": missed, "skipped": skipped},
    }
    if home is not None:
        refused: dict[str, int] = {}
        for record in read_events(home, since=since, until=until, event="turn.refused",
                                  limit=100_000):
            reason = str(record.get("reason") or "unknown")
            refused[reason] = refused.get(reason, 0) + 1
        document["refused_turns"] = {"by_reason": refused, "total": sum(refused.values()),
                                     "source": "the local event log"}
    return document


def summarize(stats: Mapping[str, Any]) -> str:
    """A few human lines for ``stats``."""
    turns, latency = stats["turns"], stats["latency_seconds"]["all"]
    outcomes = ", ".join(f"{count} {name}" for name, count in sorted(turns["outcomes"].items()))
    lines = [f"turns: {turns['total']} ({outcomes or 'none'}), {turns['grail_requests']} Grail "
             f"request(s), {turns['helpers']} helper(s)",
             f"uncertain: {stats['uncertain']['total']} (turns {stats['uncertain']['turns']}, "
             f"receipts {stats['uncertain']['receipts']})"]
    if latency.get("count"):
        lines.append(f"latency: p50 {latency['p50']}s, p90 {latency['p90']}s, p99 "
                     f"{latency['p99']}s, max {latency['max']}s")
    top = list(stats["tools"].items())[:8]
    if top:
        lines.append("tools: " + ", ".join(f"{name} {entry['calls']}" for name, entry in top))
    runs = stats["scheduled_runs"]
    if runs["total"]:
        delay = runs["fire_delay_seconds"]
        lines.append(f"scheduled runs: {runs['total']} ({runs['states']}), fire delay p50 "
                     f"{delay.get('p50', '-')}s p90 {delay.get('p90', '-')}s, late {runs['late']}")
    if stats.get("refused_turns", {}).get("total"):
        lines.append(f"refused turns: {stats['refused_turns']['by_reason']}")
    return "\n".join(lines)
