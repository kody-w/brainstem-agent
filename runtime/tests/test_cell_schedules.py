"""B4-B9 unit specs: schedule time math (DST, timezones, clock jumps), the next-fire
loop on a virtual clock (missed runs, overlap, crash recovery) and the schedule organ."""

import datetime as dt
import threading
import time
import unittest
import zoneinfo

from acceptance_support import criteria, private_dir
from brainstem_agent import schedules
from brainstem_agent.organs.base import InvocationContext, OrganError
from brainstem_agent.schedules import (Cron, ScheduleError, ScheduleOrgan, Scheduler,
                                       change_schedule, create_schedule, latest_fire, next_fire)
from brainstem_agent.state import Store

NY = "America/New_York"
ALL = ("files.read", "files.write", "memory.read", "memory.write", "shell.run", "schedule.read",
       "schedule.write")
CORE = ALL[:5]


def utc(text):
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp())


def fires(spec, zone, after, count):
    found = []
    for _ in range(count):
        after = next_fire(spec, zone, after)
        found.append(after)
    return found


class Clock:
    def __init__(self, now):
        self.now = float(now)

    def __call__(self):
        return self.now


class CronTests(unittest.TestCase):
    @criteria("B5")
    def test_fields_names_ranges_steps_and_refusals(self):
        cron = Cron("*/15 9-17 * JAN-mar mon-fri")
        self.assertEqual(cron.minutes, [0, 15, 30, 45])
        self.assertEqual(cron.hours, list(range(9, 18)))
        self.assertEqual(cron.months, frozenset({1, 2, 3}))
        self.assertEqual(cron.weekdays, {1, 2, 3, 4, 5})
        self.assertEqual(Cron("0 0 * * 7").weekdays, {0})
        for bad in ("* * * *", "61 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *",
                    "5-1 * * * *", "* * 0 * *", 42):
            with self.subTest(bad=bad), self.assertRaises(ScheduleError):
                Cron(bad)

    @criteria("B5")
    def test_day_of_month_and_weekday_follow_vixie_or_semantics(self):
        after = utc("2026-09-01T00:00")
        # the 15th OR any Monday (both restricted)
        either = fires({"kind": "cron", "cron": "0 12 15 * 1"}, "UTC", after, 3)
        self.assertEqual(either, [utc("2026-09-07T12:00"), utc("2026-09-14T12:00"),
                                  utc("2026-09-15T12:00")])
        # '*' in one field: only the other restricts
        self.assertEqual(fires({"kind": "cron", "cron": "0 12 * * 1"}, "UTC", after, 2),
                         [utc("2026-09-07T12:00"), utc("2026-09-14T12:00")])
        self.assertIsNone(next_fire({"kind": "cron", "cron": "0 0 31 2 *"}, "UTC", after))


