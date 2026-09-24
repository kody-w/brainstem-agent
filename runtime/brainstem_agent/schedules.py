"""Durable schedules: wall-clock time math, the schedule organ and the next-fire loop.

Kinds: ``once`` (a local wall time), ``interval`` (every N seconds of elapsed time
from an anchor) and 5-field ``cron`` evaluated on the wall clock of an IANA
timezone (``zoneinfo``). The DST rule: each matching wall time fires exactly once.
A wall time that exists fires at the first instant the clock reads it, so a time
repeated by fall-back fires only in its first pass (PEP 495 ``fold=0``). A wall
time skipped by spring-forward fires as if the old offset still held, shifted
forward by the gap (``fold=0`` again: 02:30 becomes 03:30 EDT); coinciding instants
fire once. Every scheduled instant is an integer UTC second, and an occurrence's
identity is (schedule, instant, manual), so it is claimed at most once, even across
restarts. ``test_cell_clock_oracle`` checks this against a minute-by-minute walk of
the clock.

One loop (``Scheduler.run``) sleeps until the earliest pending instant of the
store's next-fire index, recomputed on every change or wake-up and at least every
``MAX_NAP`` seconds (so clock jumps and system sleep are noticed), then claims the
occurrence and advances its schedule in one transaction and runs the turn on a
helper thread. Meanwhile the loop records each instant of that schedule that comes
due (and each run-now request) as ``skipped`` (overlap), when it arrives. An
instant that is not the latest pending one, predates this daemon's start or is more
than ``LATE_GRACE`` seconds old is missed: the schedule's policy runs the latest
missed instant once, marked late (``run-latest-once-late``, the default), or
records it ``skipped`` (``skip``). Every occurrence records ``missed_count``, the
scheduled instants it accounts for that never ran.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import threading
import time
import zoneinfo
from typing import Any, Callable, Iterable, Mapping, Sequence

from .organs.base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec
from .organs.skills import untrusted
from .state import ConflictError, StateError

__all__ = ["COUNT_LIMIT", "Cron", "LATE_GRACE", "MAX_NAP", "MISSED_POLICIES", "ScheduleError",
           "ScheduleOrgan", "Scheduler", "change_schedule", "count_fires", "create_schedule",
           "describe", "find_schedule", "latest_fire",
           "local_iso", "local_zone", "next_fire", "normalize_timezone", "run_occurrence"]

MAX_NAP = 30.0
LATE_GRACE = 60.0
MIN_INTERVAL = 60
COUNT_LIMIT = 100_000
MISSED_POLICIES = ("run-latest-once-late", "skip")
_WHEN = ("in_seconds", "at", "every_seconds", "cron")
_SEARCH_DAYS = 366 * 8 + 2
_MONTHS = {name: number for number, name in
           enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_WEEKDAYS = {name: number for number, name in enumerate("sun mon tue wed thu fri sat".split())}


class ScheduleError(ValueError):
    """An invalid schedule request; the message is safe for the owner and the model."""


# -- time ----------------------------------------------------------------------------
def normalize_timezone(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or name.strip().startswith("/"):
        raise ScheduleError("The timezone must be an IANA name such as America/New_York.")
    name = name.strip()
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"Unknown IANA timezone {name!r}.") from None
    return name


def local_zone(environ: Mapping[str, str] | None = None) -> str:
    """The owner's IANA zone: ``TZ``, else the target of ``/etc/localtime``, else UTC."""
    name = (os.environ if environ is None else environ).get("TZ", "").lstrip(":")
    if not name:
        target = os.path.realpath("/etc/localtime")
        name = target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else "UTC"
    try:
        return normalize_timezone(name)
    except ScheduleError:
        return "UTC"


def local_iso(instant: float | None, zone: str) -> str | None:
    if instant is None:
        return None
    return dt.datetime.fromtimestamp(instant, zoneinfo.ZoneInfo(zone)).isoformat()


def _cron_value(text: str, names: Mapping[str, int]) -> int:
    text = text.strip().lower()
    if text in names:
        return names[text]
    if text.isdigit():
        return int(text)
    raise ScheduleError(f"Invalid cron value {text!r}.")


def _cron_field(text: str, low: int, high: int, names: Mapping[str, int]) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step = part.partition("/")
        if base == "*":
            start, end = low, high
        else:
            first, dash, last = base.partition("-")
            start = _cron_value(first, names)
            end = _cron_value(last, names) if dash else (high if slash else start)
        if slash and not step.isdigit():
            raise ScheduleError(f"Invalid cron step in {part!r}.")
        stride = int(step) if slash else 1
        if stride < 1 or not low <= start <= end <= high:
            raise ScheduleError(f"Cron field {part!r} is outside {low}-{high}.")
        values.update(range(start, end + 1, stride))
    return frozenset(values)


