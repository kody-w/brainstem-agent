"""H9 unit specs: resource hygiene.

Bounded, rotated logs (the event log, the daemon's output, worker logs); a retention policy
for receipts, run events, the egress log and the inbox with ``prune --dry-run`` reporting
exactly what ``prune`` then removes; store compaction; and a disk-space floor below which
new turns are refused with a clear message before anything is written.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

from acceptance_support import cli_json, criteria, isolated_env, private_dir, run_cli
from brainstem_agent import hygiene, observe, schedules, worker as worker_module
from brainstem_agent.host import AgentHost
from daemon_support import daemon_env, spawn_fake_daemon
from ops_support import FakeWorker, age_rows, seed_home, tool_turn

DAY = 86400


def counts(database: Path) -> dict:
    connection = sqlite3.connect(database)
    try:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("receipts", "run_events", "occurrences", "chats")}
    finally:
        connection.close()


class HygieneCase(unittest.TestCase):
    def setUp(self):
        self.home, self.workspace, self.environ = seed_home(self)
        self.scratch = private_dir(self)
        self.database = self.home / "state" / "agent.sqlite3"

    def env(self, **extra):
        return {**isolated_env(self.home, self.scratch),
                "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": self.environ[
                    "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"], **extra}

    def cli(self, *arguments, env=None):
        result = run_cli([*arguments, "--json"], env or self.env(), timeout=120)
        return result.returncode, cli_json(result)


class RetentionTests(HygieneCase):
    def age_everything(self):
        """Make the seeded records old and add records retention must keep."""
        connection = sqlite3.connect(self.database)
        schedule_id = connection.execute("SELECT schedule_id FROM schedules").fetchone()[0]
        namespace = connection.execute("SELECT namespace FROM schedules").fetchone()[0]
        for number in range(3):  # older inbox entries of the same schedule
            connection.execute(
                """INSERT INTO occurrences (occurrence_id, schedule_id, namespace, scheduled_at,
                   manual, state, claimed_at, finished_at, result_json)
                   VALUES (?, ?, ?, ?, 0, 'succeeded', ?, ?, '{}')""",
                (f"occ_old_{number}", schedule_id, namespace, 1000 + number, 1000 + number,
                 1001 + number))
        for seq in (1, 2):
            connection.execute(
                "INSERT INTO run_events VALUES (?, 'turn_x', ?, '{}', ?)",
                (namespace, seq, time.time() - 40 * DAY))
        # A schedule a conversation wrote: its turn's receipts carry taint provenance.
        turn = connection.execute("SELECT turn_id FROM receipts LIMIT 1").fetchone()[0]
        connection.execute("UPDATE schedules SET created_by = ?", (f"turn:{turn}",))
        connection.commit()
        connection.close()
        age_rows(self.database, "receipts", "created_at", 100 * DAY)
        age_rows(self.database, "occurrences", "claimed_at", 100 * DAY,
                 "occurrence_id NOT LIKE 'occ_old_%'")
        egress = self.home / "state" / "egress.jsonl"
        lines = [json.dumps({"at": "2020-01-01T00:00:00Z", "tool": "web_fetch", "host": "old"}),
                 json.dumps({"at": observe._iso(time.time()), "tool": "web_fetch",
                             "host": "new"})]
        egress.write_text("\n".join(lines) + "\n")
        os.chmod(egress, 0o600)
        (self.home / "operations.json").write_text(json.dumps(
            {"retention": {"inbox_keep_per_schedule": 1}}))
        return turn

    @criteria("H9")
    def test_prune_dry_run_reports_exactly_what_prune_then_removes(self):
        protected_turn = self.age_everything()
        receipts_total = counts(self.database)["receipts"]
        before_counts = counts(self.database)
        egress_before = (self.home / "state" / "egress.jsonl").read_bytes()
        code, dry = self.cli("prune", "--dry-run")
        self.assertEqual(code, 0, dry)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(counts(self.database), before_counts, "a dry run changes nothing")
        self.assertEqual((self.home / "state" / "egress.jsonl").read_bytes(), egress_before)
        categories = dry["categories"]
        connection = sqlite3.connect(self.database)
        protected = connection.execute("SELECT count(*) FROM receipts WHERE turn_id = ?",
                                       (protected_turn,)).fetchone()[0]
        connection.close()
        self.assertGreater(protected, 0)
        self.assertEqual(categories["receipts"]["count"], receipts_total - protected)
        self.assertEqual(categories["receipts"]["protected"], protected)
        self.assertEqual(categories["run_events"]["count"], 2)
        self.assertEqual(categories["inbox"]["count"], 3)
        self.assertEqual(categories["egress_log"]["count"], 1)
        code, done = self.cli("prune")
        self.assertEqual(code, 0, done)
        for name in ("receipts", "run_events", "inbox", "egress_log"):
            self.assertEqual(done["categories"][name]["deleted"], categories[name]["count"], name)
        after = counts(self.database)
        self.assertEqual(after["receipts"], protected)
        self.assertEqual(after["run_events"], 0)
        self.assertEqual(after["occurrences"], 1, "each schedule keeps its newest entries")
        self.assertEqual(after["chats"], before_counts["chats"], "turns and replay keys stay")
        remaining = (self.home / "state" / "egress.jsonl").read_text()
        self.assertIn('"new"', remaining)
        self.assertNotIn('"old"', remaining)
        events = observe.read_events(self.home, event="prune.completed")
        self.assertEqual(len(events), 1)

    @criteria("H9")
    def test_compaction_reclaims_space_and_needs_the_home_to_itself(self):
        self.age_everything()
        self.cli("prune")
        code, compacted = self.cli("compact")
        self.assertEqual(code, 0, compacted)
        self.assertEqual(compacted["quick_check"], "ok")
        self.assertLessEqual(compacted["bytes_after"], compacted["bytes_before"])
        self.assertIn("health", compacted)
        env = daemon_env(isolated_env(self.home, self.scratch),
                         Path(self.environ["BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"]), self.scratch)
        process = spawn_fake_daemon(self.home, self.workspace, env)
        self.addCleanup(lambda: (run_cli(["stop", "--json"], env, timeout=60),
                                 process.communicate(timeout=30)))
        code, refused = self.cli("compact", env=env)
        self.assertEqual(code, 1)
        self.assertIn("stop it first", refused["error"])


class DiskFloorTests(HygieneCase):
    @criteria("H9")
    def test_low_disk_refuses_new_turns_before_anything_is_written(self):
        before = counts(self.database)
        env = self.env(BRAINSTEM_AGENT_MIN_FREE_MB="100000000")
        result = run_cli(["chat", "hello", "--workspace", str(self.workspace), "--json"], env)
        self.assertEqual(result.returncode, 1)
        report = cli_json(result)
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["evidence"]["refused"], "disk")
        self.assertIn("Low disk space", report["error"])
        self.assertIn("prune", report["error"])
        self.assertEqual(counts(self.database), before, "a refused turn writes nothing")
        self.assertFalse(any((self.home / "workers").glob("*")) if (self.home / "workers")
                         .exists() else False)
        code, doctor = self.cli("doctor", env=env)
        self.assertEqual(code, 1)
        self.assertFalse(doctor["checks"]["disk_space"]["ok"])
        self.assertIn("prune", doctor["checks"]["disk_space"]["fix"])
        refused = observe.read_events(self.home, event="turn.refused")
        self.assertEqual(refused[-1]["reason"], "disk")

    @criteria("H9")
    def test_a_replay_is_never_refused_and_a_scheduled_run_is_refused_honestly(self):
        environ = {**self.environ, "BRAINSTEM_AGENT_MIN_FREE_MB": "100000000"}
        started = []

        def factory(**options):
            started.append(options["worker_id"])
            return FakeWorker(tool_turn(), **options)
        host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                         worker_factory=factory)
        first = host.chat("keyed turn", idempotency_key="k-1")
        self.assertTrue(first.ok, first.error)
        host.close()
        host = AgentHost(self.home, workspace=self.workspace, environ=environ,
                         worker_factory=factory)
        try:
            again = host.chat("keyed turn", idempotency_key="k-1")
            self.assertTrue(again.ok and again.replayed)
            record = schedules.create_schedule(
                host.store, namespace=host.namespace, workspace=str(host.workspace),
                prompt="Summarize", when={"every_seconds": 3600}, capabilities=["files.read"],
                allowed=host.known_capabilities(), default=host.turn_capabilities(),
                created_by="owner", now=time.time())
            schedules.change_schedule(host.store, host.namespace, record["schedule_id"],
                                      "run_now", {}, allowed=host.known_capabilities(),
                                      now=time.time(), writer="owner")
            count = len(started)
            with host.exclusive():
                [run] = schedules.Scheduler(host.store, lambda occurrence: schedules.run_occurrence(
                    host, occurrence)).tick(schedule_id=record["schedule_id"], limit=1)
            self.assertEqual(run["state"], "failed")
            self.assertIn("Low disk space", run["result"]["error"])
            self.assertEqual(len(started), count, "no worker started for the refused run")
        finally:
            host.close()


class LogTests(unittest.TestCase):
    @criteria("H9")
    def test_the_event_log_rotates_and_stays_bounded(self):
        home = private_dir(self)
        log = observe.EventLog(home, max_bytes=4000, keep=2)
        for number in range(600):
            log.write("test.event", number=number, padding="x" * 50)
        files = sorted(item.name for item in (home / "logs").iterdir()
                       if item.name.startswith("events"))
        self.assertEqual(files, ["events.jsonl", "events.jsonl.1", "events.jsonl.2"])
        for name in files:
            self.assertLess((home / "logs" / name).stat().st_size, 4000 + 400)
            self.assertEqual((home / "logs" / name).stat().st_mode & 0o777, 0o600)
        newest = observe.read_events(home, limit=1)[0]
        self.assertEqual(newest["number"], 599)

    @criteria("H9")
    def test_daemon_output_rotates_in_place_and_worker_logs_are_capped(self):
        home = private_dir(self)
        logs = home / "logs"
        (logs / "workers").mkdir(parents=True)
        daemon_log = logs / "daemon.log"
        writer = subprocess.Popen([sys.executable, "-c", (
            "import os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "os.write(fd, b'x' * 5000 + b'\\n')\n"
            "time.sleep(1.0)\n"
            "os.write(fd, b'after rotation\\n')\n"), str(daemon_log)])
        time.sleep(0.5)
        for number in range(25):
            path = logs / "workers" / f"w{number:02d}-g000000000000.log"
            path.write_text("line\n")
            os.utime(path, (1000 + number, 1000 + number))
        policy = hygiene.load_policy(home)[0]
        policy["logs"]["max_bytes"] = 1000
        done = hygiene.log_hygiene(home, policy)
        self.assertEqual(done["rotated"], ["daemon.log"])
        self.assertEqual(done["worker_logs_removed"], 5)
        writer.wait(10)
        self.assertEqual(daemon_log.read_text(), "after rotation\n",
                         "the daemon keeps appending to the truncated file")
        self.assertGreater((logs / "daemon.log.1").stat().st_size, 5000)
        remaining = sorted(item.name for item in (logs / "workers").iterdir())
        self.assertEqual(remaining[0], "w05-g000000000000.log", "the oldest ones went")

    @criteria("H9")
    def test_a_long_lived_workers_log_rotates_at_its_cap(self):
        home = private_dir(self)
        folder = home / "logs" / "workers"
        folder.mkdir(parents=True)
        fake = object.__new__(worker_module.GrailWorker)
        fake.log_path = folder / "w1-g1.log"
        fake._secrets = []
        fake._process = subprocess.Popen(
            [sys.executable, "-c", "import sys\nfor n in range(100): print('y' * 999)\n"],
            stdout=subprocess.PIPE)
        import threading
        fake._pumped = threading.Event()
        original = worker_module.LOG_MAX_BYTES
        worker_module.LOG_MAX_BYTES = 20_000
        try:
            fake._copy_log()
        finally:
            worker_module.LOG_MAX_BYTES = original
            fake._process.wait(10)
            fake._process.stdout.close()
        self.assertLessEqual(fake.log_path.stat().st_size, 20_000)
        self.assertLessEqual((folder / "w1-g1.log.1").stat().st_size, 20_000)

    @criteria("H9")
    def test_a_bad_operations_policy_keeps_defaults_and_is_an_advisory(self):
        home, scratch = private_dir(self), private_dir(self)
        (home / "operations.json").write_text(json.dumps(
            {"retention": {"receipts_days": -3, "nonsense": 1}, "disk": {"min_free_mb": "lots"},
             "other": {}}))
        policy, problems = hygiene.load_policy(home)
        self.assertEqual(policy["retention"]["receipts_days"], 90)
        self.assertEqual(policy["disk"]["min_free_mb"], 512)
        self.assertEqual(len(problems), 4, problems)
        doctor = cli_json(run_cli(["doctor", "--json"], isolated_env(home, scratch)))
        check = doctor["checks"]["operations_policy"]
        self.assertFalse(check["ok"])
        self.assertFalse(check["required"])
        self.assertIn("operations_policy", doctor["health"]["advisories"])