class DaylightSavingTests(unittest.TestCase):
    """America/New_York: 2026-03-08 02:00 EST -> 03:00 EDT; 2026-11-01 02:00 EDT -> 01:00 EST."""

    @criteria("B5")
    def test_spring_forward_skipped_time_fires_once_at_the_shifted_instant(self):
        daily = {"kind": "cron", "cron": "30 2 * * *"}
        self.assertEqual(fires(daily, NY, utc("2026-03-07T12:00"), 2),
                         [utc("2026-03-08T07:30"), utc("2026-03-09T06:30")])  # 03:30 EDT, 02:30 EDT
        half_hourly = {"kind": "cron", "cron": "*/30 1-3 * * *"}
        day = fires(half_hourly, NY, utc("2026-03-08T05:59"), 5)
        # 01:00, 01:30 EST; 02:00->03:00 EDT and 02:30->03:30 EDT coincide with 03:00/03:30.
        self.assertEqual(day, [utc("2026-03-08T06:00"), utc("2026-03-08T06:30"),
                               utc("2026-03-08T07:00"), utc("2026-03-08T07:30"),
                               utc("2026-03-09T05:00")])
        self.assertEqual(latest_fire(daily, NY, utc("2026-03-08T12:00")), utc("2026-03-08T07:30"))
        once = {"kind": "once", "at": "2026-03-08T02:30:00", "fold": 0}
        self.assertEqual(next_fire(once, NY, 0), utc("2026-03-08T07:30"))

    @criteria("B5")
    def test_fall_back_repeated_time_fires_only_in_its_first_pass(self):
        daily = {"kind": "cron", "cron": "30 1 * * *"}
        self.assertEqual(fires(daily, NY, utc("2026-10-31T12:00"), 2),
                         [utc("2026-11-01T05:30"), utc("2026-11-02T06:30")])
        hourly = {"kind": "cron", "cron": "0 * * * *"}
        self.assertEqual(fires(hourly, NY, utc("2026-11-01T03:30"), 4),
                         [utc("2026-11-01T04:00"), utc("2026-11-01T05:00"),
                          utc("2026-11-01T07:00"), utc("2026-11-01T08:00")])
        self.assertEqual(latest_fire(hourly, NY, utc("2026-11-01T06:59")), utc("2026-11-01T05:00"))
        first = schedules.parse_when({"at": "2026-11-01T01:30"}, NY, 0)
        second = schedules.parse_when({"at": "2026-11-01T01:30:00-05:00"}, NY, 0)
        self.assertEqual((first["fold"], second["fold"]), (0, 1))
        self.assertEqual(next_fire(first, NY, 0), utc("2026-11-01T05:30"))
        self.assertEqual(next_fire(second, NY, 0), utc("2026-11-01T06:30"))

    @criteria("B5")
    def test_relative_one_shot_in_the_repeated_hour_keeps_its_real_instant(self):
        now = utc("2026-11-01T06:10")  # 01:10 EST, the second pass
        spec = schedules.parse_when({"in_seconds": 600}, NY, now)
        self.assertEqual(spec["fold"], 1)
        self.assertEqual(next_fire(spec, NY, now), now + 600)

    @criteria("B5")
    def test_intervals_count_elapsed_seconds_across_both_transitions(self):
        for anchor in (utc("2026-03-08T05:00"), utc("2026-11-01T04:00")):
            spec = {"kind": "interval", "every": 3600, "anchor": anchor}
            self.assertEqual(fires(spec, NY, anchor - 1, 4),
                             [anchor + 3600 * step for step in range(4)])


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(private_dir(self) / "agent.sqlite3")
        self.addCleanup(self.store.close)
        self.clock = Clock(utc("2026-09-23T12:00"))
        self.runs = []

    def create(self, when, *, zone=NY, capabilities=None, policy=None, name=None):
        return create_schedule(self.store, namespace="ns", workspace="/w", prompt="do it",
                               when=when, capabilities=capabilities, allowed=ALL, default=CORE,
                               created_by="owner", now=self.clock(), timezone=zone, name=name,
                               missed_policy=policy)

    def scheduler(self, turn=None):
        def run(occurrence):
            self.runs.append(occurrence["occurrence_id"])
            return turn(occurrence) if turn else {"state": "succeeded", "turn_id": "turn_x",
                                                  "result": {"response": "done"}}
        return Scheduler(self.store, run, clock=self.clock)

    def occurrences(self, schedule_id):
        return {item["scheduled_at"]: item for item in
                self.store.list_occurrences("ns", schedule_id=schedule_id)}


class TimezoneEditTests(StoreCase):
    @criteria("B4", "B5")
    def test_timezone_edits_reinterpret_wall_clock_schedules_only(self):
        self.clock.now = utc("2026-09-23T00:00")
        cron = self.create({"cron": "0 9 * * *"})
        once = self.create({"at": "2026-09-23T09:00"})
        interval = self.create({"every_seconds": 3600})
        self.assertEqual(cron["timezone"], NY)
        self.assertEqual((cron["next_fire_at"], once["next_fire_at"]),
                         (utc("2026-09-23T13:00"), utc("2026-09-23T13:00")))
        edit = lambda item, zone: change_schedule(self.store, "ns", item["schedule_id"], "edit",
                                                  {"timezone": zone}, allowed=ALL,
                                                  now=self.clock())
        self.assertEqual(edit(cron, "Europe/London")["next_fire_at"], utc("2026-09-23T08:00"))
        self.assertEqual(edit(once, "UTC")["next_fire_at"], utc("2026-09-23T09:00"))
        self.assertEqual(edit(interval, "Asia/Tokyo")["next_fire_at"], interval["next_fire_at"])
        with self.assertRaises(ScheduleError):
            edit(cron, "Mars/Olympus")
        with self.assertRaises(ScheduleError):
            self.create({"at": "2026-09-22T09:00"})  # already passed


