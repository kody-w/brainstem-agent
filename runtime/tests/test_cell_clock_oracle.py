"""B5 unit specs: the next-fire computation against a brute-force clock oracle.

The oracle knows nothing about folds, day scans or PEP 495. It walks UTC minute by
minute, asks ``zoneinfo`` only for the local reading of each instant (UTC to local
is never ambiguous) and applies the documented rule directly:

- a wall minute fires at the first instant the clock reads it, so a time that
  fall-back repeats fires only in its first pass;
- a wall minute that spring-forward skips fires as if the old offset still held,
  i.e. shifted forward by the length of the gap (New York 02:30 becomes 03:30 EDT,
  Lord Howe 02:15 becomes 02:45 +11);
- instants that coincide fire once.

``schedules.next_fire`` and ``schedules.latest_fire`` must agree with it for cron and
one-shot specs around every UTC-offset transition of 2025-2027 in fourteen zones
(northern and southern hemisphere, 30-minute DST on Lord Howe, +12:45 Chatham,
midnight transitions in Santiago, Havana and Cairo, Dublin's negative DST,
Casablanca's Ramadan suspension, St John's -03:30), plus two zones without DST.
Relative one-shots must keep their exact instant. Scale with
``BRAINSTEM_AGENT_ORACLE_SCALE`` (default 1) for every cron on every window.
"""

import datetime as dt
import math
import os
import random
import unittest
import zoneinfo

from acceptance_support import criteria, record_metric
from brainstem_agent import schedules
from brainstem_agent.schedules import latest_fire, next_fire

SCALE = max(1, int(os.environ.get("BRAINSTEM_AGENT_ORACLE_SCALE", "1")))
MINUTE = dt.timedelta(minutes=1)
YEARS = (2025, 2027)
DST_ZONES = ("America/New_York", "Europe/London", "Europe/Dublin", "Europe/Paris",
             "Australia/Sydney", "Australia/Lord_Howe", "Pacific/Chatham", "America/Santiago",
             "America/Havana", "Africa/Casablanca", "America/St_Johns", "Pacific/Auckland",
             "Africa/Cairo", "Asia/Gaza")
PLAIN_ZONES = ("Asia/Kolkata", "UTC")
HALF_WINDOW = 20 * 3600
MARGIN = 3 * 3600
# Sparse expressions run on every window; they aim at the hours transitions touch.
SPARSE = ("30 2 * * *", "0 2 * * *", "59 1 * * *", "30 1 * * *", "0 * * * *",
          "*/15 0-4,22-23 * * *",
          "*/30 0-3 * * *", "45 23 * * *", "0 0 * * *", "15 0 * * *", "5-10 2 * * *",
          "0,30 1-3 * * *", "2 2 * * *", "50 2 * * *", "59 * * * *", "0 9 * * 1-5",
          "30 2 1-7 * 0", "0 3 * mar-apr,sep-nov *", "20 23,0 * * *", "*/20 22-23 * * sat,sun")
# Dense expressions (hundreds of fires a day) run on one window per zone.
DENSE = ("* 2 * * *", "*/7 0-4,22-23 * * *", "* 0 * * *", "*/3 23 * * *", "*/15 * * * *")
_NAMES = {**{name: number for number, name in
             enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)},
          **{name: number for number, name in enumerate("sun mon tue wed thu fri sat".split())}}


def utc(*parts):
    return int(dt.datetime(*parts, tzinfo=dt.timezone.utc).timestamp())


def reading(instant, zone):
    """The zone's wall clock at a UTC instant (never ambiguous)."""
    return dt.datetime.fromtimestamp(instant, zoneinfo.ZoneInfo(zone)).replace(tzinfo=None)


def transitions(zone, first_year, last_year):
    """Every instant in the years at which the zone's UTC offset changes."""
    tz = zoneinfo.ZoneInfo(zone)
    offset = lambda instant: dt.datetime.fromtimestamp(instant, tz).utcoffset()
    found, instant = [], utc(first_year, 1, 1)
    end, previous = utc(last_year + 1, 1, 1), offset(utc(first_year, 1, 1))
    while instant < end:
        step = instant + 3600
        if offset(step) != previous:
            low, high = instant, step
            while high - low > 1:
                middle = (low + high) // 2
                low, high = (middle, high) if offset(middle) == previous else (low, middle)
            found.append(high)
            previous = offset(high)
        instant = step
    return found


