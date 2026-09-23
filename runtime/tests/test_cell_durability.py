"""D9 unit specs: crash injection at every boundary of a long turn.

A real host process runs a long-turn scenario (``longturn_support.py``: the real cell, a
Grail-faithful fake worker, real sandboxed shell commands, scripts and processes) and is
SIGKILLed at a journaled boundary (``BRAINSTEM_AGENT_CRASH_AT``) or while a tool runs.
Then the next host proves the outcomes:

- never false success: the turn is ``uncertain`` when any tool of it, its segments or
  its helpers had started, ``failed`` when provably none had;
- completed effects stay completed and are never repeated (replay does not redispatch,
  receipts that finished stay finished, the effect log shows each effect once);
- whatever was running is settled (segments, helpers, inner calls, processes);
- orphans are cleaned up within a bound (the lifeline watchdog kills every recorded
  process group of the dead host; the reaper removes their trees);
- the next command works.
"""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from acceptance_support import (RUNTIME, criteria, group_exists, kill_quietly, pid_running,
                                private_dir, record_metric, wait_until, write_token_file)
from brainstem_agent import sandbox
from brainstem_agent.host import AgentHost
from longturn_support import factory_for, recorded_groups, scripted_policy

HERE = Path(__file__).resolve().parent
HAS_SANDBOX = sandbox.available()


class CrashCase(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}

    def crash(self, scenario, *, at=None, after=None, key="crash-key"):
        env = {**self.environ, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
               "PYTHONPATH": os.pathsep.join([str(RUNTIME), str(HERE)])}
        if at:
            env["BRAINSTEM_AGENT_CRASH_AT"] = at
        if after:
            env["LONGTURN_KILL_AFTER"] = str(after)
        process = subprocess.Popen(
            [sys.executable, str(HERE / "longturn_support.py"), str(self.home),
             str(self.workspace), scenario, key], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            kill_quietly(process.pid)
            stdout, stderr = process.communicate()
            self.fail("the scenario never crashed: " + stderr[-400:])
        self.assertEqual(process.returncode, -signal.SIGKILL,
                         f"expected a SIGKILL crash: {stdout[-300:]} {stderr[-600:]}")
        ready = json.loads(stdout.splitlines()[0])
        if ready.get("watchdog"):
            self.addCleanup(kill_quietly, ready["watchdog"], group=False)
        # Bounded orphan cleanup: every group the dead host recorded is gone within 5 s.
        groups = recorded_groups(self.home)
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: not any(group_exists(pid) for _k, pid in groups), 5),
                        f"outlived their host: {[g for g in groups if group_exists(g[1])]}")
        if groups:
            record_metric(f"d9_orphans_gone_seconds_{scenario}",
                          round(time.monotonic() - started, 3))
        self.groups = groups
        return ready

    def next_host(self, policy=None):
        host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                         worker_factory=factory_for(policy or scripted_policy([], "fresh")))
        self.addCleanup(host.close)
        with host.exclusive():  # recovery runs here, as for every mutating command
            pass
        return host

    def turn_of(self, host):
        [journal] = host.store.list_journals(host.namespace)
        return host.store.journal(host.namespace, journal["turn_id"])

    def assert_next_command_works(self, host, key="crash-key"):
        replay = host.chat("do it again", idempotency_key=key)
        self.assertTrue(replay.replayed)
        self.assertFalse(replay.ok)
        self.assertEqual(replay.evidence["grail_calls"], 0, "never redispatched")
        fresh = host.chat("hello after the crash")
        self.assertTrue(fresh.ok, fresh.error)
        self.assertEqual(fresh.response["response"], "fresh")

    def assert_no_leftovers(self):
        for parts in (("run", "shell"), ("run", "scripts"), ("run", "processes"), ("workers",)):
            root = self.home.joinpath(*parts)
            leftovers = list(root.iterdir()) if root.exists() else []
            self.assertEqual(leftovers, [], f"{'/'.join(parts)} still holds {leftovers}")


