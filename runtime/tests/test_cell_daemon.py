"""B1-B4 and B6-B10 unit specs: real daemon processes and the public CLI, fake Grail.

The daemon, store, broker, grants, organs, sandbox and lifeline are the real
product; only the Grail process is replaced (``daemon_support.ScriptedWorker``).
"""

import json
import os
import plistlib
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from acceptance_support import (CANARY_TOKEN, criteria, isolated_env, kill_quietly, leaks,
                                pid_running, private_dir, record_metric, run_cli, cli_json,
                                wait_until, write_token_file)
from brainstem_agent import daemon, sandbox
from brainstem_agent.grail import GrailSourceError
from brainstem_agent.host import AgentHost
from daemon_support import ScriptedWorker, daemon_env, spawn_fake_daemon

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def directive(tool, **arguments):
    return f"[[{tool} {json.dumps(arguments)}]]"


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.scratch = private_dir(self)
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(self.scratch)
        self.env = daemon_env(isolated_env(self.home, self.scratch,
                                           BRAINSTEM_AGENT_CACHE=str(self.home / "cache")),
                              self.token, self.scratch)
        self.outputs = []
        self.processes = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                run_cli(["stop", "--json"], self.env, timeout=60)
            if process.poll() is None:
                kill_quietly(process.pid, group=False)
            out, err = process.communicate(timeout=30)
            self.outputs += [out, err]
        record = daemon.read_record(self.home)
        if record is not None:  # a detached daemon started through the CLI
            run_cli(["stop", "--json"], self.env, timeout=60)
            kill_quietly(record["pid"], group=False)
        self.assertEqual(leaks({"canary": CANARY_TOKEN}, roots=[self.home],
                               texts=self.outputs), [])

    def start(self):
        process = spawn_fake_daemon(self.home, self.workspace, self.env)
        self.processes.append(process)
        return process

    def cli(self, *arguments, workspace=True, timeout=120):
        extra = ["--workspace", str(self.workspace)] if workspace else []
        result = run_cli([*arguments, *extra, "--json"], self.env, timeout=timeout)
        self.outputs += [result.stdout, result.stderr]
        return result.returncode, cli_json(result)

    def chat(self, message, *extra):
        return self.cli("chat", message, *extra)

    def wait_run(self, schedule_id, predicate=lambda run: run["state"] != "running", timeout=20):
        found = {}

        def ready():
            runs = self.cli("schedules", "runs", schedule_id)[1]["runs"]
            found["runs"] = runs
            return any(predicate(run) for run in runs)
        self.assertTrue(wait_until(ready, timeout, 0.2), found.get("runs"))
        return found["runs"]

    def token_value(self):
        return json.loads((self.home / "run" / "daemon.json").read_text())["token"]