class LoopTests(StoreCase):
    @criteria("B5")
    def test_one_stable_identity_per_instant_and_restarts_never_duplicate(self):
        item = self.create({"every_seconds": 60})
        first = item["next_fire_at"]
        self.clock.now = first
        [fired] = self.scheduler().tick()
        self.assertEqual(fired["occurrence_id"], f"occ_{item['schedule_id']}_{first}")
        self.assertEqual((fired["state"], fired["late_seconds"]), ("succeeded", None))
        self.assertEqual(self.store.get_schedule("ns", item["schedule_id"])["next_fire_at"],
                         first + 60)
        again = self.scheduler()  # a restart at the same instant
        self.assertEqual(again.tick(), [])
        # Even a schedule forced back onto a fired instant is never run twice.
        self.store.update_schedule("ns", item["schedule_id"], next_fire_at=first)
        [duplicate] = again.tick()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.runs, [fired["occurrence_id"]])

    @criteria("B5", "B6")
    def test_clock_jumps_forward_run_the_latest_instant_once_and_backward_never_refire(self):
        item = self.create({"every_seconds": 60})
        start = item["next_fire_at"]
        loop = self.scheduler()
        self.clock.now = start
        loop.tick()
        self.clock.now = start + 3600 + 5  # the wall clock jumps an hour forward
        [late] = loop.tick()
        self.assertEqual((late["scheduled_at"], late["state"]), (start + 3600, "succeeded"))
        self.assertEqual((late["late_seconds"], late["missed_from"]), (5.0, start + 60))
        self.clock.now = start + 30  # ... and back again
        self.assertEqual(loop.tick(), [])
        change_schedule(self.store, "ns", item["schedule_id"], "edit", {"name": "renamed"},
                        allowed=ALL, now=self.clock())
        self.clock.now = start + 3600 + 1
        self.store.update_schedule("ns", item["schedule_id"], next_fire_at=start + 3600)
        [duplicate] = loop.tick()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.runs), 2)

    @criteria("B6")
    def test_missed_runs_while_down_follow_the_recorded_policy(self):
        latest = self.create({"every_seconds": 60})
        skip = self.create({"every_seconds": 60}, policy="skip")
        once = self.create({"in_seconds": 30})
        down_from = latest["next_fire_at"]
        self.clock.now = down_from + 5 * 60 + 10  # restart after five missed instants
        results = {item["schedule_id"]: item for item in self.scheduler().tick()}
        ran = results[latest["schedule_id"]]
        self.assertEqual((ran["state"], ran["scheduled_at"]), ("succeeded", down_from + 300))
        self.assertEqual((ran["late_seconds"], ran["missed_from"]), (10.0, down_from))
        skipped = results[skip["schedule_id"]]
        self.assertEqual((skipped["state"], skipped["reason"]), ("skipped", "missed"))
        self.assertEqual(results[once["schedule_id"]]["state"], "succeeded")
        self.assertGreater(results[once["schedule_id"]]["late_seconds"], 60)
        self.assertEqual(self.store.get_schedule("ns", once["schedule_id"])["state"], "completed")
        self.assertEqual(len(self.runs), 2)
        for item in (latest, skip):
            self.assertGreater(self.store.get_schedule("ns", item["schedule_id"])["next_fire_at"],
                               self.clock.now)

    @criteria("B6")
    def test_an_instant_that_passed_before_the_daemon_started_is_missed_even_if_recent(self):
        item = self.create({"in_seconds": 10}, policy="skip")
        self.clock.now = item["next_fire_at"] + 2
        [result] = self.scheduler().tick()
        self.assertEqual((result["state"], result["reason"]), ("skipped", "missed"))

    @criteria("B7")
    def test_instants_due_while_the_previous_run_is_still_running_are_skipped(self):
        item = self.create({"every_seconds": 60})
        start = item["next_fire_at"]

        def long_turn(occurrence):
            if len(self.runs) == 1:
                change_schedule(self.store, "ns", item["schedule_id"], "run_now", {},
                                allowed=ALL, now=self.clock() + 40)
                self.clock.now += 150  # this run outlasts two more fire times
            return {"state": "succeeded", "result": {"response": "ok"}}

        self.clock.now = start
        loop = self.scheduler(long_turn)
        records = loop.tick()
        self.assertEqual([(r["state"], r["reason"], r["manual"]) for r in records],
                         [("succeeded", None, False), ("skipped", "overlap", True),
                          ("skipped", "overlap", False)])
        self.assertEqual(records[2]["scheduled_at"], start + 120)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.store.get_schedule("ns", item["schedule_id"])["next_fire_at"],
                         start + 180)
        self.clock.now = start + 180
        [normal] = loop.tick()
        self.assertEqual((normal["state"], normal["late_seconds"]), ("succeeded", None))

    @criteria("B4")
    def test_pause_resume_run_now_and_remove(self):
        item = self.create({"every_seconds": 600})
        sid = item["schedule_id"]
        change = lambda action: change_schedule(self.store, "ns", sid, action, {}, allowed=ALL,
                                                now=self.clock())
        self.assertEqual(change("pause")["state"], "paused")
        self.clock.now = item["next_fire_at"] + 1
        self.assertEqual(self.scheduler().tick(), [])
        self.assertEqual(change("run_now")["run_now_at"], self.clock.now)
        [manual] = self.scheduler().tick()
        self.assertEqual((manual["manual"], manual["state"]), (True, "succeeded"))
        self.assertTrue(manual["occurrence_id"].endswith("_m"))
        resumed = change("resume")
        self.assertEqual(resumed["state"], "active")
        self.assertGreater(resumed["next_fire_at"], self.clock.now)
        self.assertEqual(change("remove")["state"], "removed")
        self.assertEqual(self.store.list_schedules("ns"), [])
        with self.assertRaises(ScheduleError):
            change("resume")

    @criteria("B8")
    def test_recovery_marks_a_crashed_run_uncertain_after_a_tool_else_failed(self):
        with_tool, without = self.create({"in_seconds": 5}), self.create({"in_seconds": 5})
        self.clock.now = with_tool["next_fire_at"]
        running = lambda schedule, last, now: {"scheduled_at": schedule["next_fire_at"],
                                               "manual": False, "state": "running",
                                               "next_fire_at": None,
                                               "schedule_state": "completed"}
        claimed = [self.store.claim_due(self.clock(), running) for _ in range(2)]
        turn = self.store.reserve_chat("ns", "run", idempotency_key=claimed[0]["occurrence_id"])
        self.store.mark_chat_running("ns", turn.turn_id)
        self.store.begin_receipt("ns", turn.turn_id, "call_1", "run_command", "shell.run", {})
        self.store.recover_interrupted()
        states = {item["schedule_id"]: item for item in self.store.list_occurrences("ns")}
        self.assertEqual(states[claimed[0]["schedule_id"]]["state"], "uncertain")
        self.assertEqual(states[claimed[0]["schedule_id"]]["turn_id"], turn.turn_id)
        self.assertEqual(states[claimed[1]["schedule_id"]]["state"], "failed")
        self.assertEqual(self.scheduler().tick(), [])  # never run again
        self.assertIn(without["schedule_id"], states)

    @criteria("B5")
    def test_an_unevaluable_schedule_is_paused_with_an_error_not_hot_looped(self):
        broken = self.store.create_schedule(
            "ns", workspace="/w", created_by="owner", spec={"kind": "cron", "cron": "0 9 * * *"},
            timezone="Nowhere/Zone", name="broken", prompt="x", capabilities=list(CORE),
            missed_policy="run-latest-once-late", next_fire_at=int(self.clock()) - 1)
        [record] = self.scheduler().tick()
        self.assertEqual((record["state"], record["reason"]), ("skipped", "invalid schedule"))
        self.assertEqual(self.store.get_schedule("ns", broken["schedule_id"])["state"], "paused")
        self.assertEqual(self.runs, [])

    @criteria("B5")
    def test_the_loop_sleeps_until_the_next_fire_and_wakes_on_change(self):
        self.clock.now = time.time()  # this spec runs the real loop on the real clock
        item = self.create({"in_seconds": 3600})
        loop = Scheduler(self.store, lambda occurrence: {"state": "succeeded"}, nap=30)
        self.assertEqual(self.store.next_wake(), item["next_fire_at"])
        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
        self.addCleanup(loop.stop)
        self.clock.now = time.time()
        soon = self.create({"in_seconds": 1})
        loop.wake()
        deadline = time.monotonic() + 10
        while not self.store.list_occurrences("ns", schedule_id=soon["schedule_id"]):
            self.assertLess(time.monotonic(), deadline, "the loop never fired")
            time.sleep(0.02)
        [fired] = self.store.list_occurrences("ns", schedule_id=soon["schedule_id"])
        self.assertLess(fired["claimed_at"] - fired["scheduled_at"], 0.5)
        loop.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())