class BoundaryTests(CrashCase):
    @criteria("D9")
    def test_crash_after_a_segment_is_journaled_but_before_it_is_sent_is_failed(self):
        self.crash("chain", at="segment.started#1")
        host = self.next_host()
        journal = self.turn_of(host)
        self.assertEqual([(s["kind"], s["state"]) for s in journal["steps"]],
                         [("turn", "failed"), ("segment", "failed")])
        self.assertEqual(journal["receipts"], [])
        self.assertEqual(host.store.get_chat(host.namespace, journal["turn_id"]).state, "failed")
        self.assertFalse((self.workspace / "chain").exists())
        self.assert_next_command_works(host)

    @criteria("D9")
    def test_crash_between_segments_keeps_the_completed_segment_and_is_uncertain(self):
        self.crash("chain", at="segment.finished#1")
        host = self.next_host()
        journal = self.turn_of(host)
        self.assertEqual([(s["kind"], s["seq"], s["state"]) for s in journal["steps"]],
                         [("turn", 0, "uncertain"), ("segment", 1, "succeeded")])
        self.assertEqual([(r["tool"], r["state"]) for r in journal["receipts"]],
                         [("write_file", "succeeded"), ("read_file", "succeeded"),
                          ("write_file", "succeeded")])
        self.assertEqual(host.store.get_chat(host.namespace, journal["turn_id"]).state,
                         "uncertain")
        self.assertEqual(sorted(p.name for p in (self.workspace / "chain").iterdir()),
                         ["1.txt", "2.txt"])
        self.assert_next_command_works(host)
        self.assertFalse((self.workspace / "chain" / "3.txt").exists(), "not continued")

    @criteria("D9")
    def test_crash_after_the_next_segment_is_journaled_marks_only_that_one_failed(self):
        self.crash("chain", at="segment.started#2")
        host = self.next_host()
        journal = self.turn_of(host)
        self.assertEqual([(s["kind"], s["seq"], s["state"]) for s in journal["steps"]],
                         [("turn", 0, "uncertain"), ("segment", 1, "succeeded"),
                          ("segment", 2, "failed")])
        self.assert_next_command_works(host)

    @criteria("D9")
    def test_crash_while_finishing_a_successful_turn_is_never_success(self):
        self.crash("chain", at="turn.finishing")
        host = self.next_host()
        journal = self.turn_of(host)
        states = [(s["kind"], s["state"]) for s in journal["steps"]]
        self.assertEqual(states[0], ("turn", "uncertain"))
        self.assertEqual(states[1:], [("segment", "succeeded")] * 4)
        self.assertEqual(len(journal["receipts"]), 11)
        self.assertEqual((self.workspace / "chain" / "6.txt").read_text(), "6")
        self.assert_next_command_works(host)