class LifecycleTests(DaemonCase):
    @criteria("B1")
    def test_serve_status_second_serve_refuses_and_stop_is_clean(self):
        process = self.start()
        code, status = self.cli("status")
        self.assertEqual(code, 0)
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], process.pid)
        self.assertEqual(status["health"], "ok")
        self.assertTrue(status["scheduler"]["loop_alive"])
        self.assertEqual(len(status["workers"]), 1)
        self.assertTrue(status["workers"][0]["warm"])
        self.assertIn("last_errors", status)
        code, refused = self.cli("serve")
        self.assertEqual(code, 1)
        self.assertIn("already running", refused["error"])
        self.assertIn(str(process.pid), refused["error"])
        code, stopped = self.cli("stop", workspace=False)
        self.assertEqual(code, 0, stopped)
        self.assertTrue(stopped["stopped"] and stopped["workers_gone"])
        self.assertEqual(process.wait(15), 0)
        self.assertFalse((self.home / "run" / "daemon.json").exists())
        code, after = self.cli("status")
        self.assertEqual((code, after["running"]), (1, False))
        record_metric("stop_seconds", stopped["seconds"])

    @criteria("B1")
    def test_foreground_serve_prints_its_status_and_sigterm_stops_it_cleanly(self):
        process = subprocess.Popen([sys.executable, "-m", "brainstem_agent", "serve",
                                    "--workspace", str(self.workspace), "--json"], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        ready = json.loads(process.stdout.readline())
        self.assertEqual((ready["running"], ready["pid"]), (True, process.pid))
        self.assertEqual(self.cli("status")[1]["pid"], process.pid)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(15), 0)
        stopped = json.loads(process.stdout.readline())
        self.assertTrue(stopped["stopped"] and stopped["group_gone"])
        self.assertIsNone(daemon.read_record(self.home))

    @criteria("B1", "B2")
    def test_detached_serve_through_the_cli_and_in_process_chat_without_a_daemon(self):
        code, started = self.cli("serve", "--detach")
        self.assertEqual(code, 0, started)
        pid = started["pid"]
        self.addCleanup(kill_quietly, pid, group=False)
        self.assertTrue(pid_running(pid))
        # No Grail cache in the unit tier: the daemon stays up and reports why it is not warm.
        code, status = self.cli("status")
        self.assertEqual((code, status["pid"]), (0, pid))
        self.assertTrue(any("warm worker" in item["error"] for item in status["last_errors"]))
        token = self.token_value()
        code, refused = self.cli("serve", "--detach")
        self.assertEqual(code, 1)
        self.assertIn(f"already running for this home (pid {pid})", refused["error"])
        code, stopped = self.cli("stop", workspace=False)
        self.assertEqual(code, 0, stopped)
        self.assertTrue(wait_until(lambda: not pid_running(pid), 10))
        code, report = self.chat("hello")  # in-process again, exactly as without a daemon
        self.assertEqual(report["state"], "failed")
        self.assertNotIn("daemon", report.get("evidence") or {})
        self.assertIn("Grail", report["error"])
        self.assertEqual(leaks({"daemon-token": token}, roots=[self.home / "logs"],
                               texts=self.outputs), [])


class WarmPathTests(DaemonCase):
    @criteria("B2")
    def test_chat_goes_through_the_daemon_and_reuses_the_verified_warm_worker(self):
        process = self.start()
        timings = []
        for _ in range(3):
            started = time.monotonic()
            code, report = self.chat("[[tools]]")
            timings.append(time.monotonic() - started)
            self.assertEqual(code, 0, report)
            self.assertEqual(report["evidence"]["daemon"]["pid"], process.pid)
            self.assertTrue(report["evidence"]["worker"]["reused"])
            self.assertTrue(report["evidence"]["worker"]["integrity_reuse"]["ok"])
        self.assertIn("schedule_create", report["response"]["response"])
        record_metric("unit_daemon_chat_seconds", round(sorted(timings)[1], 3))
        # Turns share the one warm worker: a chat waits for it, bounded by its own timeout.
        busy = subprocess.Popen([sys.executable, "-m", "brainstem_agent", "chat", "[[pause 4]]",
                                 "--workspace", str(self.workspace), "--json"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertTrue(wait_until(lambda: (self.cli("status")[1]["active_turn"] or {}).get(
            "phase") == "streaming", 15, 0.1))
        started = time.monotonic()
        code, refused = self.chat("[[tools]]", "--timeout", "1")
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual((code, refused["state"]), (1, "failed"))
        self.assertIn("busy", refused["error"])
        out, err = busy.communicate(timeout=30)
        self.outputs += [out, err]
        self.assertEqual(json.loads(out)["state"], "succeeded")
        # RAPP/1 /chat: exactly response, agent_logs, session_id.
        request = urllib.request.Request(
            f"http://127.0.0.1:{daemon.read_record(self.home)['port']}/chat",
            data=json.dumps({"user_input": "[[tools]]"}).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.token_value(),
                     "Content-Type": "application/json"})
        with OPEN.open(request, timeout=30) as response:
            envelope = json.loads(response.read())
        self.assertEqual(set(envelope), {"response", "agent_logs", "session_id"})
        self.assertIsInstance(envelope["agent_logs"], list)


class IntegrityCheckedWorker(ScriptedWorker):
    tampered = False

    def verify_integrity(self):
        if self.tampered:
            raise GrailSourceError("Grail source mismatch: soul.md (sha256 differs)")
        return [".env"]


class WarmIntegrityTests(unittest.TestCase):
    @criteria("B2")
    def test_a_warm_worker_is_reused_only_while_its_copy_still_verifies(self):
        scratch = private_dir(self)
        environ = daemon_env({"HOME": str(scratch)}, write_token_file(scratch), scratch)
        workers = []

        def factory(**options):
            workers.append(IntegrityCheckedWorker(**options))
            return workers[-1]
        host = AgentHost(private_dir(self), workspace=private_dir(self), environ=environ,
                         worker_factory=factory)
        self.addCleanup(host.close)
        self.assertFalse(host.chat("[[tools]]").evidence["worker"]["reused"])
        reused = host.chat("[[tools]]").evidence["worker"]
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["integrity_reuse"]["untracked"], [".env"])
        workers[0].tampered = True
        replaced = host.chat("[[tools]]")
        self.assertTrue(replaced.ok, replaced.error)
        self.assertFalse(replaced.evidence["worker"]["reused"])
        self.assertIn("soul.md", replaced.evidence["worker"]["replaced_after_integrity_failure"])
        self.assertEqual(len(workers), 2)
        self.assertTrue(workers[0].stopped.is_set())