class OrganTests(StoreCase):
    def context(self, capabilities, turn="turn_1"):
        return InvocationContext(owner="local", workspace="/w", namespace="ns", session_id="s",
                                 turn_id=turn, call_id="c", workspace_root=private_dir(self),
                                 capabilities=tuple(capabilities), deadline=10**12,
                                 cancelled=threading.Event())

    def setUp(self):
        super().setUp()
        self.woken = []
        self.organ = ScheduleOrgan(self.store, notify=lambda: self.woken.append(1) or None,
                                   clock=self.clock)

    @criteria("B3", "B9")
    def test_chat_created_schedules_are_bounded_by_the_creating_turn(self):
        turn = ("files.read", "files.write", "schedule.write")
        result = self.organ.invoke(self.context(turn), "schedule_create",
                                   {"prompt": "Write the time into notes/time.txt",
                                    "in_seconds": 120})
        [record] = self.store.list_schedules("ns")
        self.assertIn(record["schedule_id"], result.content)
        self.assertEqual(record["capabilities"], ["files.read", "files.write"])
        self.assertEqual(record["timezone"], schedules.local_zone())
        self.assertEqual(record["next_fire_at"], int(self.clock()) + 120)
        self.assertEqual(record["created_by"], "turn:turn_1")
        self.assertEqual(self.woken, [1])
        for requested in (["shell.run"], ["files.read", "memory.write"], []):
            with self.subTest(requested=requested), self.assertRaises(OrganError):
                self.organ.invoke(self.context(turn), "schedule_create",
                                  {"prompt": "x", "in_seconds": 5, "capabilities": requested})
        owner = self.create({"every_seconds": 600}, capabilities=["shell.run"])
        for action in ("pause", "run_now", "remove", "edit"):
            with self.subTest(action=action), self.assertRaises(OrganError):
                self.organ.invoke(self.context(turn), "schedule_update",
                                  {"schedule_id": owner["schedule_id"], "action": action,
                                   "prompt": "escalate"})
        with self.assertRaises(OrganError):
            self.organ.invoke(self.context(turn), "schedule_update", {
                "schedule_id": record["schedule_id"], "action": "edit",
                "capabilities": ["files.read", "shell.run"]})

    @criteria("B4")
    def test_chat_tools_list_show_edit_pause_resume_run_now_and_remove(self):
        turn = ALL
        created = self.organ.invoke(self.context(turn), "schedule_create", {
            "prompt": "Summarise notes", "cron": "0 9 * * 1-5", "name": "weekday summary",
            "timezone": "Europe/Paris", "missed_policy": "skip"})
        [record] = self.store.list_schedules("ns")
        sid = record["schedule_id"]
        self.assertEqual((record["timezone"], record["missed_policy"]), ("Europe/Paris", "skip"))
        self.assertIn("weekday summary", self.organ.invoke(self.context(turn), "schedule_list",
                                                           {}).content)
        shown = self.organ.invoke(self.context(turn), "schedule_list", {"schedule_id": sid})
        self.assertIn("Summarise notes", shown.content)
        update = lambda **arguments: self.organ.invoke(
            self.context(turn), "schedule_update", {"schedule_id": sid, **arguments})
        update(action="edit", prompt="Summarise notes briefly", every_seconds=3600)
        edited = self.store.get_schedule("ns", sid)
        self.assertEqual((edited["prompt"], edited["spec"]["kind"]),
                         ("Summarise notes briefly", "interval"))
        update(action="pause")
        self.assertEqual(self.store.get_schedule("ns", sid)["state"], "paused")
        update(action="resume")
        self.assertEqual(self.store.get_schedule("ns", sid)["state"], "active")
        update(action="run_now")
        self.assertEqual(self.store.get_schedule("ns", sid)["run_now_at"], self.clock())
        by_name = self.organ.invoke(self.context(turn), "schedule_update",
                                    {"schedule_id": "Weekday Summary", "action": "pause"})
        self.assertIn(sid, by_name.content)
        self.assertEqual(self.store.get_schedule("ns", sid)["state"], "paused")
        self.assertIn(sid, self.organ.invoke(self.context(turn), "schedule_list",
                                             {"schedule_id": "weekday summary"}).content)
        with self.assertRaisesRegex(OrganError, sid):  # a miss lists what exists
            self.organ.invoke(self.context(turn), "schedule_list", {"schedule_id": "daily"})
        update(action="remove")
        self.assertEqual(self.store.get_schedule("ns", sid)["state"], "removed")
        self.assertIn(sid, created.content)
        self.assertIn("Now:", self.organ.context(None))
        with self.assertRaises(OrganError):
            self.organ.invoke(self.context(turn), "schedule_create",
                              {"prompt": "x", "in_seconds": 5, "cron": "* * * * *"})


if __name__ == "__main__":
    unittest.main()