@unittest.skipUnless(HAS_SANDBOX, "needs /usr/bin/sandbox-exec")
class RunningWorkTests(CrashCase):
    @criteria("D9")
    def test_crash_while_a_tool_runs_leaves_it_uncertain_and_kills_its_group(self):
        self.crash("mid_tool", after=2.0)
        pid = int((self.workspace / "shell.pid").read_text().strip())
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: not pid_running(pid), 5), "the shell outlived its host")
        record_metric("d9_orphan_shell_seconds", round(time.monotonic() - started, 3))
        host = self.next_host()
        journal = self.turn_of(host)
        self.assertEqual([(s["kind"], s["state"]) for s in journal["steps"]],
                         [("turn", "uncertain"), ("segment", "uncertain")])
        self.assertEqual([(r["tool"], r["state"]) for r in journal["receipts"]],
                         [("write_file", "succeeded"), ("run_command", "uncertain")])
        self.assert_no_leftovers()
        self.assert_next_command_works(host)

    @criteria("D9", "D5")
    def test_crash_while_helpers_run_settles_each_helper_and_kills_their_work(self):
        self.crash("children", after=2.5)
        self.assertEqual(sorted(kind for kind, _pid in self.groups), ["shell", "shell"])
        host = self.next_host()
        journal = self.turn_of(host)
        kinds = [(s["kind"], s["state"]) for s in journal["steps"]]
        self.assertEqual(kinds.count(("child", "uncertain")), 2, kinds)
        self.assertIn(("turn", "uncertain"), kinds)
        self.assertEqual([s["state"] for s in journal["steps"] if s["kind"] == "segment"],
                         ["uncertain"] * 3, "the parent's and both helpers' segments")
        tools = sorted((r["tool"], r["state"]) for r in journal["receipts"])
        self.assertEqual(tools, [("delegate_tasks", "uncertain"),
                                 ("run_command", "uncertain"), ("run_command", "uncertain"),
                                 ("write_file", "succeeded"), ("write_file", "succeeded")])
        self.assertEqual((self.workspace / "par" / "a.txt").read_text(), "a")
        self.assert_no_leftovers()
        self.assert_next_command_works(host)

    @criteria("D9", "D6")
    def test_crash_mid_script_keeps_finished_inner_calls_and_never_repeats_them(self):
        self.crash("script", at="script.inner#2")
        self.assertEqual([kind for kind, _pid in self.groups], ["script"])
        host = self.next_host()
        journal = self.turn_of(host)
        receipts = [(r["tool"], r["state"], r["call_id"].count(".")) for r in journal["receipts"]]
        self.assertEqual(receipts, [("run_script", "uncertain", 0),
                                    ("write_file", "succeeded", 1),
                                    ("write_file", "succeeded", 1)])
        self.assertEqual(sorted(p.name for p in (self.workspace / "script").iterdir()),
                         ["1.txt", "2.txt"])
        self.assert_no_leftovers()
        self.assert_next_command_works(host)
        self.assertEqual(sorted(p.name for p in (self.workspace / "script").iterdir()),
                         ["1.txt", "2.txt"], "nothing re-ran")

    @criteria("D9", "D7")
    def test_crash_with_a_background_process_running_marks_it_lost_and_kills_it(self):
        self.crash("process", after=2.0)
        host0 = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                          worker_factory=factory_for(scripted_policy([], "fresh")))
        [record] = host0.store.list_processes(host0.namespace)
        host0.close()
        self.assertTrue(wait_until(lambda: not group_exists(record["pid"]), 5),
                        "the background process outlived its host")
        host = self.next_host()
        self.assertEqual(host.store.get_process(host.namespace, record["process_id"])["state"],
                         "lost")
        journal = self.turn_of(host)
        self.assertEqual(journal["steps"][0]["state"], "uncertain")
        self.assert_no_leftovers()
        self.assert_next_command_works(host)

    @criteria("D9")
    def test_an_in_process_turn_killed_mid_way_can_be_inspected_with_turns_show(self):
        from acceptance_support import cli_json, run_cli

        self.crash("chain", at="segment.finished#2")
        env = {**self.environ, "PATH": "/usr/bin:/bin", "PYTHONPATH": str(RUNTIME),
               "BRAINSTEM_AGENT_HOME": str(self.home)}
        listed = cli_json(run_cli(["turns", "list", "--workspace", str(self.workspace),
                                   "--json"], env))
        [turn] = listed["turns"]
        shown = cli_json(run_cli(["turns", "show", turn["turn_id"], "--workspace",
                                  str(self.workspace), "--json"], env))
        # A query command does not take the lock, so it shows the journal as left.
        self.assertEqual([s["state"] for s in shown["steps"]],
                         ["running", "succeeded", "succeeded"])
        self.assertEqual(len(shown["receipts"]), 6)