class ScheduleFromChatTests(DaemonCase):
    @criteria("B3", "B12")
    def test_a_chat_tool_creates_a_durable_schedule_that_fires_and_leaves_a_result(self):
        self.start()
        prompt = directive("write_file", path="notes/time.txt", content="fired")
        code, report = self.chat(directive("schedule_create", prompt=prompt, in_seconds=2,
                                           name="time"))
        self.assertEqual(code, 0, report)
        [schedule] = self.cli("schedules", "list")[1]["schedules"]
        self.assertEqual(schedule["spec"]["kind"], "once")
        self.assertTrue(schedule["timezone"] and "/" in schedule["timezone"] or
                        schedule["timezone"] == "UTC")
        self.assertIsInstance(schedule["next_fire_at"], int)
        self.assertEqual(schedule["created_by"], "turn:" + report["turn_id"])
        [run] = self.wait_run(schedule["schedule_id"])
        self.assertEqual(run["state"], "succeeded", run)
        self.assertEqual((self.workspace / "notes" / "time.txt").read_text(), "fired")
        delay = run["claimed_at"] - run["scheduled_at"]
        self.assertLess(delay, 1.0)
        record_metric("unit_fire_delay_seconds", round(delay, 3))
        [item] = self.cli("inbox")[1]["inbox"]
        self.assertEqual((item["occurrence_id"], item["name"]), (run["occurrence_id"], "time"))
        self.assertIn("Wrote 5 bytes", item["result"]["response"])
        receipts = self.cli("receipts")[1]["receipts"]
        self.assertTrue(any(r["tool"] == "write_file" and r["turn_id"] == run["turn_id"]
                            and r["state"] == "succeeded" for r in receipts))


