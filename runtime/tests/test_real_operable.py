"""Real-core H1/H3/H5/H7/H8 specs: the unchanged Grail process, started by the cell, without
inference (worker start-up exchanges the credential with GitHub; no model is called).

Skips unless BRAINSTEM_AGENT_REAL_CORE=1 (and a verified Grail seed is available).
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from acceptance_support import (HAVE_SEED, INSTALLED_CREDENTIAL, NO_SEED, REAL_CORE, RUNTIME,
                                SEED, base_env, cli_json, criteria, leaks, pid_running,
                                prepared_cache, private_dir, real_credential_needles,
                                record_metric, run_cli, wait_until, write_token_file)
from brainstem_agent import daemon, release
from ops_support import (copy_package, fake_brainstem, make_candidate, offline_wheel_problem,
                         replace_token)


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start the real Grail core")
@unittest.skipUnless(HAVE_SEED, NO_SEED)
class RealOperableCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    def setUp(self):
        self.home, self.workspace = private_dir(self), private_dir(self)
        self.outputs = []
        self.addCleanup(self.stop_daemon)
        self.addCleanup(lambda: self.assertEqual(
            leaks(real_credential_needles(), roots=[self.home], texts=self.outputs), []))

    def env(self, home=None, **extra):
        return base_env(home or self.home, BRAINSTEM_AGENT_CACHE=str(self.cache), **extra)

    def cli(self, *arguments, env=None, timeout=300):
        result = run_cli([*arguments, "--json"], env or self.env(), timeout=timeout)
        self.outputs += [result.stdout, result.stderr]
        return result.returncode, cli_json(result)

    def stop_daemon(self, home=None):
        home = home or self.home
        record = daemon.read_record(home)
        if record is not None:
            run_cli(["stop", "--json"], self.env(home), timeout=90)
            if pid_running(record["pid"]):
                os.kill(record["pid"], 9)

    def serve_ready(self, env=None, home=None) -> dict:
        code, started = self.cli("serve", "--detach", "--workspace", str(self.workspace), env=env)
        self.assertEqual(code, 0, started)
        client = daemon.connect(home or self.home)
        seen = {}

        def ready():
            seen["health"] = client.call("GET", "/v1/health", timeout=30)
            return seen["health"]["ready"]
        self.assertTrue(wait_until(ready, 180, 0.5), seen.get("health"))
        return seen["health"]


class InstallTests(RealOperableCase):
    @criteria("H1", "H12")
    def test_a_fresh_venv_install_then_setup_and_doctor_succeed_in_a_fresh_home(self):
        work = private_dir(self)
        source = work / "runtime"
        source.mkdir()
        shutil.copy(RUNTIME / "pyproject.toml", source)
        shutil.copy(RUNTIME / "README.md", source)
        copy_package(source)
        steps, began = [], time.monotonic()

        def step(label, command, env=None, timeout=900):
            started = time.monotonic()
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                    env=env, cwd=str(work))
            self.outputs += [result.stdout, result.stderr]
            steps.append({"step": label, "exit": result.returncode,
                          "seconds": round(time.monotonic() - started, 2)})
            self.assertEqual(result.returncode, 0, result.stderr[-1500:] + result.stdout[-800:])
            return result

        pip_env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "PIP_NO_INPUT": "1",
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        if sys.version_info < (3, 12):
            step("python -m venv .venv", [sys.executable, "-m", "venv", str(work / "venv")])
            problem = offline_wheel_problem(work / "venv" / "bin" / "python")
            if problem:
                self.skipTest(problem)
            step(".venv/bin/pip install --no-index --no-build-isolation ./runtime",
                 [str(work / "venv" / "bin" / "pip"), "install", "--quiet", "--no-index",
                  "--no-build-isolation", str(source)], env=pip_env)
            command = [str(work / "venv" / "bin" / "brainstem-agent")]
            path = "pip offline (--no-index --no-build-isolation)"
        else:
            built = release.build_zipapp(work / "brainstem-agent.pyz", root=source /
                                         "brainstem_agent")
            steps.append({"step": "python -m brainstem_agent.release --zipapp "
                          "brainstem-agent.pyz", "exit": 0, "seconds": 0.0})
            command = [sys.executable, built["path"]]
            path = "zipapp"
        install_seconds = round(time.monotonic() - began, 2)
        home, cache = work / "home", work / "cache"
        env = base_env(home, BRAINSTEM_AGENT_CACHE=str(cache),
                       BRAINSTEM_AGENT_GRAIL_SEED=str(SEED))
        env.pop("PYTHONPATH")
        step("brainstem-agent setup", [*command, "setup", "--json"], env=env)
        doctor = json.loads(step("brainstem-agent doctor --deep",
                                 [*command, "doctor", "--deep", "--json", "--workspace",
                                  str(self.workspace)], env=env, timeout=300).stdout)
        self.assertTrue(doctor["ready"], doctor["problems"])
        self.assertTrue(doctor["deep"]["ok"])
        version = json.loads(step("brainstem-agent version", [*command, "version", "--json"],
                                  env=env).stdout)
        self.assertTrue(version["release_manifest"]["verified"])
        self.assertEqual(leaks(real_credential_needles(), roots=[home]), [])
        record_metric("h1_install_transcript", {
            "python": sys.version.split()[0], "path": path, "install_seconds": install_seconds,
            "steps": steps, "doctor_ready": doctor["ready"], "deep_ok": doctor["deep"]["ok"],
            "version_id": version["version_id"], "fresh_home": True, "fresh_cache": True})


class DaemonOperationsTests(RealOperableCase):
    @criteria("H3", "H7")
    def test_backup_while_a_real_daemon_is_warm_restores_into_a_ready_home(self):
        health = self.serve_ready()
        self.assertTrue(health["live"])
        worker = next(item for item in health["checks"] if item["id"] == "worker")
        self.assertTrue(worker["ok"])
        code, status = self.cli("status")
        self.assertEqual(code, 0)
        self.assertTrue(status["readiness"]["ready"])
        self.cli("profile", "add", "--text", "The owner's favorite color is teal.")
        target = private_dir(self) / "backup"
        code, made = self.cli("backup", "--output", str(target))
        self.assertEqual(code, 0, made)
        fresh = private_dir(self)
        code, restored = self.cli("restore", str(target), env=self.env(fresh))
        self.assertEqual(code, 0, restored)
        self.assertTrue(restored["health"]["ready"], restored["health"]["failing"])
        code, profile = self.cli("profile", "list", env=self.env(fresh))
        self.assertIn("The owner's favorite color is teal.", [f["text"] for f in profile["facts"]])
        self.assertEqual(leaks(real_credential_needles(), roots=[target, fresh]), [])


class UpgradeTests(RealOperableCase):
    @criteria("H5", "H7")
    def test_upgrade_and_rollback_with_a_real_warm_daemon(self):
        before = self.serve_ready()
        candidate = make_candidate(private_dir(self), "0.1.1")
        code, done = self.cli("upgrade", "--from", str(candidate))
        self.assertEqual(code, 0, done)
        client = daemon.connect(self.home)
        self.assertEqual(client.call("GET", "/v1/version")["version_id"], done["to"])
        self.assertTrue(wait_until(lambda: client.call("GET", "/v1/health")["ready"], 180, 0.5))
        code, back = self.cli("rollback")
        self.assertEqual(code, 0, back)
        client = daemon.connect(self.home)
        self.assertEqual(client.call("GET", "/v1/version")["version_id"], before["version"])
        self.assertTrue(wait_until(lambda: client.call("GET", "/v1/health")["ready"], 180, 0.5))


class CredentialTests(RealOperableCase):
    @unittest.skipUnless(INSTALLED_CREDENTIAL.exists(), "needs the installed brainstem's sign-in")
    @criteria("H8")
    def test_real_grail_rejecting_a_token_and_a_new_sign_in_picked_up_by_the_daemon(self):
        fake_home = private_dir(self)
        bogus = "ghu_" + "0" * 36
        brainstem = fake_brainstem(fake_home, bogus)
        env = self.env(HOME=str(fake_home))
        code, first = self.cli("chat", "hello", "--workspace", str(self.workspace), env=env)
        self.assertEqual(code, 1)
        self.assertIn("Sign in again", first["error"])
        record = json.loads((self.home / "state" / "credential.json").read_text())
        self.assertEqual(record["kind"], "invalid")
        started = time.monotonic()
        code, refused = self.cli("chat", "hello", "--workspace", str(self.workspace), env=env)
        self.assertEqual(refused["evidence"]["refused"], "credential-invalid")
        self.assertLess(time.monotonic() - started, 10, "refused without starting Grail")
        code, served = self.cli("serve", "--detach", "--workspace", str(self.workspace), env=env)
        self.assertEqual(code, 0, served)
        client = daemon.connect(self.home)
        health = client.call("GET", "/v1/health")
        self.assertFalse(health["ready"])
        self.assertIn("credential", health["failing"])
        # A new sign-in: the installed brainstem's file is replaced (here, in the stand-in,
        # by a copy of the real sign-in; the real file is only read).
        raw = INSTALLED_CREDENTIAL.read_text().strip()
        value = json.loads(raw)["access_token"] if raw.startswith("{") else raw
        try:
            replace_token(brainstem, value)
            client.call("POST", "/v1/wake", {})
            self.assertTrue(wait_until(lambda: client.call("GET", "/v1/health")["ready"], 180,
                                       0.5), client.call("GET", "/v1/health"))
            self.assertEqual(daemon.read_record(self.home)["pid"], served["pid"], "no restart")
            self.assertFalse((self.home / "state" / "credential.json").exists())
        finally:
            self.stop_daemon()
            (brainstem / "src" / "rapp_brainstem" / ".copilot_token").unlink(missing_ok=True)
        self.assertEqual(leaks({"bogus": bogus, **real_credential_needles()},
                               roots=[self.home]), [])
