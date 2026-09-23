"""A8 unit specs for process-group lifecycles with real processes (no Grail).

Covers the orphan lifeline (a host that dies, even by SIGKILL, takes its
process groups with it), dead-host reaping on the next start (only processes
provably started by that host are killed) and macOS zombie semantics
(``killpg`` answers EPERM when only zombies remain).
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from acceptance_support import (
    RUNTIME, criteria, group_exists, kill_quietly, pid_running, private_dir, wait_until,
)
from brainstem_agent.grail import GrailSource
from brainstem_agent.worker import GrailWorker, WorkerConfig

try:
    from brainstem_agent import lifeline
except ImportError:  # the red run records the missing mechanism as failures, not an import error
    lifeline = None

# A host process: a Supervisor tracks one `sleep` process group, then the host waits to be killed.
HOST = r"""
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brainstem_agent.lifeline import Supervisor
home = Path(sys.argv[2])
supervisor = Supervisor(home)
tree = home / "workers" / "w1" / "g1"
tracked = supervisor.track("worker", tree)
tree.mkdir(parents=True, mode=0o700)
(tree / "marker").write_text("generation tree")
child = subprocess.Popen(["/bin/sleep", "120"], start_new_session=True)
tracked.started(child.pid, expect=("/bin/sleep",))
print(json.dumps({"pid": child.pid, "watchdog": supervisor.watchdog_pid, "host": supervisor.host_id}),
      flush=True)
time.sleep(300)
"""

# A session leader L whose group G keeps a zombie Z after L exits: Z's parent P lives in
# another group of the same session, so signalling G never reaches P and Z stays unreaped.
ZOMBIE_TREE = r"""
import os, sys, time
group = os.getpid()
read, write = os.pipe()
if os.fork() == 0:
    os.setpgid(0, 0)
    zombie = os.fork()
    if zombie == 0:
        os.setpgid(0, group)
        os._exit(0)
    os.write(write, f"{os.getpid()} {zombie}\n".encode())
    time.sleep(300)
    os._exit(0)
