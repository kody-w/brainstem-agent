"""Real-core A8 specs: an unchanged Grail worker never outlives its host.

Skips unless BRAINSTEM_AGENT_REAL_CORE=1. A host process starts a real Grail
worker (no credential, so no inference) and is then SIGKILLed. The worker's
process group must exit on its own within seconds, and the next host start must
reap whatever the dead host recorded, killing only processes it can prove
belong to that host.
"""

import json
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

from acceptance_support import (
    REAL_CORE, RUNTIME, criteria, group_exists, kill_quietly, pid_running, prepared_cache,
    private_dir, record_metric, wait_until,
)

HOST = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brainstem_agent.host import AgentHost
home, workspace, cache, brainstem = map(Path, sys.argv[2:6])
host = AgentHost(home, workspace=workspace, cache=cache,
                 environ={"HOME": str(brainstem), "BRAINSTEM_HOME": str(brainstem)})
worker = host._grail_worker(worker_id="orphan", broker_url=host.broker.url, credential=None,
                            model="auto", register=host.broker.register_worker)
worker.start()
supervisor = getattr(host, "supervisor", None)
print(json.dumps({"pid": worker.pid, "tree": str(worker.tree),
                  "watchdog": getattr(supervisor, "watchdog_pid", None),
                  "host": getattr(supervisor, "host_id", None)}), flush=True)
time.sleep(600)
"""


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start the real Grail process")
class RealLifelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.brainstem = private_dir(self)

    def start_host(self):
        process = subprocess.Popen(
            [sys.executable, "-c", HOST, str(RUNTIME), str(self.home), str(self.workspace),
             str(self.cache), str(self.brainstem)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.reap, process)
        line = process.stdout.readline()
        if not line:
            self.fail("the host never started a worker: " + process.stderr.read()[-600:])
        info = json.loads(line)
        self.addCleanup(kill_quietly, info["pid"])
        if info.get("watchdog"):
            self.addCleanup(kill_quietly, info["watchdog"], group=False)
        self.assertTrue(pid_running(info["pid"]))
        return process, info

    @staticmethod
    def reap(process):
        kill_quietly(process.pid)
        process.wait(10)
        process.stdout.close()
        process.stderr.close()

    def next_host(self):
        from brainstem_agent.host import AgentHost

        host = AgentHost(self.home, workspace=self.workspace, cache=self.cache,
                         environ={"HOME": str(self.brainstem), "BRAINSTEM_HOME": str(self.brainstem)})
        self.addCleanup(host.close)
        return host

    @criteria("A8")
    def test_grail_worker_exits_on_its_own_when_its_host_is_sigkilled(self):
        process, info = self.start_host()
        import time

        killed = time.monotonic()
        process.kill()
        process.wait(10)
        gone = wait_until(lambda: not group_exists(info["pid"]), 5.0)
        record_metric("orphan_worker_exit_seconds", round(time.monotonic() - killed, 3))
        self.assertTrue(gone, "the Grail worker outlived its SIGKILLed host")
        self.assertLess(time.monotonic() - killed, 5.0)
        host = self.next_host()
        [entry] = [item for item in getattr(host, "recovered", []) if item["pid"] == info["pid"]]
        self.assertEqual(entry["action"], "already-gone", entry)
        self.assertFalse(Path(info["tree"]).exists(), "the dead host's worker tree was left behind")

    @criteria("A8")
    def test_next_host_start_reaps_a_worker_whose_host_and_lifeline_died(self):
        process, info = self.start_host()
        self.assertTrue(info["watchdog"], "the host has no lifeline watchdog")
        os.kill(info["watchdog"], signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not pid_running(info["watchdog"]), 3.0))
        process.kill()
        process.wait(10)
        self.assertTrue(pid_running(info["pid"]), "precondition: the orphan survived")
        host = self.next_host()
        [entry] = [item for item in host.recovered if item["pid"] == info["pid"]]
        self.assertEqual(entry["action"], "killed", entry)
        self.assertTrue(entry["group_gone"], entry)
        self.assertTrue(wait_until(lambda: not group_exists(info["pid"]), 3.0))
        self.assertFalse(Path(info["tree"]).exists())


if __name__ == "__main__":
    unittest.main()