class ManagementTests(DaemonCase):
    @criteria("B4")
    def test_cli_management_works_through_restarts(self):
        process = self.start()
        code, created = self.cli("schedules", "create", "--prompt", "[[tools]]", "--cron",
                                 "0 9 * * 1-5", "--timezone", "Europe/Paris", "--name", "daily",
                                 "--missed", "skip")
        self.assertEqual(code, 0, created)
        sid = created["schedule"]["schedule_id"]
        self.assertTrue(created["daemon_running"])
        # An owner-created schedule gets an owner chat turn's defaults (the long-turn and
        # web capabilities included).
        self.assertEqual(created["schedule"]["capabilities"],
                         ["files.read", "files.write", "memory.read", "memory.write", "shell.run",
                          "schedule.read", "schedule.write", "skills.read", "skills.write",
                          "sessions.read", "agents.delegate", "scripts.run", "processes.run",
                          "web.fetch", "web.search"])
        code, edited = self.cli("schedules", "edit", sid, "--every", "3600", "--prompt",
                                "[[tools]] edited", "--capabilities", "files.read")
        self.assertEqual((code, edited["schedule"]["spec"]["kind"]), (0, "interval"))
        self.assertEqual(self.cli("schedules", "pause", sid)[1]["schedule"]["state"], "paused")
        code, ran = self.cli("schedules", "run-now", sid)
        self.assertEqual(code, 0, ran)
        self.assertEqual((ran["run"]["state"], ran["run"]["manual"]), ("succeeded", True))
        self.assertEqual(ran["run"]["result"]["response"], "tools=list_files,read_file")
        code, shown = self.cli("schedules", "show", sid)
        self.assertEqual(shown["schedule"]["prompt"], "[[tools]] edited")
        self.assertEqual(len(shown["runs"]), 1)
        run_cli(["stop", "--json"], self.env, timeout=60)
        self.assertEqual(process.wait(15), 0)
        self.start()  # every change survives the restart
        [kept] = self.cli("schedules", "list")[1]["schedules"]
        self.assertEqual((kept["state"], kept["spec"]["kind"], kept["capabilities"],
                          kept["missed_policy"]), ("paused", "interval", ["files.read"], "skip"))
        self.assertEqual(self.cli("schedules", "resume", sid)[1]["schedule"]["state"], "active")
        self.assertEqual(self.cli("schedules", "remove", sid)[1]["schedule"]["state"], "removed")
        self.assertEqual(self.cli("schedules", "list")[1]["schedules"], [])
        self.assertEqual(len(self.cli("schedules", "list", "--all")[1]["schedules"]), 1)
        code, refused = self.cli("schedules", "create", "--prompt", "x", "--in", "5", "--cron",
                                 "* * * * *")
        self.assertEqual(code, 1)
        self.assertIn("exactly one", refused["error"])

    @criteria("B4", "B9")
    def test_chat_tools_manage_schedules_within_the_turns_capabilities(self):
        self.start()
        code, owner = self.cli("schedules", "create", "--prompt", "[[tools]]", "--every", "600")
        sid = owner["schedule"]["schedule_id"]
        reads_only = ("--capabilities", "files.read,schedule.read,schedule.write")
        code, report = self.chat(directive("schedule_update", schedule_id=sid, action="pause"),
                                 *reads_only)
        self.assertIn("schedule_update:200:False", report["response"]["response"])
        self.assertEqual(self.cli("schedules", "show", sid)[1]["schedule"]["state"], "active")
        code, report = self.chat(
            directive("schedule_update", schedule_id=sid, action="pause")
            + directive("schedule_list", schedule_id=sid))
        self.assertIn("paused", report["response"]["response"])
        code, report = self.chat(directive("schedule_update", schedule_id=sid, action="run_now"))
        [run] = self.wait_run(sid)
        self.assertEqual((run["manual"], run["state"]), (True, "succeeded"))
        code, report = self.chat(directive("schedule_update", schedule_id=sid, action="remove"))
        self.assertEqual(self.cli("schedules", "show", sid)[1]["schedule"]["state"], "removed")