sys.stdout.write(os.read(read, 64).decode())
sys.stdout.flush()
time.sleep(300)
"""


def ps_state(pid):
    return subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                          text=True).stdout.strip()


class LifelineCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(lifeline, "brainstem_agent.lifeline (orphan lifeline) is missing")
        self.home = private_dir(self)

    def start_host(self):
        process = subprocess.Popen([sys.executable, "-c", HOST, str(RUNTIME), str(self.home)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.addCleanup(self.reap, process)
        line = process.stdout.readline()
        if not line:
            self.fail("host did not start: " + process.stderr.read()[-500:])
        info = json.loads(line)
        self.addCleanup(kill_quietly, info["pid"])
        if info.get("watchdog"):
            self.addCleanup(kill_quietly, info["watchdog"], group=False)
        return process, info

    @staticmethod
    def reap(process):
        kill_quietly(process.pid)
        process.wait(10)
        for stream in (process.stdout, process.stderr):
            stream.close()


class HostDeathTests(LifelineCase):
    @criteria("A8")
    def test_sigkilled_host_takes_its_process_groups_with_it_promptly(self):
        process, info = self.start_host()
        self.assertTrue(pid_running(info["pid"]))
        self.assertTrue(info["watchdog"] and pid_running(info["watchdog"]))
        killed = time.monotonic()
        process.kill()
        process.wait(10)
        self.assertTrue(wait_until(lambda: not group_exists(info["pid"]), 3.0),
                        "the tracked group outlived its SIGKILLed host")
        self.assertLess(time.monotonic() - killed, 3.0)
        self.assertTrue(wait_until(lambda: not pid_running(info["watchdog"]), 3.0))

    @criteria("A8")
    def test_next_start_reaps_leftovers_when_host_and_watchdog_both_died(self):
        process, info = self.start_host()
        tree = self.home / "workers" / "w1" / "g1"
        os.kill(info["watchdog"], signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not pid_running(info["watchdog"]), 3.0))
        process.kill()
        process.wait(10)
        time.sleep(0.3)
        self.assertTrue(pid_running(info["pid"]), "without a lifeline the orphan survives")
        self.assertTrue(tree.exists())
        supervisor = lifeline.Supervisor(self.home)
        self.addCleanup(supervisor.close)
        [entry] = [item for item in supervisor.recovered if item["pid"] == info["pid"]]
        self.assertEqual(entry["action"], "killed", entry)
        self.assertEqual(entry["host"], info["host"])
        self.assertTrue(entry["group_gone"], entry)
        self.assertTrue(wait_until(lambda: not group_exists(info["pid"]), 3.0))
        self.assertFalse(tree.exists(), "the dead host's generation tree must be removed")
        self.assertFalse((self.home / "run" / "hosts" / info["host"]).exists())

    @criteria("A8")
    def test_reaper_after_a_clean_watchdog_kill_removes_trees_and_records(self):
        process, info = self.start_host()
        process.kill()
        process.wait(10)
        self.assertTrue(wait_until(lambda: not group_exists(info["pid"]), 3.0))
        supervisor = lifeline.Supervisor(self.home)
        self.addCleanup(supervisor.close)
        [entry] = [item for item in supervisor.recovered if item["pid"] == info["pid"]]
        self.assertEqual(entry["action"], "already-gone", entry)
        self.assertFalse((self.home / "workers" / "w1" / "g1").exists())
        self.assertFalse((self.home / "run" / "hosts" / info["host"]).exists())

    @criteria("A8")
    def test_normal_close_stops_tracked_groups_and_leaves_nothing_behind(self):
        supervisor = lifeline.Supervisor(self.home)
        tree = self.home / "workers" / "w2" / "g2"
        tracked = supervisor.track("worker", tree)
        tree.mkdir(parents=True, mode=0o700)
        child = subprocess.Popen(["/bin/sleep", "120"], start_new_session=True)
        self.addCleanup(kill_quietly, child.pid)
        tracked.started(child.pid, expect=("/bin/sleep",))
        watchdog = supervisor.watchdog_pid
        supervisor.close()
        child.wait(5)
        self.assertFalse(group_exists(child.pid))
        self.assertTrue(wait_until(lambda: not pid_running(watchdog), 3.0))
        self.assertEqual(list((self.home / "run" / "hosts").iterdir()), [])


class ReaperIdentityTests(LifelineCase):
    def dead_host(self, records):
        """A host directory whose lease nobody holds (its host is dead)."""
        host = "h" + secrets.token_hex(6)
        directory = self.home / "run" / "hosts" / host
        directory.mkdir(parents=True, mode=0o700)
        (directory / "lease").write_text("")
        for index, record in enumerate(records):
            (directory / f"r{index}.json").write_text(json.dumps(record))
        return host, directory

    def stranger(self):
        process = subprocess.Popen(["/bin/sleep", "120"], start_new_session=True)
        self.addCleanup(self.reap_stranger, process)
        return process

    @staticmethod
    def reap_stranger(process):
        kill_quietly(process.pid)
        process.wait(10)

    @criteria("A6", "A8")
    def test_reaper_never_kills_a_process_it_cannot_prove_is_the_dead_hosts(self):
        stranger = self.stranger()
        tree = self.home / "workers" / "w3" / "g3"
        tree.mkdir(parents=True, mode=0o700)
        host, directory = self.dead_host([
            {"kind": "worker", "path": str(tree), "pid": stranger.pid, "pgid": stranger.pid,
             "spawned_at": time.time() - 3600, "expect": ["/bin/sleep"]},
        ])
        supervisor = lifeline.Supervisor(self.home)
        self.addCleanup(supervisor.close)
        [entry] = supervisor.recovered
        self.assertEqual(entry["action"], "not-ours", entry)
        self.assertTrue(pid_running(stranger.pid), "a reused PID must never be killed")
        self.assertFalse(tree.exists())
        self.assertFalse(directory.exists())

    @criteria("A6", "A8")
    def test_reaper_requires_the_recorded_program_too(self):
        stranger = self.stranger()
        self.dead_host([
            {"kind": "shell", "path": str(self.home / "run" / "shell" / "c1"), "pid": stranger.pid,
             "pgid": stranger.pid, "spawned_at": time.time(), "expect": ["/bin/sh"]},
        ])
        supervisor = lifeline.Supervisor(self.home)
        self.addCleanup(supervisor.close)
        [entry] = supervisor.recovered
        self.assertEqual(entry["action"], "not-ours", entry)
        self.assertTrue(pid_running(stranger.pid))

    @criteria("A8")
    def test_reaper_kills_a_proven_leftover_and_leaves_live_hosts_alone(self):
        leftover = self.stranger()
        self.dead_host([
            {"kind": "worker", "path": str(self.home / "workers" / "w4" / "g4"),
             "pid": leftover.pid, "pgid": leftover.pid, "spawned_at": time.time(),
             "expect": ["/bin/sleep"]},
        ])
        alive = lifeline.Supervisor(self.home)
        self.addCleanup(alive.close)
        guarded = self.stranger()
        tracked = alive.track("worker", self.home / "workers" / "w5" / "g5")
        tracked.started(guarded.pid, expect=("/bin/sleep",))
        self.addCleanup(tracked.finish)
        [entry] = alive.recovered
        self.assertEqual(entry["action"], "killed", entry)
        leftover.wait(5)
        self.assertFalse(group_exists(leftover.pid))
        second = lifeline.Supervisor(self.home)
        self.addCleanup(second.close)
        self.assertEqual(second.recovered, [], "a live host's records are never reaped")
        self.assertTrue(pid_running(guarded.pid))

    @criteria("A8")
    def test_reaper_treats_a_zombie_only_group_as_not_running(self):
        leader = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        self.addCleanup(self.reap_stranger, leader)
        self.assertTrue(wait_until(lambda: ps_state(leader.pid).startswith("Z"), 5.0))
        self.dead_host([
            {"kind": "shell", "path": str(self.home / "run" / "shell" / "c2"), "pid": leader.pid,
             "pgid": leader.pid, "spawned_at": time.time(), "expect": ["/bin/sh"]},
        ])
        supervisor = lifeline.Supervisor(self.home)
        self.addCleanup(supervisor.close)
        [entry] = supervisor.recovered
        self.assertIn(entry["action"], ("already-gone", "zombies"), entry)


class ZombieGroupTests(unittest.TestCase):
    """macOS killpg returns EPERM when every member of the group is a zombie."""

    def zombie_tree(self):
        leader = subprocess.Popen([sys.executable, "-c", ZOMBIE_TREE], stdout=subprocess.PIPE,
                                  text=True, start_new_session=True)
        holder, zombie = map(int, leader.stdout.readline().split())
        self.addCleanup(self.cleanup_tree, leader, holder)
        self.assertTrue(wait_until(lambda: ps_state(zombie).startswith("Z"), 5.0))
        return leader, holder, zombie

    @staticmethod
    def cleanup_tree(leader, holder):
        kill_quietly(holder)
        kill_quietly(leader.pid)
        leader.wait(10)
        leader.stdout.close()

    @staticmethod
    def worker_for(process):
        config = WorkerConfig(worker_id="zombie", home=Path("/nonexistent"),
                              source=GrailSource("0" * 40, Path("/nonexistent"), {}),
                              python=Path(sys.executable), broker_url="http://127.0.0.1:9")
        worker = GrailWorker(config, register=lambda *_: "key")
        worker._process = process
        worker.pid = worker.pgid = process.pid
        return worker

    @criteria("A8")
    def test_worker_stop_survives_eperm_from_a_zombie_only_group(self):
        leader, holder, zombie = self.zombie_tree()
        worker = self.worker_for(leader)
        started = time.monotonic()
        stopped = worker.stop(grace=0.5, timeout=1.5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(stopped["exit_code"])
        self.assertFalse(pid_running(leader.pid))
        self.assertEqual(stopped["group_state"], "zombies", stopped)
        self.assertFalse(stopped["group_gone"], "a group with a zombie member is not gone yet")
        kill_quietly(holder)
        self.assertTrue(wait_until(lambda: not group_exists(leader.pid), 5.0),
                        "once the zombie is reaped the group must really be gone")
        self.assertEqual(lifeline.group_state(leader.pid), "gone")

    @criteria("A8")
    def test_group_state_distinguishes_alive_zombies_and_gone(self):
        self.assertIsNotNone(lifeline, "brainstem_agent.lifeline is missing")
        leader = subprocess.Popen(["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE,
                                  start_new_session=True)
        self.assertEqual(lifeline.group_state(leader.pid), "alive")
        leader.stdin.close()
        self.assertTrue(wait_until(lambda: ps_state(leader.pid).startswith("Z"), 5.0))
        self.assertEqual(lifeline.group_state(leader.pid), "zombies")
        leader.wait(5)
        self.assertEqual(lifeline.group_state(leader.pid), "gone")

    @criteria("A8")
    def test_stop_group_reaps_our_zombie_leader_and_confirms_the_group_is_gone(self):
        self.assertIsNotNone(lifeline, "brainstem_agent.lifeline is missing")
        leader = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & exit 0"], start_new_session=True)
        self.addCleanup(kill_quietly, leader.pid)
        self.assertTrue(wait_until(lambda: ps_state(leader.pid).startswith("Z"), 5.0))
        self.assertTrue(group_exists(leader.pid))
        result = lifeline.stop_group(leader.pid, process=leader, grace=0.2, timeout=3.0)
        self.assertEqual(result["group_state"], "gone", result)
        self.assertTrue(result["group_gone"])
        self.assertFalse(group_exists(leader.pid))


if __name__ == "__main__":
    unittest.main()
