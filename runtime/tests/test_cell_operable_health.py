"""H7 unit specs: liveness versus readiness, with reasons and fixes.

``doctor`` explains every failing check; ``status`` without a daemon is not live and says how to
start it; the daemon's ``GET /v1/health`` (also inside ``status --json``) reports live (process
up, schedule loop running) separately from ready (Grail verified, credential usable, worker
warm, sandbox available, MCP servers healthy, disk space, not draining).
"""

import hashlib
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

from acceptance_support import (HAVE_SEED, NO_SEED, SEED, cli_json, criteria, isolated_env,
                                private_dir, run_cli, wait_until, write_token_file)
from brainstem_agent import daemon, grail, health
from daemon_support import ScriptedWorker, daemon_env, spawn_fake_daemon

REQUIRED_FIELDS = {"id", "kind", "ok", "required", "reason", "fix"}


def prepared_cache(root: Path) -> Path:
    """A verified Grail cache (from the seed) and a worker-venv marker (the fake Grail never
    runs the interpreter, so only its readiness marker is needed)."""
    cache = root / "cache"
    grail.ensure_grail_source(cache, seed_dir=SEED, fetch=False)
    lock = hashlib.sha256(grail.LOCK_FILE.read_bytes()).hexdigest()
    venv = cache / "venvs" / f"{lock[:16]}-py{sys.version_info[0]}{sys.version_info[1]}"
    (venv / "bin").mkdir(parents=True)
    os.symlink(sys.executable, venv / "bin" / "python")
    (venv / ".brainstem-agent-venv.json").write_text(json.dumps({"lock_sha256": lock}))
    return cache


class DoctorTests(unittest.TestCase):
    @criteria("H7")
    def test_doctor_explains_each_failing_check_with_a_reason_and_a_fix(self):
        home, scratch = private_dir(self), private_dir(self)
        env = isolated_env(home, scratch, BRAINSTEM_AGENT_SANDBOX_EXEC="/nonexistent/sandbox",
                           BRAINSTEM_AGENT_MIN_FREE_MB="100000000")
        result = run_cli(["doctor", "--json"], env)
        self.assertEqual(result.returncode, 1)
        report = cli_json(result)
        self.assertFalse(report["health"]["ready"])
        failing = set(report["health"]["failing"])
        self.assertEqual(failing, {"grail_source", "worker_interpreter", "credential", "sandbox",
                                   "disk_space"})
        for check in report["health"]["checks"]:
            self.assertEqual(REQUIRED_FIELDS - set(check), set(), check)
            if not check["ok"]:
                self.assertTrue(check["reason"] and check["fix"], check)
        human = run_cli(["doctor"], env)
        text = human.stdout + human.stderr
        self.assertIn("not ready", text)
        self.assertEqual(text.count("fix: "), len(failing))

    @criteria("H7")
    def test_status_without_a_daemon_is_not_live_and_says_how_to_start_it(self):
        home, scratch = private_dir(self), private_dir(self)
        result = run_cli(["status", "--json"], isolated_env(home, scratch))
        self.assertEqual(result.returncode, 1)
        readiness = cli_json(result)["readiness"]
        self.assertFalse(readiness["live"])
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["state"], "down")
        daemon_check = next(item for item in readiness["checks"] if item["id"] == "daemon")
        self.assertEqual(daemon_check["kind"], "liveness")
        self.assertIn("serve --detach", daemon_check["fix"])


