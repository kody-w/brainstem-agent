"""Overlap skips recorded when they happen, and missed-run counts.

Virtual clock, real store and scheduler. While a schedule's run is still going,
each of its instants that comes due is recorded ``skipped`` (overlap) at once,
under its own occurrence identity, instead of once the run ends. Instants the loop
could not see in time (the clock jumped, the machine slept) are recorded once for
the stretch, with a count. Every missed run records how many scheduled instants
never ran (``missed_count``), and the scheduled turn is told.
"""

import threading
import time
import unittest

from acceptance_support import criteria, wait_until
from brainstem_agent import schedules
from brainstem_agent.host import TurnResult
from brainstem_agent.schedules import change_schedule
from test_cell_schedules import ALL, StoreCase, utc


class FakeHost:
    def __init__(self):
        self.messages = []

    def chat(self, message, **_options):
        self.messages.append(message)
        return TurnResult(True, "succeeded", False, "turn_x", "s_x", {"response": "done"}, None,
                          {"receipts": []})


class OverlapAtFireTimeTests(StoreCase):
    def runs_of(self, schedule_id):
        return self.store.list_occurrences("ns", schedule_id=schedule_id)

    @criteria("B7")
    def test_instants_that_come_due_during_a_run_are_skipped_at_once(self):
        item = self.create({"every_seconds": 60})
        sid, start = item["schedule_id"], item["next_fire_at"]
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocking(occurrence):
            entered.set()
            release.wait(30)
            return {"state": "succeeded", "result": {"response": "ok"}}
        loop = self.scheduler(blocking)
        self.clock.now = start
        done = {}
        thread = threading.Thread(target=lambda: done.update(records=loop.tick()), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(10))
        for step in (1, 2):
            self.clock.now = start + 60 * step + 1
            loop.wake()
            due = start + 60 * step
            self.assertTrue(wait_until(lambda: any(run["scheduled_at"] == due
                                                   for run in self.runs_of(sid)), 5),
                            f"the instant due {step} minute(s) in was not recorded during the run")
            [skipped] = [run for run in self.runs_of(sid) if run["scheduled_at"] == due]
            self.assertEqual((skipped["state"], skipped["reason"], skipped["manual"],
                              skipped["missed_count"]), ("skipped", "overlap", False, 1))
            self.assertEqual(skipped["occurrence_id"], f"occ_{sid}_{due}")
        change_schedule(self.store, "ns", sid, "run_now", {}, allowed=ALL, now=self.clock())
        loop.wake()
        self.assertTrue(wait_until(lambda: any(run["manual"] for run in self.runs_of(sid)), 5),
                        "a run-now made during the run was not recorded until it ended")
        [manual] = [run for run in self.runs_of(sid) if run["manual"]]
        self.assertEqual((manual["state"], manual["reason"]), ("skipped", "overlap"))
        self.assertEqual([run["state"] for run in self.runs_of(sid) if run["scheduled_at"] == start],
                         ["running"])
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual([(r["state"], r["reason"]) for r in done["records"]],
                         [("succeeded", None), ("skipped", "overlap"), ("skipped", "overlap"),
                          ("skipped", "overlap")])
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.store.get_schedule("ns", sid)["next_fire_at"], start + 180)

    @criteria("B7")
    def test_an_instant_due_before_the_run_began_waits_for_it_without_a_busy_loop(self):
        item = self.create({"every_seconds": 60})
        sid, first = item["schedule_id"], item["next_fire_at"]
        self.clock.now = first + 5
        # A run-now requested before the instant: it runs first; the instant stays due.
        change_schedule(self.store, "ns", sid, "run_now", {}, allowed=ALL, now=first - 1)
        entered, release, calls = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)
        original = self.store.claim_due
        self.store.claim_due = lambda *args, **kwargs: (calls.append(1),
                                                        original(*args, **kwargs))[1]

        def blocking(occurrence):
            entered.set()
            release.wait(30)
            return {"state": "succeeded", "result": {"response": "ok"}}
        loop = self.scheduler(blocking)
        done = {}
        thread = threading.Thread(target=lambda: done.update(records=loop.tick()), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(10))
        time.sleep(1.0)
        self.assertLess(len(calls), 10, "the loop spun on an instant it cannot claim yet")
        release.set()
        thread.join(10)
        self.assertEqual([(r["manual"], r["state"]) for r in done["records"]],
                         [(True, "succeeded"), (False, "succeeded")])
        self.assertEqual(len(self.runs), 2)

    @criteria("B7")
    def test_an_overlap_the_loop_could_not_see_in_time_is_recorded_once_with_its_count(self):
        item = self.create({"every_seconds": 60})
        start = item["next_fire_at"]

        def long_turn(occurrence):
            self.clock.now += 150  # the machine slept through two more fire times
            return {"state": "succeeded", "result": {"response": "ok"}}
        self.clock.now = start
        records = self.scheduler(long_turn).tick()
        self.assertEqual([(r["state"], r["reason"]) for r in records],
                         [("succeeded", None), ("skipped", "overlap")])
        self.assertEqual((records[1]["scheduled_at"], records[1]["missed_count"],
                          records[1]["missed_from"]), (start + 120, 2, start + 60))


