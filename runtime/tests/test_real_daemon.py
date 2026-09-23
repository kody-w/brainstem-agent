"""Real-core B1/B2/B6/B8 specs: the daemon with the unchanged Grail process, no inference.

Skips unless BRAINSTEM_AGENT_REAL_CORE=1. The warm worker starts with the installed
brainstem's credential (Grail checks it and fetches its model catalog; no chat turn
is sent), exactly like ``doctor --deep``.
"""

import json
import os
import signal
import time
import unittest

from acceptance_support import (REAL_CORE, base_env, cli_json, criteria, group_exists, leaks,
                                pid_running, prepared_cache, private_dir, real_credential_needles,
                                record_metric, run_cli, wait_until)


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start the real Grail process")
class RealDaemonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.outputs = []
        self.env = base_env(self.home, BRAINSTEM_AGENT_CACHE=str(self.cache))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        from brainstem_agent import daemon

        record = daemon.read_record(self.home)
        if record is not None:
            run_cli(["stop", "--json"], self.env, timeout=60)
            if pid_running(record["pid"]):
                os.kill(record["pid"], signal.SIGKILL)
        self.assertEqual(leaks(real_credential_needles(), roots=[self.home], texts=self.outputs),
                         [])

    def cli(self, *arguments, env=None, timeout=120):
        result = run_cli([*arguments, "--json"], env or self.env, timeout=timeout)
        self.outputs += [result.stdout, result.stderr]
        return result.returncode, cli_json(result)

    def serve(self, env=None):
        code, started = self.cli("serve", "--detach", "--workspace", str(self.workspace), env=env)
        self.assertEqual(code, 0, started)
        return started["pid"]

    def warm_status(self, timeout=120):
        seen = {}

        def warm():
            seen["status"] = self.cli("status")[1]
            workers = seen["status"].get("workers") or []
            return bool(workers) and workers[0]["warm"]
        self.assertTrue(wait_until(warm, timeout, 0.25), seen.get("status"))
        return seen["status"]

    @criteria("B1", "B2")
    def test_detached_daemon_keeps_a_verified_warm_grail_worker_and_stops_it_cleanly(self):
        started = time.monotonic()
        pid = self.serve()
        status = self.warm_status()
        record_metric("daemon_ready_warm_seconds", round(time.monotonic() - started, 2))
        [worker] = status["workers"]
        self.assertEqual((status["health"], status["pid"]), ("ok", pid))
        self.assertTrue(worker["alive"] and worker["pgid"])
        self.assertTrue(worker["integrity"]["ok"])
        record_metric("daemon_worker_start_seconds", worker["start_seconds"])
        # The loop re-verifies the warm copy against the pinned inventory while idle.
        self.assertTrue(wait_until(lambda: "seconds" in self.cli("status")[1]["workers"][0][
            "integrity"], 45, 0.5))
        code, refused = self.cli("serve", "--workspace", str(self.workspace))
        self.assertEqual(code, 1)
        self.assertIn("already running", refused["error"])
        code, stopped = self.cli("stop")
        self.assertEqual(code, 0, stopped)
        self.assertTrue(stopped["workers_gone"])
        self.assertEqual(stopped["workers"], [{"pgid": worker["pgid"], "group_state": "gone"}])
        self.assertFalse(group_exists(worker["pgid"]))
        self.assertTrue(wait_until(lambda: not pid_running(pid), 10))
        record_metric("daemon_stop_seconds", stopped["seconds"])

    @criteria("B1")
    def test_status_calls_a_real_worker_starting_until_its_start_completed(self):
        from brainstem_agent import daemon

        self.serve()
        client, seen = daemon.connect(self.home), []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            workers = client.call("GET", "/v1/status", timeout=10)["workers"]
            if workers:
                seen.append((workers[0]["state"], workers[0]["warm"], workers[0]["pgid"]))
                if workers[0]["warm"]:
                    break
            time.sleep(0.02)
        states = [(state, warm) for state, warm, _pgid in seen]
        record_metric("real_status_samples_while_starting", states.count(("starting", False)))
        self.assertEqual(states[-1], ("warm", True), seen[-5:])
        self.assertIn(("starting", False), states)  # a real start takes most of a second
        self.assertEqual({state for state, warm in states if warm}, {"warm"})
        self.assertEqual(self.cli("stop")[0], 0)

    @criteria("B2")
    def test_a_tampered_warm_worker_is_replaced_before_it_can_be_reused(self):
        self.serve()
        [worker] = self.warm_status()["workers"]
        soul = (self.home / "workers" / worker["worker_id"] / worker["generation"] /
                "rapp_brainstem" / "soul.md")
        os.chmod(soul, 0o644)
        soul.write_text("tampered from outside")
        # Any wake-up re-verifies the idle warm copy; a schedule change wakes the loop.
        self.cli("schedules", "create", "--workspace", str(self.workspace), "--in", "3600",
                 "--prompt", "Nothing yet.")
        seen = {}

        def replaced():
            seen["workers"] = self.cli("status")[1]["workers"]
            return (bool(seen["workers"]) and seen["workers"][0]["generation"]
                    != worker["generation"] and seen["workers"][0]["warm"])
        self.assertTrue(wait_until(replaced, 120, 0.25), seen.get("workers"))
        self.assertFalse(group_exists(worker["pgid"]))
        self.assertFalse(soul.exists())
        self.assertEqual(self.cli("stop")[0], 0)

    @criteria("B1", "B8")
    def test_sigkill_of_the_daemon_takes_its_grail_worker_with_it(self):
        pid = self.serve()
        [worker] = self.warm_status()["workers"]
        os.kill(pid, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not group_exists(worker["pgid"]), 5),
                        "the Grail worker outlived its SIGKILLed daemon")
        self.serve()
        self.assertEqual(self.warm_status()["health"], "ok")
        self.assertEqual(self.cli("stop")[0], 0)

    @criteria("B6")
    def test_a_real_restart_applies_the_missed_run_policy_without_inference(self):
        empty = private_dir(self)
        offline = base_env(self.home, BRAINSTEM_AGENT_CACHE=str(self.cache),
                           BRAINSTEM_HOME=str(empty))  # no credential: nothing can reach Copilot
        self.serve(offline)
        arguments = ("schedules", "create", "--workspace", str(self.workspace), "--in", "6",
                     "--capabilities", "files.read", "--prompt", "List the workspace.")
        skip = self.cli(*arguments, "--missed", "skip", env=offline)[1]["schedule"]
        late = self.cli(*arguments, env=offline)[1]["schedule"]
        self.assertEqual(self.cli("stop", env=offline)[0], 0)
        self.assertTrue(wait_until(lambda: time.time() > late["next_fire_at"] + 2, 15))
        self.serve(offline)
        runs = {}

        def finished():
            for item in (skip, late):
                found = self.cli("schedules", "runs", item["schedule_id"], "--workspace",
                                 str(self.workspace), env=offline)[1]["runs"]
                runs[item["schedule_id"]] = found
            return all(found and found[0]["state"] != "running" for found in runs.values())
        self.assertTrue(wait_until(finished, 60, 0.5), runs)
        [skipped], [ran] = runs[skip["schedule_id"]], runs[late["schedule_id"]]
        self.assertEqual((skipped["state"], skipped["reason"]), ("skipped", "missed"))
        # Run once, marked late; without a credential that one run fails explicitly.
        self.assertEqual(ran["state"], "failed")
        self.assertGreater(ran["late_seconds"], 1.5)
        self.assertIn("credential", ran["result"]["error"].lower())
        self.assertEqual(self.cli("stop", env=offline)[0], 0)
        schedules = self.cli("schedules", "list", "--all", "--workspace", str(self.workspace),
                             env=offline)[1]["schedules"]
        self.assertEqual({item["state"] for item in schedules}, {"completed"})
        self.assertEqual(json.dumps(runs).count("occ_"), 2)


if __name__ == "__main__":
    unittest.main()