@unittest.skipUnless(HAVE_SEED, NO_SEED)
class DaemonHealthTests(unittest.TestCase):
    def setUp(self):
        self.scratch, self.home, self.workspace = (private_dir(self), private_dir(self),
                                                   private_dir(self))
        self.cache = prepared_cache(self.scratch)
        self.token = write_token_file(self.scratch)
        self.env = daemon_env(isolated_env(self.home, self.scratch,
                                           BRAINSTEM_AGENT_CACHE=str(self.cache)),
                              self.token, self.scratch)

    @criteria("H7")
    def test_the_health_route_separates_live_from_ready_and_explains_both(self):
        process = spawn_fake_daemon(self.home, self.workspace, self.env)
        self.addCleanup(lambda: (run_cli(["stop", "--json"], self.env, timeout=60),
                                 process.communicate(timeout=30)))
        client = daemon.connect(self.home)
        self.assertTrue(wait_until(lambda: client.call("GET", "/v1/health")["ready"], 20, 0.2),
                        client.call("GET", "/v1/health"))
        report = client.call("GET", "/v1/health")
        self.assertTrue(report["live"])
        self.assertEqual(report["state"], "ready")
        ids = [check["id"] for check in report["checks"]]
        for expected in ("process", "scheduler_loop", "grail_source", "worker_interpreter",
                         "credential", "sandbox", "disk_space", "store", "worker", "mcp",
                         "draining"):
            self.assertIn(expected, ids)
        kinds = {check["id"]: check["kind"] for check in report["checks"]}
        self.assertEqual(kinds["process"], "liveness")
        self.assertEqual(kinds["worker"], "readiness")
        status = cli_json(run_cli(["status", "--json"], self.env))
        self.assertTrue(status["readiness"]["ready"])
        drained = client.call("POST", "/v1/drain", {"timeout": 5})
        self.assertTrue(drained["drained"])
        report = client.call("GET", "/v1/health")
        self.assertTrue(report["live"])
        self.assertFalse(report["ready"])
        self.assertEqual(report["failing"], ["draining"])
        refused = cli_json(run_cli(["chat", "[[tools]]", "--workspace", str(self.workspace),
                                    "--json"], self.env))
        self.assertEqual(refused["evidence"]["refused"], "draining")
        client.call("POST", "/v1/drain", {"resume": True})
        self.assertTrue(client.call("GET", "/v1/health")["ready"])
        version = client.call("GET", "/v1/version")
        self.assertEqual(version["version_id"], report["version"])

    @criteria("H7")
    def test_a_dead_schedule_loop_is_not_live_and_a_failed_mcp_server_is_not_ready(self):
        (self.home / "reach.json").write_text(json.dumps({"mcpServers": {
            "broken": {"command": "/nonexistent/mcp-server"}}}))
        cell = daemon.Daemon(self.home, workspace=self.workspace, environ=self.env,
                             worker_factory=lambda **options: ScriptedWorker(**options))
        ready = threading.Event()
        thread = threading.Thread(target=cell.serve, kwargs={"ready": lambda _s: ready.set()},
                                  daemon=True)
        thread.start()
        self.addCleanup(lambda: (cell.stop(), thread.join(30)))
        self.assertTrue(ready.wait(20))
        self.assertTrue(wait_until(lambda: cell.host.worker_status() and
                                   cell.host.worker_status()["state"] == "warm", 20))
        cell.host.mcp_organ.prepare(["mcp.broken"])
        report = cell.health()
        self.assertTrue(report["live"])
        mcp = next(item for item in report["checks"] if item["id"] == "mcp")
        self.assertFalse(mcp["ok"])
        self.assertIn("broken", mcp["reason"])
        self.assertIn("mcp status", mcp["fix"])
        cell.scheduler.stop()
        cell._loop.join(10)
        report = cell.health()
        self.assertFalse(report["live"])
        self.assertEqual(report["state"], "down")
        loop = next(item for item in report["checks"] if item["id"] == "scheduler_loop")
        self.assertIn("Restart the daemon", loop["fix"])


class SummaryTests(unittest.TestCase):
    @criteria("H7")
    def test_advisory_checks_never_block_readiness_but_liveness_does(self):
        checks = [health.Check("process", True, "up", kind="liveness"),
                  health.Check("grail_source", True, "ok"),
                  health.Check("operations_policy", False, "bad value", "fix it",
                               required=False)]
        summary = health.summarize(checks)
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["advisories"], ["operations_policy"])
        checks[0] = health.Check("process", False, "gone", "start it", kind="liveness")
        summary = health.summarize(checks)
        self.assertEqual((summary["live"], summary["ready"], summary["state"]),
                         (False, False, "down"))
        self.assertIn("fix: start it", health.render(summary))


class DrainTests(unittest.TestCase):
    @criteria("H7", "H5")
    def test_a_drain_lets_the_running_turn_finish_and_starts_nothing_queued(self):
        scratch, home, workspace = private_dir(self), private_dir(self), private_dir(self)
        env = daemon_env(isolated_env(home, scratch, BRAINSTEM_AGENT_CACHE=str(home / "cache")),
                         write_token_file(scratch), scratch)
        process = spawn_fake_daemon(home, workspace, env)
        self.addCleanup(lambda: (run_cli(["stop", "--json"], env, timeout=60),
                                 process.communicate(timeout=30)))
        results = {}

        def chat(name, message):
            results[name] = cli_json(run_cli(["chat", message, "--workspace", str(workspace),
                                              "--json"], env, timeout=60))
        running = threading.Thread(target=chat, args=("running", "[[pause 2]] first"))
        running.start()
        client = daemon.connect(home)
        self.assertTrue(wait_until(lambda: client.call("GET", "/v1/status")["active_turn"],
                                   20, 0.05))
        queued = threading.Thread(target=chat, args=("queued", "[[tools]] second"))
        queued.start()
        time.sleep(0.3)
        drained = client.call("POST", "/v1/drain", {"timeout": 20}, timeout=60)
        running.join(30)
        queued.join(30)
        self.assertTrue(drained["drained"], drained)
        self.assertTrue(results["running"]["ok"], results["running"])
        self.assertEqual(results["queued"]["evidence"].get("refused"), "draining",
                         results["queued"])