class MissedOverlapCrashTests(DaemonCase):
    @criteria("B6")
    def test_a_real_restart_applies_each_schedules_missed_run_policy(self):
        process = self.start()
        # Created through the running daemon, due far enough away that no load can make them
        # fire before the stop; once it has stopped they are made due at once, so every run
        # is missed while the daemon is down, however long the stop took.
        late = self.cli("schedules", "create", "--prompt", "[[tools]]", "--in", "3600")[1]
        skip = self.cli("schedules", "create", "--prompt", "[[tools]]", "--in", "3600",
                        "--missed", "skip")[1]
        self.assertTrue(late["daemon_running"] and skip["daemon_running"])
        run_cli(["stop", "--json"], self.env, timeout=60)
        self.assertEqual(process.wait(60), 0)
        due = 0.0
        for created in (late, skip):
            code, edited = self.cli("schedules", "edit", created["schedule"]["schedule_id"],
                                    "--in", "1")
            self.assertEqual(code, 0, edited)
            self.assertFalse(edited["daemon_running"])
            due = max(due, edited["schedule"]["next_fire_at"])
        self.assertEqual(self.cli("schedules", "show", skip["schedule"]["schedule_id"])[1]
                         ["schedule"]["missed_policy"], "skip")
        self.assertTrue(wait_until(lambda: time.time() > due + 1.5, 30))
        self.start()
        [ran] = self.wait_run(late["schedule"]["schedule_id"])
        self.assertEqual(ran["state"], "succeeded")
        self.assertGreater(ran["late_seconds"], 1.0)
        [skipped] = self.wait_run(skip["schedule"]["schedule_id"])
        self.assertEqual((skipped["state"], skipped["reason"]), ("skipped", "missed"))
        record_metric("restart_late_seconds", ran["late_seconds"])

    @criteria("B7")
    def test_a_run_now_while_the_same_schedule_runs_is_recorded_as_skipped(self):
        self.start()
        sid = self.cli("schedules", "create", "--prompt", "[[pause 5]]", "--every", "3600")[1][
            "schedule"]["schedule_id"]
        first = subprocess.Popen([sys.executable, "-m", "brainstem_agent", "schedules", "run-now",
                                  sid, "--workspace", str(self.workspace), "--json"],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        self.wait_run(sid, lambda run: run["state"] == "running", timeout=15)
        time.sleep(1.1)
        code, second = self.cli("schedules", "run-now", sid)
        out, err = first.communicate(timeout=60)
        self.outputs += [out, err]
        self.assertEqual(json.loads(out)["run"]["state"], "succeeded")
        self.assertEqual(code, 1)
        self.assertEqual((second["run"]["state"], second["run"]["reason"]), ("skipped", "overlap"))
        runs = self.cli("schedules", "runs", sid)[1]["runs"]
        self.assertEqual(sorted(run["state"] for run in runs), ["skipped", "succeeded"])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("B8")
    def test_kill_9_mid_occurrence_is_uncertain_after_a_tool_else_failed_and_others_continue(self):
        process = self.start()
        tool = self.cli("schedules", "create", "--prompt",
                        directive("run_command", command="echo $$ > sleeper.pid; exec sleep 60",
                                  timeout_seconds=120), "--in", "1")[1]["schedule"]
        self.wait_run(tool["schedule_id"], lambda run: run["state"] == "running")
        marker = self.workspace / "sleeper.pid"
        self.assertTrue(wait_until(lambda: marker.exists() and marker.read_text().strip(), 15))
        sleeper = int(marker.read_text())
        self.addCleanup(kill_quietly, sleeper)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(10)
        self.assertTrue(wait_until(lambda: not pid_running(sleeper), 5), "the tool outlived it")
        quiet = self.cli("schedules", "create", "--prompt", "[[pause 30]]", "--in", "1")[1][
            "schedule"]
        later = self.cli("schedules", "create", "--prompt", "[[tools]]", "--in", "4")[1][
            "schedule"]
        process = self.start()
        [crashed] = self.cli("schedules", "runs", tool["schedule_id"])[1]["runs"]
        self.assertEqual(crashed["state"], "uncertain")
        self.assertIn("not run again", crashed["result"]["error"])
        self.wait_run(quiet["schedule_id"], lambda run: run["state"] == "running")
        time.sleep(0.5)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(10)
        self.start()
        [quiet_run] = self.cli("schedules", "runs", quiet["schedule_id"])[1]["runs"]
        self.assertEqual(quiet_run["state"], "failed")
        [other] = self.wait_run(later["schedule_id"])
        self.assertEqual(other["state"], "succeeded")
        self.assertEqual(len(self.cli("schedules", "runs", tool["schedule_id"])[1]["runs"]), 1)


class AuthorityTests(DaemonCase):
    def raw(self, path, *, token=None, host=None, body=None):
        port = daemon.read_record(self.home)["port"]
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if host:
            headers["Host"] = host
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST"
                                         if body is not None else "GET", headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            with OPEN.open(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    @criteria("B9")
    def test_the_control_surface_is_owner_only(self):
        self.start()
        run, record = self.home / "run", self.home / "run" / "daemon.json"
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        token = self.token_value()
        self.assertEqual(self.raw("/v1/status", token=token), 200)
        self.assertEqual(self.raw("/v1/status"), 401)
        self.assertEqual(self.raw("/v1/status", token="x" * 43), 401)
        self.assertEqual(self.raw("/v1/stop", body={}), 401)
        self.assertEqual(self.raw("/v1/status", token=token, host="evil.example:80"), 403)
        self.assertTrue(daemon.read_record(self.home))  # the refused stop did nothing
        code, status = self.cli("status")
        self.assertNotIn(token, json.dumps(status))
        self.assertEqual(leaks({"daemon-token": token}, roots=[self.home / "logs"],
                               texts=self.outputs), [])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("B9")
    def test_sandboxed_commands_can_neither_read_the_token_nor_reach_the_daemon(self):
        self.start()
        port = daemon.read_record(self.home)["port"]
        command = (f"cat {self.home / 'run' / 'daemon.json'} >/dev/null 2>&1 && echo READ || "
                   f"echo NO_READ; /usr/bin/curl -s -m 3 http://127.0.0.1:{port}/v1/status "
                   ">/dev/null && echo REACHED || echo NO_REACH")
        code, result = self.cli("tool", "run_command", "--arguments",
                                json.dumps({"command": command}))
        self.assertIn("NO_READ", result["content"])
        self.assertIn("NO_REACH", result["content"])

    @criteria("B9")
    def test_each_scheduled_run_gets_a_fresh_grant_with_only_the_schedules_capabilities(self):
        self.start()
        prompt = "[[tools]]" + directive("write_file", path="x.txt", content="no")
        code, report = self.chat(directive("schedule_create", prompt=prompt, every_seconds=3600,
                                           capabilities=["files.read"]))
        [schedule] = self.cli("schedules", "list")[1]["schedules"]
        sid = schedule["schedule_id"]
        for _ in range(2):
            self.cli("schedules", "run-now", sid)
            time.sleep(1.05)
        runs = self.cli("schedules", "runs", sid)[1]["runs"]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertIn("tools=list_files,read_file", run["result"]["response"])
            self.assertIn("write_file:403", run["result"]["response"])
        self.assertFalse((self.workspace / "x.txt").exists())
        receipts = self.cli("receipts")[1]["receipts"]
        self.assertTrue(all(r["state"] == "denied" for r in receipts if r["tool"] == "write_file"))
        connection = sqlite3.connect(self.home / "state" / "agent.sqlite3")
        try:
            grants = [json.loads(row[0]) | {"revoked": row[1]} for row in connection.execute(
                "SELECT binding_json, revoked FROM grants")]
        finally:
            connection.close()
        turns = {run["turn_id"] for run in runs}
        mine = [grant for grant in grants if grant["turn_id"] in turns]
        self.assertEqual(len(mine), 2)
        self.assertTrue(all(grant["capabilities"] == ["files.read"] and grant["revoked"]
                            for grant in mine))


class EvidenceTests(unittest.TestCase):
    @criteria("B11", "B12")
    def test_the_evidence_runner_traces_b1_to_b12_and_keeps_every_cell_v1_suite(self):
        import importlib

        import run_acceptance
        self.assertLessEqual({f"B{number}" for number in range(1, 13)},
                             set(run_acceptance.CRITERIA))
        self.assertEqual([run_acceptance.evidence_class(name) for name in run_acceptance.ALWAYS_ON],
                         ["unit", "unit", "real-core", "live"])
        for name in ("test_live", "test_real_core", "test_real_lifeline", "test_cell_host",
                     "test_cell_cli", "test_cell_shell", *run_acceptance.PREEXISTING):
            self.assertNotIn(name, run_acceptance.ALWAYS_ON)
            importlib.import_module(name)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.agents = private_dir(self)
        tools = private_dir(self)
        self.calls = tools / "calls.txt"
        self.launchctl = tools / "launchctl"
        self.launchctl.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        self.launchctl.chmod(0o700)
        self.env = isolated_env(self.home, private_dir(self),
                                BRAINSTEM_AGENT_LAUNCH_AGENTS=str(self.agents),
                                BRAINSTEM_AGENT_LAUNCHCTL=str(self.launchctl))
        real = os.path.expanduser("~/Library/LaunchAgents")
        self.assertNotEqual(os.path.realpath(self.env["BRAINSTEM_AGENT_LAUNCH_AGENTS"]),
                            os.path.realpath(real))

    def service(self, *arguments):
        result = run_cli(["service", *arguments, "--json"], self.env)
        return result.returncode, cli_json(result)

    @criteria("B10")
    def test_dry_run_renders_a_valid_launch_agent_for_this_home_and_touches_nothing(self):
        code, report = self.service("install", "--dry-run")
        self.assertEqual(code, 0, report)
        plist = private_dir(self) / "agent.plist"
        plist.write_text(report["plist"])
        lint = subprocess.run(["/usr/bin/plutil", "-lint", str(plist)], capture_output=True,
                              text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        document = plistlib.loads(report["plist"].encode())
        self.assertEqual(document["Label"], report["label"])
        self.assertEqual(document["ProgramArguments"][-3:], ["-m", "brainstem_agent", "serve"])
        self.assertEqual(document["EnvironmentVariables"]["BRAINSTEM_AGENT_HOME"],
                         str(self.home))
        self.assertTrue(document["RunAtLoad"])
        self.assertEqual(document["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(report["commands"][0][1:3], ["bootstrap", f"gui/{os.getuid()}"])
        code, undo = self.service("uninstall", "--dry-run")
        self.assertEqual((code, undo["path"]), (0, report["path"]))
        self.assertEqual(undo["commands"][0][1:], ["bootout", f"gui/{os.getuid()}",
                                                   report["path"]])
        self.assertEqual(list(self.agents.iterdir()), [])
        self.assertFalse(self.calls.exists())
        other = isolated_env(private_dir(self), private_dir(self),
                             BRAINSTEM_AGENT_LAUNCH_AGENTS=str(self.agents))
        other_label = cli_json(run_cli(["service", "install", "--dry-run", "--json"],
                                       other))["label"]
        self.assertNotEqual(other_label, report["label"])

    @criteria("B10")
    def test_install_and_uninstall_are_symmetric(self):
        code, installed = self.service("install")
        self.assertEqual(code, 0, installed)
        path = self.agents / (installed["label"] + ".plist")
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        code, removed = self.service("uninstall")
        self.assertEqual(code, 0, removed)
        self.assertFalse(path.exists())
        self.assertEqual(self.calls.read_text().splitlines(),
                         [f"bootstrap gui/{os.getuid()} {path}", f"bootout gui/{os.getuid()} {path}"])


if __name__ == "__main__":
    unittest.main()