class Cron:
    """A 5-field cron expression (minute hour day month weekday) with Vixie day semantics."""

    def __init__(self, expression: Any) -> None:
        fields = expression.split() if isinstance(expression, str) else []
        if len(fields) != 5:
            raise ScheduleError("A cron expression has 5 fields: minute hour day month weekday.")
        self.expression = " ".join(fields)
        self.minutes = sorted(_cron_field(fields[0], 0, 59, {}))
        self.hours = sorted(_cron_field(fields[1], 0, 23, {}))
        self.days = _cron_field(fields[2], 1, 31, {})
        self.months = _cron_field(fields[3], 1, 12, _MONTHS)
        self.weekdays = {day % 7 for day in _cron_field(fields[4], 0, 7, _WEEKDAYS)}
        # Vixie cron: a restricted day-of-month OR a restricted weekday, unless either is '*'.
        self.either = not (fields[2].startswith("*") or fields[4].startswith("*"))

    def _matches(self, day: dt.date) -> bool:
        if day.month not in self.months:
            return False
        dom, dow = day.day in self.days, day.isoweekday() % 7 in self.weekdays
        return (dom or dow) if self.either else (dom and dow)

    def _instants(self, day: dt.date, tz: dt.tzinfo) -> list[int]:
        return sorted({int(dt.datetime(day.year, day.month, day.day, hour, minute,
                                       tzinfo=tz).timestamp())
                       for hour in self.hours for minute in self.minutes})

    def next_after(self, after: float, tz: dt.tzinfo) -> int | None:
        # A skipped wall time is shifted forward, so wall order and instant order can
        # differ near a transition: keep scanning through the found instant's local day.
        start = dt.datetime.fromtimestamp(after, tz).date() - dt.timedelta(days=1)
        found = None
        for offset in range(_SEARCH_DAYS):
            day = start + dt.timedelta(days=offset)
            if self._matches(day):
                later = [instant for instant in self._instants(day, tz) if instant > after]
                if later and (found is None or later[0] < found):
                    found = later[0]
            if found is not None and day >= dt.datetime.fromtimestamp(found, tz).date():
                return found
        return found

    def latest_at_or_before(self, at: float, tz: dt.tzinfo) -> int | None:
        start = dt.datetime.fromtimestamp(at, tz).date() + dt.timedelta(days=1)
        found = None
        for offset in range(_SEARCH_DAYS):
            day = start - dt.timedelta(days=offset)
            if self._matches(day):
                earlier = [instant for instant in self._instants(day, tz) if instant <= at]
                if earlier and (found is None or earlier[-1] > found):
                    found = earlier[-1]
            if found is not None and day < dt.datetime.fromtimestamp(found, tz).date():
                return found
        return found

    def count_between(self, low: float, high: float, tz: dt.tzinfo, limit: int) -> int:
        """Distinct instants in [low, high], counted up to ``limit``."""
        count, previous = 0, set()
        day = dt.datetime.fromtimestamp(low, tz).date() - dt.timedelta(days=1)
        last = dt.datetime.fromtimestamp(high, tz).date() + dt.timedelta(days=1)
        while day <= last and count < limit:
            current = ({instant for instant in self._instants(day, tz) if low <= instant <= high}
                       if self._matches(day) else set())
            count += len(current - previous)  # a shifted instant may repeat the next day's
            previous = current
            day += dt.timedelta(days=1)
        return min(count, limit)


def _once(spec: Mapping[str, Any], tz: dt.tzinfo) -> int:
    wall = dt.datetime.fromisoformat(spec["at"])
    return int(wall.replace(tzinfo=tz, fold=int(spec.get("fold", 0))).timestamp())


