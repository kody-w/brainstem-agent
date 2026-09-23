"""Live B2/B3/B4/B6/B8 specs: the always-on cell with real Copilot inference.

Skips unless BRAINSTEM_AGENT_LIVE=1. About 11 live turns: B2 5 (2 cold in-process,
3 warm through the daemon), B3 2 (the conversation, then the fired run), B4 1,
B6/B8 2 (a run killed mid-tool, then a missed run executed late after restart), and 1
for the tools that used to require no argument (list_files, schedule_list).
"""

import os
import signal
import statistics
import time
import unittest

from acceptance_support import (LIVE, cli_json, criteria, group_exists, pid_running,
                                record_metric, run_cli, wait_until)
from test_live import LiveCase

READY = "Reply with exactly the word: ready"


@unittest.skipUnless(LIVE, "set BRAINSTEM_AGENT_LIVE=1 to spend real Copilot turns")
class LiveDaemonTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.stop_daemon)

    def stop_daemon(self):
        from brainstem_agent import daemon

        record = daemon.read_record(self.home)
        if record is not None:
            run_cli(["stop", "--json"], self.env(), timeout=90)
            if pid_running(record["pid"]):
                os.kill(record["pid"], signal.SIGKILL)

    def command(self, *arguments, timeout=120):
        result = run_cli([*arguments, "--workspace", str(self.workspace), "--json"], self.env(),
                         timeout=timeout)
        self.outputs += [result.stdout, result.stderr]
        return result.returncode, cli_json(result)

    def serve(self):
        code, started = self.command("serve", "--detach")
        self.assertEqual(code, 0, started)
        seen = {}

        def warm():
            seen["status"] = self.command("status")[1]
            return bool(seen["status"].get("workers")) and seen["status"]["workers"][0]["warm"]
        self.assertTrue(wait_until(warm, 120, 0.25), seen.get("status"))
        return seen["status"]

    def runs(self, schedule_id):
        return self.command("schedules", "runs", schedule_id)[1]["runs"]

    def wait_finished(self, schedule_id, timeout):
        found = {}

        def done():
            found["runs"] = self.runs(schedule_id)
            return bool(found["runs"]) and found["runs"][0]["state"] != "running"
        self.assertTrue(wait_until(done, timeout, 0.5), found.get("runs"))
        return found["runs"][0]

    def receipts(self):
        return self.command("receipts")[1]["receipts"]

    @criteria("B2")
    def test_b2_chat_through_the_daemon_reuses_the_warm_worker_and_beats_cold_chat(self):
        cold = []
        for _ in range(2):
            started = time.monotonic()
            result, report = self.chat(READY)
            cold.append(time.monotonic() - started)
            self.assert_success(result, report)
            self.assertNotIn("daemon", report["evidence"])
            self.assertFalse(report["evidence"]["worker"]["reused"])
        status = self.serve()
        warm = []
        for _ in range(3):
            started = time.monotonic()
            result, report = self.chat(READY)
            warm.append(time.monotonic() - started)
            self.assert_success(result, report)
            self.assertEqual(report["evidence"]["daemon"]["pid"], status["pid"])
            self.assertTrue(report["evidence"]["worker"]["reused"])
            verified = report["evidence"]["worker"]["integrity_reuse"]
            self.assertTrue(verified["ok"])
            self.assertIn(".env", verified["untracked"])
        record_metric("cold_chat_seconds", round(statistics.median(cold), 3))
        record_metric("warm_chat_seconds", round(statistics.median(warm), 3))
        record_metric("cold_chat_samples", [round(value, 3) for value in cold])
        record_metric("warm_chat_samples", [round(value, 3) for value in warm])
        self.assertLess(statistics.median(warm), statistics.median(cold))

    @criteria("B3", "B12")
    def test_b3_a_conversation_schedules_work_that_the_daemon_fires_on_time(self):
        from brainstem_agent import schedules

        self.serve()
        asked = time.time()
        result, report = self.chat("In 2 minutes, write the current time into notes/time.txt")
        self.assert_success(result, report)
        record_metric("b3_answer", report["response"]["response"][:300])
        self.assertTrue(any(r["tool"] == "schedule_create" and r["state"] == "succeeded"
                            for r in report["evidence"]["receipts"]), report["evidence"])
        self.assertFalse((self.workspace / "notes" / "time.txt").exists())
        [schedule] = self.command("schedules", "list")[1]["schedules"]
        self.assertEqual(schedule["spec"]["kind"], "once")
        self.assertEqual(schedule["timezone"], schedules.local_zone())
        self.assertTrue(asked + 90 <= schedule["next_fire_at"] <= asked + 150, schedule)
        self.assertEqual(schedule["created_by"], "turn:" + report["turn_id"])
        record_metric("b3_schedule", {key: schedule[key] for key in (
            "spec", "timezone", "next_fire_local", "capabilities", "prompt")})
        run = self.wait_finished(schedule["schedule_id"],
                                 schedule["next_fire_at"] - time.time() + 240)
        self.assertEqual(run["state"], "succeeded", run)
        delay = run["claimed_at"] - run["scheduled_at"]
        record_metric("fire_delay_seconds", round(delay, 3))
        record_metric("fire_to_turn_start_seconds",
                      round(run["result"]["started_at"] - run["scheduled_at"], 3))
        self.assertLess(delay, 2.0)
        written = (self.workspace / "notes" / "time.txt").read_text()
        self.assertTrue(written.strip())
        record_metric("b3_time_txt", written[:120])
        # The run's own tool wrote it (write_file, or run_command when the model chose a
        # shell-only schedule such as ``date > notes/time.txt``).
        self.assertTrue(any(r["tool"] in ("write_file", "run_command") and r["state"] ==
                            "succeeded" and r["turn_id"] == run["turn_id"]
                            for r in self.receipts()))
        inbox = self.command("inbox")[1]["inbox"]
        self.assertEqual(inbox[0]["occurrence_id"], run["occurrence_id"])
        self.assertTrue(inbox[0]["result"]["response"])
        record_metric("b3_inbox_response", inbox[0]["result"]["response"][:300])

    @criteria("A2", "B4")
    def test_tools_that_used_to_require_no_argument_reach_the_cell_live(self):
        """list_files and schedule_list require an argument, so a
        real model's calls reach the cell instead of empty arguments Grail refuses."""
        self.serve()
        code, created = self.command("schedules", "create", "--name", "weekly-review", "--cron",
                                     "0 9 * * 1", "--prompt", "Review my notes.")
        self.assertEqual(code, 0, created)
        (self.workspace / "notes").mkdir()
        (self.workspace / "notes" / "ideas.txt").write_text("an idea\n")
        result, report = self.chat(
            "Use the list_files tool to list my notes folder and the schedule_list tool to "
            "list my schedules. Then tell me the file names and the schedule names.")
        self.assert_success(result, report)
        record_metric("item1_answer", report["response"]["response"][:300])
        receipts = report["evidence"]["receipts"]
        record_metric("item1_receipts", [f"{r['tool']}:{r['state']}" for r in receipts])
        for tool in ("list_files", "schedule_list"):
            self.assertTrue(any(r["tool"] == tool and r["state"] == "succeeded"
                                for r in receipts), (tool, receipts))
        answer = report["response"]["response"]
        self.assertIn("ideas.txt", answer)
        self.assertIn("weekly-review", answer)

    @criteria("B4")
    def test_b4_a_chat_pauses_a_schedule_it_finds_by_name(self):
        self.serve()
        code, created = self.command("schedules", "create", "--name", "daily-report", "--cron",
                                     "0 9 * * *", "--prompt", "Summarize my notes.")
        self.assertEqual(code, 0, created)
        result, report = self.chat("Pause my schedule named daily-report, then confirm its state.")
        self.assert_success(result, report)
        record_metric("b4_answer", report["response"]["response"][:300])
        shown = self.command("schedules", "show", created["schedule"]["schedule_id"])[1]
        self.assertEqual(shown["schedule"]["state"], "paused")
        self.assertTrue(any(r["tool"] == "schedule_update" and r["state"] == "succeeded"
                            for r in report["evidence"]["receipts"]), report["evidence"])

    @criteria("B6", "B8")
    def test_b6_b8_a_killed_run_stays_uncertain_and_a_restart_runs_the_missed_one_late(self):
        status = self.serve()
        sleeper = self.command(
            "schedules", "create", "--in", "3", "--name", "sleeper", "--prompt",
            "Use the run_command tool to run exactly this command: sleep 45\n"
            "Then report its output.")[1]["schedule"]
        self.assertTrue(wait_until(lambda: any(
            r["tool"] == "run_command" and r["state"] == "started" for r in self.receipts()),
            150, 0.3), "the scheduled run never started its tool")
        worker = self.command("status")[1]["workers"][0]
        os.kill(status["pid"], signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not group_exists(worker["pgid"]), 5))
        missed = self.command(
            "schedules", "create", "--in", "3", "--name", "late-note", "--capabilities",
            "files.read,files.write", "--prompt",
            "Write exactly the word late into notes/late.txt.")[1]["schedule"]
        self.assertTrue(wait_until(lambda: time.time() > missed["next_fire_at"] + 3, 20))
        self.serve()
        [crashed] = self.runs(sleeper["schedule_id"])
        self.assertEqual(crashed["state"], "uncertain")
        self.assertIn("not run again", crashed["result"]["error"])
        late = self.wait_finished(missed["schedule_id"], 240)
        self.assertEqual(late["state"], "succeeded", late)
        self.assertGreater(late["late_seconds"], 2.0)
        record_metric("b6_late_seconds", late["late_seconds"])
        self.assertEqual((self.workspace / "notes" / "late.txt").read_text().strip(), "late")
        self.assertEqual(len(self.runs(sleeper["schedule_id"])), 1)
        errors = self.command("status")[1]["last_errors"]
        self.assertTrue(any(item["source"] == crashed["occurrence_id"] for item in errors))


if __name__ == "__main__":
    unittest.main()