class InProcessFailureTests(CrashCase):
    """Deaths the host survives: its Grail worker, a helper's worker, a script."""

    def host(self, policy, worker_class=None):
        from longturn_support import GrailEmulator
        workers = []

        def factory(**options):
            worker = (worker_class or GrailEmulator)(policy, **options)
            workers.append(worker)
            return worker
        host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                         worker_factory=factory)
        self.addCleanup(host.close)
        return host, workers

    @criteria("D9")
    def test_a_worker_dying_in_a_continuation_ends_the_turn_uncertain_and_never_continues(self):
        from longturn_support import GrailEmulator, chain_policy

        class Dying(GrailEmulator):
            def _loop(self, worker, request, grant):
                for index, chunk in enumerate(super()._loop(worker, request, grant)):
                    yield chunk
                    if "continuation: step 2" in request["user_input"] and index == 1:
                        raise ConnectionResetError("Grail died")
        host, workers = self.host(chain_policy(), Dying)
        result = host.chat("chain")
        self.assertEqual(result.state, "uncertain", result.error)
        journal = host.store.journal(host.namespace, result.turn_id)
        self.assertEqual([(s["kind"], s["state"]) for s in journal["steps"]],
                         [("turn", "uncertain"), ("segment", "succeeded"),
                          ("segment", "uncertain")])
        self.assertTrue(all(r["state"] != "started" for r in journal["receipts"]))
        self.assertFalse((self.workspace / "chain" / "4.txt").exists())
        again = host.chat("hello")  # a fresh worker for the next turn
        self.assertNotEqual(workers[-1], workers[0])

    @criteria("D9", "D6")
    def test_a_script_that_dies_keeps_its_finished_inner_calls_and_the_turn_goes_on(self):
        if not HAS_SANDBOX:
            self.skipTest("needs /usr/bin/sandbox-exec")
        code = ("import os, signal\nwrite_text('s/1.txt', 1)\nwrite_text('s/2.txt', 2)\n"
                "os.kill(os.getpid(), signal.SIGKILL)\nwrite_text('s/3.txt', 3)\n")
        host, _ = self.host(scripted_policy([[("run_script", {"code": code})],
                                             [("list_files", {"path": "s"})]], "Two files."))
        result = host.chat("script")
        self.assertTrue(result.ok, result.error)
        receipts = [(r["tool"], r["state"]) for r in host.receipts(result.turn_id)]
        self.assertEqual(receipts, [("run_script", "failed"), ("write_file", "succeeded"),
                                    ("write_file", "succeeded"), ("list_files", "succeeded")])
        self.assertEqual(sorted(p.name for p in (self.workspace / "s").iterdir()),
                         ["1.txt", "2.txt"])


class DaemonCrashTests(CrashCase):
    @criteria("D9")
    def test_sigkill_of_the_daemon_mid_long_turn_is_uncertain_and_the_next_command_works(self):
        env = {**self.environ, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
               "PYTHONPATH": os.pathsep.join([str(RUNTIME), str(HERE)]),
               "BRAINSTEM_AGENT_HOME": str(self.home),
               "BRAINSTEM_AGENT_CRASH_AT": "segment.finished#2"}
        daemon = subprocess.Popen(
            [sys.executable, str(HERE / "longturn_support.py"), str(self.home),
             str(self.workspace), "chain", "--daemon"], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(kill_quietly, daemon.pid)
        from brainstem_agent import daemon as daemon_module
        self.assertTrue(wait_until(lambda: daemon_module.read_record(self.home) is not None
                                   or daemon.poll() is not None, 30))
        chat = subprocess.run(
            [sys.executable, "-m", "brainstem_agent", "chat", "chain please", "--json",
             "--workspace", str(self.workspace), "--idempotency-key", "crash-key"],
            env={key: value for key, value in env.items() if key != "BRAINSTEM_AGENT_CRASH_AT"},
            capture_output=True, text=True, timeout=90)
        daemon.wait(30)
        self.assertEqual(daemon.returncode, -signal.SIGKILL, daemon.stderr.read()[-500:])
        daemon.stdout.close()
        daemon.stderr.close()
        self.assertNotEqual(chat.returncode, 0, "a turn whose daemon died is never success")
        host = self.next_host()
        journal = self.turn_of(host)
        self.assertEqual([(s["kind"], s["state"]) for s in journal["steps"]],
                         [("turn", "uncertain"), ("segment", "succeeded"),
                          ("segment", "succeeded")])
        self.assert_next_command_works(host)


if __name__ == "__main__":
    unittest.main()