class Oracle:
    """One minute-by-minute walk of a zone's clock over [start, end] (UTC, minute-aligned)."""

    def __init__(self, zone, start, end, *, gap="shift"):
        self.zone, self.start, self.end = zone, start, end
        previous = reading(start - 60, zone)
        high = max(reading(instant, zone) for instant in range(start - 26 * 3600, start, 60))
        self.first, self.second = {}, {}
        for instant in range(start, end + 1, 60):
            wall = reading(instant, zone)
            if wall - previous > MINUTE:  # spring-forward: these wall minutes never happen
                skipped = previous + MINUTE
                while skipped < wall:
                    if skipped > high:
                        shift = int((skipped - (previous + MINUTE)).total_seconds())
                        # "end": the other plausible rule (fire when the clock passes it)
                        self.first.setdefault(skipped, instant + (shift if gap == "shift" else 0))
                    skipped += MINUTE
            if wall > high:
                self.first.setdefault(wall, instant)
                high = wall
            else:  # fall-back: the clock reads this wall minute again
                self.second.setdefault(wall, instant)
            previous = wall

    def cron(self, expression, *, repeated="first"):
        match = matcher(expression)
        passes = self.first if repeated == "first" else {**self.first, **self.second}
        return sorted({instant for wall, instant in passes.items() if match(wall)})


def _values(text, low, high):
    found = set()
    for part in text.lower().split(","):
        base, slash, step = part.partition("/")
        if base == "*":
            first, last = low, high
        elif "-" in base:
            first, last = (_NAMES.get(item) if item in _NAMES else int(item)
                           for item in base.split("-"))
        else:
            first = _NAMES.get(base) if base in _NAMES else int(base)
            last = high if slash else first
        found.update(range(first, last + 1, int(step) if slash else 1))
    return found


def matcher(expression):
    """An independent 5-field matcher with the Vixie day rule (OR unless a day field is '*...')."""
    minute, hour, day, month, weekday = expression.split()
    minutes, hours = _values(minute, 0, 59), _values(hour, 0, 23)
    days, months = _values(day, 1, 31), _values(month, 1, 12)
    weekdays = {value % 7 for value in _values(weekday, 0, 7)}
    either = not (day.startswith("*") or weekday.startswith("*"))

    def match(wall):
        if wall.minute not in minutes or wall.hour not in hours or wall.month not in months:
            return False
        on_day, on_weekday = wall.day in days, wall.isoweekday() % 7 in weekdays
        return (on_day or on_weekday) if either else (on_day and on_weekday)
    return match


def forward(spec, zone, low, high):
    """Instants in (low, high] by chaining next_fire."""
    found, instant = [], low
    while True:
        instant = next_fire(spec, zone, instant)
        if instant is None or instant > high:
            return found
        found.append(instant)


def backward(spec, zone, low, high):
    """Instants in (low, high] by chaining latest_fire from high, returned in order."""
    found, instant = [], high
    while True:
        instant = latest_fire(spec, zone, instant)
        if instant is None or instant <= low:
            return found[::-1]
        found.append(instant)
        instant -= 1


def windows():
    """(zone, transition or None, start, end) around every 2025-2027 transition."""
    for zone in DST_ZONES:
        points = transitions(zone, *YEARS)
        assert points and all(point % 60 == 0 for point in points), (zone, points)
        for point in points:
            yield zone, point, point - HALF_WINDOW, point + HALF_WINDOW
    for zone in PLAIN_ZONES:
        assert not transitions(zone, *YEARS), zone
        for point in (utc(2025, 3, 9, 7), utc(2026, 11, 1, 6), utc(2027, 12, 31, 23)):
            yield zone, None, point - HALF_WINDOW, point + HALF_WINDOW


class ClockOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.windows = [(zone, point, start, end, Oracle(zone, start, end))
                       for zone, point, start, end in windows()]

    def test_the_oracle_encodes_the_documented_examples(self):
        ny = Oracle("America/New_York", utc(2026, 3, 7, 12), utc(2026, 3, 9, 12))
        self.assertEqual(ny.cron("30 2 * * *"), [utc(2026, 3, 8, 7, 30), utc(2026, 3, 9, 6, 30)])
        fall = Oracle("America/New_York", utc(2026, 10, 31, 12), utc(2026, 11, 2, 12))
        self.assertEqual(fall.cron("30 1 * * *"), [utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 30)])
        self.assertEqual(fall.second[dt.datetime(2026, 11, 1, 1, 30)], utc(2026, 11, 1, 6, 30))
        howe = Oracle("Australia/Lord_Howe", utc(2026, 10, 3), utc(2026, 10, 5))
        # 02:15 does not happen on 4 Oct 2026 (02:00 +10:30 -> 02:30 +11): it fires at 02:45 +11.
        self.assertIn(utc(2026, 10, 3, 15, 45), howe.cron("15 2 * * *"))

    @criteria("B5")
    def test_the_comparison_tells_the_documented_rule_from_the_other_plausible_rules(self):
        spring = (utc(2026, 3, 7, 12), utc(2026, 3, 9, 12))
        fall = (utc(2026, 10, 31, 12), utc(2026, 11, 2, 12))
        for zone, window, expression, other in (
                ("America/New_York", spring, "30 2 * * *", {"gap": "end"}),
                ("Australia/Lord_Howe", (utc(2026, 10, 3), utc(2026, 10, 5)), "15 2 * * *",
                 {"gap": "end"}),
                ("America/New_York", fall, "30 1 * * *", {"repeated": "second"}),
                ("Africa/Cairo", (utc(2026, 10, 28), utc(2026, 10, 31)), "30 23 * * *",
                 {"repeated": "second"})):
            got = forward({"kind": "cron", "cron": expression}, zone, *window)
            expected = Oracle(zone, *window).cron(expression)
            self.assertEqual(got, [instant for instant in expected if instant > window[0]])
            gap = {"gap": other["gap"]} if "gap" in other else {}
            rival = Oracle(zone, *window, **gap).cron(
                expression, **({"repeated": other["repeated"]} if "repeated" in other else {}))
            self.assertNotEqual(got, [instant for instant in rival if instant > window[0]],
                                (zone, expression, other))

    @criteria("B5")
    def test_cron_next_and_latest_fire_agree_with_the_oracle_around_every_transition(self):
        rng = random.Random(20260923)
        cases = compared = 0
        dense_done = set()
        for zone, point, start, end, oracle in self.windows:
            expressions = list(SPARSE)
            if SCALE > 1 or zone not in dense_done:
                expressions += DENSE
                dense_done.add(zone)
            low, high = start + MARGIN, end - MARGIN
            for expression in expressions:
                spec = {"kind": "cron", "cron": expression}
                expected = [instant for instant in oracle.cron(expression) if low < instant <= high]
                label = (zone, point, expression)
                self.assertEqual(forward(spec, zone, low, high), expected, label)
                self.assertEqual(backward(spec, zone, low, high), expected, label)
                for _ in range(3 * SCALE):
                    probe = rng.randrange(low, high)
                    after = [instant for instant in expected if instant > probe]
                    upto = [instant for instant in expected if instant <= probe]
                    got = next_fire(spec, zone, probe)
                    if after:
                        self.assertEqual(got, after[0], (*label, "next", probe))
                    else:
                        self.assertTrue(got is None or got > high, (*label, "next", probe))
                    got = latest_fire(spec, zone, probe)
                    if upto:
                        self.assertEqual(got, upto[-1], (*label, "latest", probe))
                    else:
                        self.assertTrue(got is None or got <= low, (*label, "latest", probe))
                cases += 1
                compared += len(expected)
        record_metric("b5_clock_oracle_cron_cases", cases)
        record_metric("b5_clock_oracle_instants", compared)
        record_metric("b5_clock_oracle_windows", len(self.windows))
        self.assertGreater(compared, 15_000)

    @criteria("B5")
    def test_one_shot_wall_times_agree_with_the_oracle_in_gaps_and_repeats(self):
        checked = 0
        for zone, point, start, end, oracle in self.windows:
            if point is None:
                continue
            for wall, instant in oracle.first.items():
                if not start + MARGIN < instant <= end - MARGIN or wall.minute % 5:
                    continue
                spec = {"kind": "once", "at": wall.isoformat(), "fold": 0}
                self.assertEqual(next_fire(spec, zone, 0), instant, (zone, wall))
                self.assertEqual(latest_fire(spec, zone, instant), instant, (zone, wall))
                self.assertIsNone(next_fire(spec, zone, instant), (zone, wall))
                parsed = schedules.parse_when({"at": wall.isoformat()}, zone, start)
                self.assertEqual(next_fire(parsed, zone, start), instant, (zone, wall))
                checked += 1
            for wall, instant in oracle.second.items():  # the repeated pass of a fall-back
                spec = {"kind": "once", "at": wall.isoformat(), "fold": 1}
                self.assertEqual(next_fire(spec, zone, 0), instant, (zone, wall, "fold 1"))
                checked += 1
        record_metric("b5_clock_oracle_one_shots", checked)
        self.assertGreater(checked, 10_000)

    @criteria("B5")
    def test_relative_one_shots_keep_their_exact_instant_through_every_transition(self):
        rng = random.Random(495)
        for zone, point, start, end, _oracle in self.windows:
            for _ in range(40 * SCALE):
                now = rng.randrange(start, end) + rng.random()
                delay = rng.choice((1, 59, 60, 120, 1800, 3600, 5400, rng.randrange(1, 86400)))
                spec = schedules.parse_when({"in_seconds": delay}, zone, now)
                self.assertEqual(next_fire(spec, zone, now), math.ceil(now) + delay,
                                 (zone, now, delay, spec))

    @criteria("B5")
    def test_intervals_are_elapsed_seconds_whatever_the_zone(self):
        rng = random.Random(3600)
        for zone, point, start, end, _oracle in self.windows:
            every = rng.choice((60, 900, 3600, 5400, 86400))
            spec = {"kind": "interval", "every": every, "anchor": start + rng.randrange(0, 3600)}
            probe = rng.randrange(start, end)
            steps = (probe - spec["anchor"]) // every
            self.assertEqual(latest_fire(spec, zone, probe),
                             spec["anchor"] + steps * every if probe >= spec["anchor"] else None)
            self.assertEqual(next_fire(spec, zone, probe),
                             spec["anchor"] + (max(steps, -1) + 1) * every)


if __name__ == "__main__":
    unittest.main()