def next_fire(spec: Mapping[str, Any], zone: str, after: float) -> int | None:
    """The first scheduled instant strictly after ``after`` (None: never again)."""
    tz = zoneinfo.ZoneInfo(zone)
    if spec["kind"] == "once":
        instant = _once(spec, tz)
        return instant if instant > after else None
    if spec["kind"] == "interval":
        every, anchor = spec["every"], spec["anchor"]
        return anchor if after < anchor else anchor + (int((after - anchor) // every) + 1) * every
    return Cron(spec["cron"]).next_after(after, tz)


def latest_fire(spec: Mapping[str, Any], zone: str, at: float) -> int | None:
    """The last scheduled instant at or before ``at`` (None: none yet)."""
    tz = zoneinfo.ZoneInfo(zone)
    if spec["kind"] == "once":
        instant = _once(spec, tz)
        return instant if instant <= at else None
    if spec["kind"] == "interval":
        every, anchor = spec["every"], spec["anchor"]
        return None if at < anchor else anchor + int((at - anchor) // every) * every
    return Cron(spec["cron"]).latest_at_or_before(at, tz)


def count_fires(spec: Mapping[str, Any], zone: str, low: float, high: float, *,
                limit: int = COUNT_LIMIT) -> int:
    """How many scheduled instants fall in [low, high] (counted up to ``limit``)."""
    if high < low:
        return 0
    tz = zoneinfo.ZoneInfo(zone)
    if spec["kind"] == "once":
        return int(low <= _once(spec, tz) <= high)
    if spec["kind"] == "interval":
        every, anchor = spec["every"], spec["anchor"]
        first, last = math.ceil((max(low, anchor) - anchor) / every), math.floor(
            (high - anchor) / every)
        return max(0, min(limit, last - first + 1))
    return Cron(spec["cron"]).count_between(low, high, tz, limit)


def _integer(value: Any, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScheduleError(f"{label} must be a whole number of at least {minimum}.")
    return value


def parse_when(arguments: Mapping[str, Any], zone: str, now: float) -> dict:
    """Normalise exactly one of in_seconds, at, every_seconds or cron into a spec.

    ``in_seconds`` together with ``every_seconds`` sets the first run of an interval.
    """
    given = [key for key in _WHEN if arguments.get(key) not in (None, "")]
    if not given or (len(given) > 1 and given != ["in_seconds", "every_seconds"]):
        raise ScheduleError("Give exactly one of in_seconds, at, every_seconds or cron "
                            "(in_seconds may also set the first run of every_seconds).")
    tz = zoneinfo.ZoneInfo(zone)
    start = None
    if "in_seconds" in given:
        start = math.ceil(now) + _integer(arguments["in_seconds"], "in_seconds", 1)
    if "every_seconds" in given:
        every = _integer(arguments["every_seconds"], "every_seconds", MIN_INTERVAL)
        return {"kind": "interval", "every": every,
                "anchor": start if start is not None else math.ceil(now) + every}
    if given == ["cron"]:
        cron = Cron(arguments["cron"])
        if cron.next_after(now, tz) is None:
            raise ScheduleError("That cron expression never fires.")
        return {"kind": "cron", "cron": cron.expression}
    if start is not None:
        wall = dt.datetime.fromtimestamp(start, tz)
    else:
        try:
            wall = dt.datetime.fromisoformat(str(arguments["at"]).strip())
        except ValueError:
            raise ScheduleError("'at' must be an ISO 8601 date-time such as "
                                "2026-09-23T09:00.") from None
        wall = wall.astimezone(tz) if wall.tzinfo is not None else wall.replace(tzinfo=tz)
    spec = {"kind": "once", "at": wall.replace(tzinfo=None, microsecond=0).isoformat(),
            "fold": wall.fold}
    if next_fire(spec, zone, now) is None:
        raise ScheduleError("That time has already passed.")
    return spec


def _when_text(spec: Mapping[str, Any], zone: str) -> str:
    if spec["kind"] == "once":
        return f"once at {spec['at']} {zone}"
    if spec["kind"] == "interval":
        return f"every {spec['every']}s from {local_iso(spec['anchor'], zone)}"
    return f"cron '{spec['cron']}' in {zone}"


def describe(schedule: Mapping[str, Any]) -> dict:
    """The owner- and model-facing view: adds local times, drops the internal namespace."""
    view = {key: value for key, value in schedule.items() if key != "namespace"}
    view["when"] = _when_text(schedule["spec"], schedule["timezone"])
    view["next_fire_local"] = local_iso(schedule["next_fire_at"], schedule["timezone"])
    return view


# -- shared owner/model operations -----------------------------------------------------
def _capabilities(requested: Iterable[str] | None, allowed: Iterable[str],
                  default: Iterable[str]) -> list[str]:
    allowed = tuple(allowed)
    chosen = list(dict.fromkeys(default if requested is None else requested))
    outside = [item for item in chosen if item not in allowed]
    if outside or not chosen:
        raise ScheduleError(f"A schedule may only hold capabilities from {list(allowed)}; "
                            f"refused: {outside or 'an empty list'}.")
    return chosen


def _text_field(value: Any, label: str, limit: int, *, line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ScheduleError(f"{label} must be non-empty text of at most {limit} characters.")
    if line and any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ScheduleError(f"{label} must be a single line of text.")
    return value.strip()


def create_schedule(store, *, namespace: str, workspace: str, prompt: Any,
                    when: Mapping[str, Any], capabilities: Iterable[str] | None,
                    allowed: Iterable[str], default: Iterable[str], created_by: str,
                    now: float, timezone: Any = None, name: Any = None,
                    missed_policy: Any = None) -> dict:
    zone = normalize_timezone(timezone) if timezone else local_zone()
    spec = parse_when(when, zone, now)
    prompt = _text_field(prompt, "The prompt", 4000)
    policy = missed_policy or MISSED_POLICIES[0]
    if policy not in MISSED_POLICIES:
        raise ScheduleError(f"missed_policy must be one of {list(MISSED_POLICIES)}.")
    return store.create_schedule(
        namespace, workspace=workspace, created_by=created_by, spec=spec, timezone=zone,
        name=_text_field(name, "The name", 80, line=True) if name else
        " ".join(prompt[:60].split()),
        prompt=prompt, capabilities=_capabilities(capabilities, allowed, default),
        missed_policy=policy, next_fire_at=next_fire(spec, zone, now))


def find_schedule(store, namespace: str, key: Any) -> dict:
    """A schedule by id or, failing that, by its exact (case-insensitive) name."""
    try:
        return store.get_schedule(namespace, key)
    except StateError:
        listed = store.list_schedules(namespace)
        wanted = str(key).strip().casefold()
        matches = [item for item in listed if item["name"].casefold() == wanted]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(f"{item['schedule_id']} '{item['name']}'" for item in listed) or "none"
        raise ScheduleError(f"No single schedule matches {key!r}; schedules here: {known}.") \
            from None


def change_schedule(store, namespace: str, schedule_id: str, action: str,
                    fields: Mapping[str, Any], *, allowed: Iterable[str], now: float,
                    acting: Iterable[str] | None = None, writer: str | None = None,
                    outside: Iterable[str] = ()) -> dict:
    """pause | resume | edit | run_now | remove, by the owner or by a turn.

    ``acting`` is a turn's own capabilities: it may only change schedules whose
    capabilities it holds, and an edit can never add capabilities beyond them.
    ``writer`` (``owner`` or ``turn:<id>``) becomes the schedule's ``created_by`` when it
    rewrites the prompt: ``created_by`` always names who wrote the prompt its runs follow,
    and only an owner-written prompt counts as the owner's request. ``outside`` lists the
    tools whose outside text (text the cell did not write) the turn has already read: such
    a turn may not rewrite the prompt or name of a schedule the owner wrote.
    """
    current = find_schedule(store, namespace, schedule_id)
    schedule_id = current["schedule_id"]
    if acting is not None and not set(current["capabilities"]) <= set(acting):
        raise ScheduleError("This schedule holds capabilities this conversation does not "
                            "have, so it cannot be changed from here.")
    if current["state"] == "removed":
        raise ScheduleError("That schedule was removed.")
    tainted = sorted(set(outside))
    if (action == "edit" and tainted and current["created_by"] == "owner"
            and (fields.get("prompt") is not None or fields.get("name") is not None)):
        raise ScheduleError(
            f"This conversation has read text the cell did not write ({', '.join(tainted)}), so "
            "it cannot rewrite the prompt or name of a schedule the owner wrote; the owner can "
            "change them with `schedules edit`.")
    zone, spec = current["timezone"], current["spec"]
    if action == "pause":
        return store.update_schedule(namespace, schedule_id, state="paused")
    if action == "remove":
        return store.update_schedule(namespace, schedule_id, state="removed", run_now_at=None)
    if action == "run_now":
        return store.update_schedule(namespace, schedule_id, run_now_at=now)
    if action == "resume":
        upcoming = next_fire(spec, zone, now)
        if upcoming is None:
            raise ScheduleError("Its one-shot time has passed; edit it with a new time.")
        return store.update_schedule(namespace, schedule_id, state="active",
                                     next_fire_at=upcoming)
    if action != "edit":
        raise ScheduleError("action must be pause, resume, edit, run_now or remove.")
    changes: dict[str, Any] = {}
    if fields.get("timezone"):
        zone = normalize_timezone(fields["timezone"])
        if spec["kind"] == "once":
            spec = {**spec, "fold": 0}
    if any(fields.get(key) not in (None, "") for key in _WHEN):
        spec = parse_when(fields, zone, now)
    if fields.get("prompt") is not None:
        changes["prompt"] = _text_field(fields["prompt"], "The prompt", 4000)
        if writer is not None:
            changes["created_by"] = writer
    if fields.get("name") is not None:
        changes["name"] = _text_field(fields["name"], "The name", 80, line=True)
    if fields.get("capabilities") is not None:
        changes["capabilities"] = _capabilities(fields["capabilities"],
                                                acting if acting is not None else allowed, ())
    if fields.get("missed_policy") is not None:
        if fields["missed_policy"] not in MISSED_POLICIES:
            raise ScheduleError(f"missed_policy must be one of {list(MISSED_POLICIES)}.")
        changes["missed_policy"] = fields["missed_policy"]
    if (spec, zone) != (current["spec"], current["timezone"]):
        changes.update(spec=spec, timezone=zone)
        if current["state"] != "paused":
            upcoming = next_fire(spec, zone, now)
            if upcoming is None:
                raise ScheduleError("That time has already passed.")
            changes.update(next_fire_at=upcoming, state="active")
    if not changes:
        raise ScheduleError("Nothing to edit: give a new prompt, name, time, timezone, "
                            "capabilities or missed_policy.")
    return store.update_schedule(namespace, schedule_id, **changes)


def _missed_text(count: Any) -> str:
    if not count:
        return ""
    return (f"; {count} earlier scheduled run{'s were' if count != 1 else ' was'} missed "
            "and not run")


def _run_view(occurrence: Mapping[str, Any]) -> str:
    result = occurrence.get("result") or {}
    text = result.get("response") or result.get("error") or ""
    late = f", {occurrence['late_seconds']:.0f}s late" if occurrence.get("late_seconds") else ""
    reason = f" ({occurrence['reason']})" if occurrence.get("reason") else ""
    missed = (f", {occurrence['missed_count']} instant(s) not run"
              if occurrence.get("missed_count") else "")
    return (f"  - {occurrence['occurrence_id']}: {occurrence['state']}{reason}{late}{missed}"
            + (f": {text[:300]}" if text else ""))


def run_occurrence(host, occurrence: Mapping[str, Any], *, timeout: float = 300.0) -> dict:
    """Run one claimed occurrence as a fresh turn with exactly the schedule's capabilities.

    The occurrence id is the turn's idempotency key, so the turn is never dispatched twice
    and recovery can trace a crashed run to its turn and receipts.
    """
    schedule = occurrence["schedule"]
    zone, now = schedule["timezone"], time.time()
    kind = "manual run" if occurrence["manual"] else "scheduled run"
    late = f", {occurrence['late_seconds']:.0f}s late" if occurrence.get("late_seconds") else ""
    late += _missed_text(occurrence.get("missed_count"))
    # One line: a name can never end the header early and pass for the prompt's words.
    name = " ".join(str(schedule["name"]).split())
    header = (f"[Brainstem Agent {kind} of schedule {schedule['schedule_id']} "
              f"\"{name}\", due {local_iso(occurrence['scheduled_at'], zone)}{late}; "
              f"now {local_iso(now, zone)} ({zone}). Do the task now with your tools; the owner "
              "is away and will read your answer later.]\n")
    result = host.chat(header + schedule["prompt"], capabilities=schedule["capabilities"],
                       workspace=schedule["workspace"], idempotency_key=occurrence["occurrence_id"],
                       timeout=timeout)
    response = (result.response or {}).get("response") or None
    return {"state": result.state, "turn_id": result.turn_id, "result": {
        "started_at": now, "response": response[:16000] if response else None,
        "error": result.error, "session_id": result.session_id, "replayed": result.replayed,
        "receipts": [f"{item['tool']}:{item['state']}"
                     for item in result.evidence.get("receipts", [])][:20]}}


# -- the organ (model tools) -------------------------------------------------------------
_WHEN_PROPERTIES = {
    "in_seconds": {"type": "integer", "minimum": 1, "maximum": 31_622_400,
                   "description": "Run once this many seconds from now (with every_seconds: "
                                  "the first run)."},
    "at": {"type": "string", "maxLength": 64,
           "description": "Run once at this local date-time, ISO 8601, e.g. 2026-09-23T09:00."},
    "every_seconds": {"type": "integer", "minimum": MIN_INTERVAL, "maximum": 31_622_400,
                      "description": "Repeat every this many seconds."},
    "cron": {"type": "string", "maxLength": 100,
             "description": "5-field cron on the local wall clock: minute hour day month "
                            "weekday, e.g. '0 9 * * 1-5'."},
    "timezone": {"type": "string", "maxLength": 64,
                 "description": "IANA timezone; default the owner's."},
    "prompt": {"type": "string", "minLength": 1, "maxLength": 4000,
               "description": "The self-contained instruction to carry out when it fires."},
    "name": {"type": "string", "minLength": 1, "maxLength": 80},
    "capabilities": {"type": "array", "maxItems": 16, "items": {"type": "string",
                                                                  "maxLength": 64},
                     "description": "Capabilities for its runs (at most this conversation's)."},
    "missed_policy": {"type": "string", "enum": list(MISSED_POLICIES),
                      "description": "If runs were missed while the cell was down."},
}
_ID = {"type": "string", "minLength": 1, "maxLength": 80,
       "description": "The schedule's id (sch_...) or its exact name."}
# Required (with a neutral default) because unchanged Grail refuses the empty argument
# string that models stream for a tool that requires nothing.
_LIST_ID = {"type": "string", "minLength": 1, "maxLength": 80, "default": "all",
            "description": "'all' to list this workspace's schedules, or one schedule's id "
                           "(sch_...) or exact name to show it with its recent runs."}
_LIST_ALL = ("all", "*")
# Helpers, scripts and background processes: a schedule holds them only when asked for.
_LONG_TURN = ("agents.delegate", "scripts.run", "processes.run")


class ScheduleOrgan:
    """Model tools to create, list and change durable schedules in the run's workspace."""

    name = "schedule"

    def __init__(self, store, *, notify: Callable[[], str | None] = lambda: None,
                 clock: Callable[[], float] = time.time,
                 prior_tools: Callable[[str], Sequence[str]] = lambda _turn: ()) -> None:
        self.store, self.notify, self.clock = store, notify, clock
        self.prior_tools = prior_tools

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("schedule_create", "Schedule work for later: the prompt runs as its own turn "
                     "in this workspace, once or repeatedly. Give exactly one of in_seconds, at, "
                     "every_seconds or cron.",
                     {"type": "object", "properties": _WHEN_PROPERTIES, "required": ["prompt"]},
                     "schedule.write", "write"),
            ToolSpec("schedule_list", "List this workspace's schedules (schedule_id 'all'), or "
                     "show one schedule with its recent runs and their results.",
                     {"type": "object", "properties": {"schedule_id": _LIST_ID},
                      "required": ["schedule_id"]},
                     "schedule.read", "read"),
            ToolSpec("schedule_update", "Pause, resume, edit, run now or remove a schedule. "
                     "For edit, give the fields to change.",
                     {"type": "object", "properties": {
                         "schedule_id": _ID,
                         "action": {"type": "string",
                                    "enum": ["pause", "resume", "edit", "run_now", "remove"]},
                         **_WHEN_PROPERTIES}, "required": ["schedule_id", "action"]},
                     "schedule.write", "write"),
        ]

    def context(self, context: BindContext) -> str | None:
        zone = local_zone()
        return (f"<schedules>Now: {local_iso(self.clock(), zone)} ({zone}). For anything to do "
                "later or repeatedly, create a schedule (in_seconds for relative times); each "
                "run is a new turn in this workspace with its own bounded capabilities."
                "</schedules>")

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        context.check()
        now = self.clock()
        turn = tuple(context.capabilities)
        try:
            if tool == "schedule_create":
                record = create_schedule(
                    self.store, namespace=context.namespace, workspace=str(context.workspace_root),
                    prompt=arguments.get("prompt"), when=arguments,
                    capabilities=arguments.get("capabilities"), allowed=turn,
                    default=[item for item in turn if not item.startswith("schedule.")
                             and item not in _LONG_TURN],
                    created_by="turn:" + context.turn_id, now=now,
                    timezone=arguments.get("timezone"), name=arguments.get("name"),
                    missed_policy=arguments.get("missed_policy"))
                view = describe(record)
                note = self.notify() or ""
                return ToolResult(
                    f"Created schedule {view['schedule_id']} ({view['when']}); next run "
                    f"{view['next_fire_local']} with capabilities {view['capabilities']}. {note}"
                    .strip(), evidence={"schedule_id": view["schedule_id"], "action": "create"})
            if tool == "schedule_list":
                wanted = arguments.get("schedule_id")
                if wanted and str(wanted).strip().casefold() not in _LIST_ALL:
                    view = describe(find_schedule(self.store, context.namespace, wanted))
                    runs = self.store.list_occurrences(context.namespace,
                                                       schedule_id=view["schedule_id"], limit=5)
                    lines = [f"{view['schedule_id']} '{view['name']}': {view['state']}, "
                             f"{view['when']}, next {view['next_fire_local']}, capabilities "
                             f"{view['capabilities']}, missed policy {view['missed_policy']}.",
                             f"Prompt: {view['prompt']}", "Recent runs:" if runs else "No runs yet."]
                    return ToolResult("\n".join(lines + [_run_view(run) for run in runs]),
                                      evidence={"schedule_id": view["schedule_id"]})
                views = [describe(item) for item in self.store.list_schedules(context.namespace)]
                if not views:
                    return ToolResult("No schedules in this workspace.", evidence={"count": 0})
                return ToolResult("\n".join(
                    f"- {view['schedule_id']} '{view['name']}': {view['state']}, {view['when']}, "
                    f"next {view['next_fire_local']}" for view in views),
                    evidence={"count": len(views)})
            if tool == "schedule_update":
                action = arguments["action"]
                record = change_schedule(self.store, context.namespace, arguments["schedule_id"],
                                         action, arguments, allowed=turn, acting=turn, now=now,
                                         writer="turn:" + context.turn_id,
                                         outside=untrusted(self.prior_tools(context.turn_id)))
                view = describe(record)
                note = self.notify() or ""
                done = {"run_now": "Requested a run now (it starts after the current turn)"
                        }.get(action, f"Schedule is now {view['state']}")
                return ToolResult(f"{done}: {view['schedule_id']} ({view['when']}); next run "
                                  f"{view['next_fire_local']}. {note}".strip(),
                                  evidence={"schedule_id": view["schedule_id"], "action": action})
        except (ScheduleError, StateError) as error:
            raise OrganError(str(error)) from None
        raise OrganError(f"Unknown schedule tool {tool!r}.")


# -- the loop --------------------------------------------------------------------------
class Scheduler:
    """The daemon's single loop over the store's next-fire index.

    ``run_turn(occurrence)`` executes one claimed occurrence and returns its outcome
    (``state``, ``turn_id``, ``result``); turns run one at a time on this thread.
    """

    def __init__(self, store, run_turn: Callable[[dict], dict], *,
                 clock: Callable[[], float] = time.time, grace: float = LATE_GRACE,
                 nap: float = MAX_NAP, idle: Callable[[], None] | None = None,
                 on_error: Callable[[str], None] | None = None) -> None:
        self.store, self.run_turn, self.clock = store, run_turn, clock
        self.grace, self.nap, self.idle, self.on_error = grace, nap, idle, on_error
        self.started_at = clock()
        self.current: dict | None = None
        self.woken = threading.Event()
        self.stopping = threading.Event()
        # While ``paused()`` (the daemon drains for maintenance) nothing new is claimed.
        self.paused: Callable[[], bool] = lambda: False

    def wake(self) -> None:
        self.woken.set()

    def stop(self) -> None:
        self.stopping.set()
        self.woken.set()

    def plan(self, schedule: dict, last: dict | None, now: float) -> dict:
        """Decide one occurrence for a due schedule (runs inside the claim transaction).

        ``missed_count`` is how many scheduled instants the record accounts for that never
        ran: 0 for a run (on time, or the latest of a missed stretch run late: then the
        earlier ones), the whole stretch for a skipped record."""
        zone, spec, first = schedule["timezone"], schedule["spec"], schedule["next_fire_at"]
        decision = {"manual": False, "state": "running", "schedule_state": schedule["state"],
                    "next_fire_at": first, "missed_count": 0}
        busy = None if last is None else (last["claimed_at"], last["finished_at"] or now)
        try:
            if schedule["run_now_at"] is not None and schedule["run_now_at"] <= now:
                requested = schedule["run_now_at"]
                decision.update(scheduled_at=int(requested), manual=True, consume_run_now=True)
                if busy and busy[0] <= requested <= busy[1]:
                    decision.update(state="skipped", reason="overlap", missed_count=1)
                elif now - requested > self.grace:
                    decision["late_seconds"] = round(now - requested, 3)
                return decision
            if busy and busy[0] < first <= busy[1]:
                # Came due while this schedule's previous run was still running: recorded as
                # each instant arrives, or once for a stretch the loop could not see in time.
                upcoming = next_fire(spec, zone, busy[1])
                instant = latest_fire(spec, zone, busy[1]) or first
                decision.update(scheduled_at=instant, state="skipped", reason="overlap",
                                next_fire_at=upcoming,
                                missed_count=max(1, count_fires(spec, zone, first, instant)),
                                missed_from=first if instant > first else None)
            else:
                instant = max(first, latest_fire(spec, zone, now) or first)
                late = now - instant
                upcoming = next_fire(spec, zone, now)
                decision.update(scheduled_at=instant, next_fire_at=upcoming)
                if instant > first or instant < self.started_at or late > self.grace:
                    stretch = max(1, count_fires(spec, zone, first, instant))
                    decision.update(late_seconds=round(late, 3), missed_count=stretch - 1,
                                    missed_from=first if instant > first else None)
                    if schedule["missed_policy"] == "skip":
                        decision.update(state="skipped", reason="missed", missed_count=stretch)
            if decision["next_fire_at"] is None:
                decision["schedule_state"] = "completed"
        except (ValueError, KeyError, TypeError, zoneinfo.ZoneInfoNotFoundError) as error:
            decision.update(scheduled_at=first or int(now), state="skipped",
                            reason="invalid schedule", next_fire_at=None,
                            schedule_state="paused", missed_count=None,
                            result={"error": f"The schedule could not be evaluated: {error}"[:300]})
        return decision

    def _overlap_only(self, schedule: dict, last: dict | None, now: float) -> dict | None:
        """``plan``, but only its overlap skips (while that schedule's run is still going)."""
        decision = self.plan(schedule, last, now)
        return decision if decision.get("reason") == "overlap" else None

    def tick(self, *, schedule_id: str | None = None, limit: int | None = None) -> list[dict]:
        """Claim and run everything due now (one schedule only with ``schedule_id``).

        Returns each claimed occurrence; overlap skips recorded during a run follow it."""
        done: list[dict] = []
        while not self.stopping.is_set() and (limit is None or len(done) < limit):
            occurrence = self.store.claim_due(self.clock(), self.plan, schedule_id=schedule_id)
            if occurrence is None:
                break
            skipped: list[dict] = []
            if occurrence["state"] == "running" and not occurrence["duplicate"]:
                occurrence = self._execute(occurrence, skipped)
            done.append(occurrence)
            done.extend(skipped)
        return done

    def _execute(self, occurrence: dict, skipped: list[dict]) -> dict:
        """Run the turn on a helper thread; meanwhile record this schedule's instants that
        come due (and run-now requests) as overlap skips, at the time they arrive."""
        outcome: dict = {}
        finished = threading.Event()

        def turn() -> None:
            try:
                outcome.update(self.run_turn(occurrence))
            except Exception as error:  # the run never started a turn
                outcome.update(state="failed",
                               result={"error": f"The run could not start: {error}"[:500]})
            finally:
                finished.set()
                self.woken.set()

        self.current = occurrence
        runner = threading.Thread(target=turn, daemon=True, name="brainstem-agent-scheduled-turn")
        try:
            runner.start()
            while True:
                self.woken.clear()
                if finished.is_set():
                    break
                if not self.stopping.is_set():
                    skipped.extend(self._skip_overlaps(occurrence["schedule_id"]))
                self.woken.wait(self._overlap_delay(occurrence["schedule_id"],
                                                    occurrence["claimed_at"]))
            runner.join()
        finally:
            self.current = None
        record = self._finish(occurrence["occurrence_id"], outcome)
        record["schedule"] = occurrence["schedule"]
        return record

    def _finish(self, occurrence_id: str, outcome: Mapping[str, Any]) -> dict:
        """Record the run's outcome; a briefly failing store is retried, since a run left
        ``running`` would make every later instant of its schedule look like an overlap."""
        delay = 0.1
        for attempt in range(6):
            try:
                return self.store.finish_occurrence(
                    occurrence_id, outcome["state"], turn_id=outcome.get("turn_id"),
                    result=outcome.get("result"), now=self.clock())
            except ConflictError:
                raise
            except StateError:
                if attempt == 5:
                    raise
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def _skip_overlaps(self, schedule_id: str) -> list[dict]:
        recorded = []
        try:
            while not self.stopping.is_set():
                record = self.store.claim_due(self.clock(), self._overlap_only,
                                              schedule_id=schedule_id)
                if record is None:
                    break
                recorded.append(record)
        except Exception as error:  # keep waiting for the run; the loop catches up after it
            if self.on_error is not None:
                self.on_error(f"scheduler: {type(error).__name__}: {error}"[:300])
        return recorded

    def _overlap_delay(self, schedule_id: str, since: float) -> float:
        """Seconds until the running schedule's next instant or run-now request after
        ``since`` (the run's claim); what was due before it waits for the run to end."""
        try:
            schedule = self.store.get_schedule(None, schedule_id)
        except StateError:
            return self.nap
        due = [value for value in (schedule["next_fire_at"] if schedule["state"] == "active"
                                   else None, schedule["run_now_at"])
               if value is not None and value > since]
        return self.nap if not due else min(self.nap, max(0.05, min(due) - self.clock()))

    def run(self) -> None:
        """Loop until ``stop()``: tick, keep warm, then sleep until the next pending instant."""
        while not self.stopping.is_set():
            self.woken.clear()
            try:
                if not self.paused():
                    self.tick()
                if self.idle is not None and not self.stopping.is_set():
                    self.idle()
                wake_at = self.store.next_wake()
            except Exception as error:  # keep the cell alive; surface it in status
                if self.on_error is not None:
                    self.on_error(f"scheduler: {type(error).__name__}: {error}"[:300])
                wake_at = None
            delay = self.nap if wake_at is None else min(self.nap, max(0.0, wake_at - self.clock()))
            self.woken.wait(delay)