class MissedCountTests(StoreCase):
    @criteria("B6")
    def test_missed_runs_record_how_many_scheduled_instants_never_ran(self):
        latest = self.create({"every_seconds": 60})
        skip = self.create({"every_seconds": 60}, policy="skip")
        cron = self.create({"cron": "*/15 * * * *"})
        once = self.create({"in_seconds": 30})
        once_skip = self.create({"in_seconds": 30}, policy="skip")
        host = FakeHost()
        self.clock.now = utc("2026-09-23T13:00") + 5  # down since 12:00
        loop = self.scheduler(lambda occurrence: schedules.run_occurrence(host, occurrence))
        results = {item["schedule_id"]: item for item in loop.tick()}
        view = lambda item: {key: results[item["schedule_id"]][key] for key in (
            "state", "scheduled_at", "missed_count", "missed_from")}
        self.assertEqual(view(latest), {"state": "succeeded", "scheduled_at": utc("2026-09-23T13:00"),
                                        "missed_count": 59, "missed_from": utc("2026-09-23T12:01")})
        self.assertEqual(view(skip), {"state": "skipped", "scheduled_at": utc("2026-09-23T13:00"),
                                      "missed_count": 60, "missed_from": utc("2026-09-23T12:01")})
        self.assertEqual(view(cron), {"state": "succeeded", "scheduled_at": utc("2026-09-23T13:00"),
                                      "missed_count": 3, "missed_from": utc("2026-09-23T12:15")})
        self.assertEqual(view(once)["missed_count"], 0)  # it ran, late
        self.assertEqual(view(once_skip)["missed_count"], 1)
        stored = self.store.get_occurrence(results[latest["schedule_id"]]["occurrence_id"])
        self.assertEqual((stored["missed_count"], stored["result"]["response"]), (59, "done"))
        self.assertEqual(len(host.messages), 3)
        self.assertTrue(any("59 earlier scheduled runs were missed" in message
                            for message in host.messages), host.messages)

    @criteria("B6", "B8")
    def test_a_crashed_late_run_keeps_its_missed_count(self):
        item = self.create({"every_seconds": 60})
        self.clock.now = item["next_fire_at"] + 3 * 60 + 5
        loop = self.scheduler()
        claimed = self.store.claim_due(self.clock(), loop.plan)
        self.assertEqual((claimed["state"], claimed["missed_count"]), ("running", 3))
        self.store.recover_interrupted()
        crashed = self.store.get_occurrence(claimed["occurrence_id"])
        self.assertEqual((crashed["state"], crashed["missed_count"]), ("failed", 3))
        self.assertIn("not run again", crashed["result"]["error"])

    @criteria("B6")
    def test_on_time_runs_record_no_missed_runs(self):
        item = self.create({"every_seconds": 60})
        self.clock.now = item["next_fire_at"]
        [ran] = self.scheduler().tick()
        self.assertEqual((ran["state"], ran["missed_count"]), ("succeeded", 0))


if __name__ == "__main__":
    unittest.main()
