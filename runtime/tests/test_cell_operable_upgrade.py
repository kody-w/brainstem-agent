"""H5 unit specs: upgrade and rollback between side-by-side versions.

Candidates are local copies of this runtime with another version (and, for the incompatible
case, a store schema extension); nothing is fetched. Upgrade verifies the candidate's release
manifest, takes a pre-upgrade backup, drains the daemon (running work finishes), keeps the
previous version installed, switches the home's active version (the CLI, the daemon and the
LaunchAgent follow it) and restarts the daemon; rollback switches back when the previous
version can read the store and otherwise refuses with guidance.
"""

import json
import os
import plistlib
import threading
import time
import unittest
from pathlib import Path

from acceptance_support import (cli_json, criteria, isolated_env, pid_running, private_dir,
                                run_cli, wait_until)
from brainstem_agent import backup, daemon, observe, release, state
from daemon_support import daemon_env, spawn_fake_daemon
from ops_support import fake_launchctl, make_candidate, seed_home


class UpgradeCase(unittest.TestCase):
    def setUp(self):
        self.home, self.workspace, self.environ = seed_home(self)
        self.scratch = private_dir(self)
        self.addCleanup(self.stop_daemon)

    def env(self, **extra):
        return {**isolated_env(self.home, self.scratch,
                               BRAINSTEM_AGENT_CACHE=str(self.home / "cache")),
                "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": self.environ[
                    "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"], **extra}

    def cli(self, *arguments, env=None, timeout=240):
        result = run_cli([*arguments, "--json"], env or self.env(), timeout=timeout)
        return result.returncode, cli_json(result)

    def stop_daemon(self):
        record = daemon.read_record(self.home)
        if record is not None:
            run_cli(["stop", "--json"], self.env(), timeout=60)
            if pid_running(record["pid"]):
                os.kill(record["pid"], 9)

    def candidate(self, version, **options) -> Path:
        return make_candidate(private_dir(self), version, **options)


class UpgradeTests(UpgradeCase):
    @criteria("H5")
    def test_upgrade_installs_side_by_side_and_rollback_restores_the_prior_version(self):
        current = release.identity()["id"]
        source = self.candidate("0.1.1")
        code, plan = self.cli("upgrade", "--from", str(source), "--dry-run")
        self.assertEqual(code, 0, plan)
        self.assertEqual((plan["from"], plan["store"]["verdict"]), (current, "reads"))
        self.assertTrue(plan["to"].startswith("0.1.1-"))
        self.assertFalse((self.home / "versions").exists(), "a dry run installs nothing")
        code, done = self.cli("upgrade", "--from", str(source))
        self.assertEqual(code, 0, done)
        self.assertTrue(done["changed"])
        installed = {item["id"] for item in release.installed_versions(self.home)}
        self.assertEqual(installed, {current, done["to"]})
        active = release.read_active(self.home)
        self.assertEqual((active["active"], active["previous"]), (done["to"], current))
        self.assertTrue(backup.verify_backup(done["pre_upgrade_backup"])["ok"])
        code, version = self.cli("version")
        self.assertEqual(version["version"], "0.1.1", "commands follow the active version")
        self.assertEqual(version["install"]["kind"], "home-version")
        code, memory = self.cli("memory", "--workspace", str(self.workspace))
        self.assertEqual(code, 0)
        self.assertTrue(memory["facts"])
        code, back = self.cli("rollback")
        self.assertEqual(code, 0, back)
        self.assertEqual(back["to"], current)
        code, version = self.cli("version")
        self.assertEqual(version["version_id"], current)
        events = {item["event"] for item in observe.read_events(self.home)}
        self.assertTrue({"upgrade.completed", "rollback.completed", "backup.created"} <= events)

    @criteria("H5")
    def test_a_tampered_or_unmanifested_release_is_refused_and_nothing_changes(self):
        tampered = self.candidate("0.1.2")
        (tampered / "brainstem_agent" / "policy.py").write_text("changed = True\n")
        code, refused = self.cli("upgrade", "--from", str(tampered))
        self.assertEqual(code, 1)
        self.assertIn("does not match its manifest", refused["error"])
        bare = self.candidate("0.1.3")
        (bare / "brainstem_agent" / "data" / "release-manifest.json").unlink()
        code, refused = self.cli("upgrade", "--from", str(bare))
        self.assertEqual(code, 1)
        self.assertIn("No release manifest", refused["error"])
        code, refused = self.cli("upgrade", "--from", str(private_dir(self)))
        self.assertEqual(code, 1)
        self.assertFalse(release.read_active(self.home))
        self.assertEqual(release.installed_versions(self.home), [])
        self.assertFalse((self.home / "backups").exists())

    @criteria("H5")
    def test_a_zipapp_is_an_upgrade_source(self):
        source = self.candidate("0.1.4")
        app = private_dir(self) / "brainstem-agent.pyz"
        release.build_zipapp(app, root=source / "brainstem_agent")
        code, done = self.cli("upgrade", "--from", str(app))
        self.assertEqual(code, 0, done)
        self.assertTrue(done["to"].startswith("0.1.4-"))
        self.assertEqual(self.cli("version")[1]["version"], "0.1.4")

    @criteria("H5", "H6")
    def test_the_launchagent_follows_the_active_version(self):
        agents = private_dir(self)
        launchctl, calls = fake_launchctl(private_dir(self))
        env = self.env(BRAINSTEM_AGENT_LAUNCH_AGENTS=str(agents),
                       BRAINSTEM_AGENT_LAUNCHCTL=str(launchctl))
        code, installed = self.cli("service", "install", env=env)
        self.assertEqual(code, 0, installed)
        code, done = self.cli("upgrade", "--from", str(self.candidate("0.1.5")), env=env)
        self.assertEqual(code, 0, done)
        with open(installed["path"], "rb") as handle:
            plist = plistlib.load(handle)
        target = str(self.home / "versions" / done["to"])
        self.assertEqual(plist["EnvironmentVariables"]["PYTHONPATH"], target)
        log = calls.read_text()
        self.assertIn("bootout", log)
        self.assertEqual(log.count("bootstrap"), 2)
        code, back = self.cli("rollback", env=env)
        with open(installed["path"], "rb") as handle:
            plist = plistlib.load(handle)
        self.assertTrue(plist["EnvironmentVariables"]["PYTHONPATH"].endswith(back["to"]))


class DaemonUpgradeTests(UpgradeCase):
    @criteria("H5")
    def test_upgrade_drains_the_daemon_and_restarts_it_on_the_new_version(self):
        env = daemon_env(isolated_env(self.home, self.scratch,
                                      BRAINSTEM_AGENT_CACHE=str(self.home / "cache")),
                         Path(self.environ["BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"]), self.scratch)
        process = spawn_fake_daemon(self.home, self.workspace, env)
        outcome = {}

        def long_turn():
            outcome["result"] = cli_json(run_cli(
                ["chat", "[[pause 2.5]] a long turn", "--workspace", str(self.workspace),
                 "--json"], env, timeout=120))
        thread = threading.Thread(target=long_turn)
        thread.start()
        self.assertTrue(wait_until(lambda: (daemon.connect(self.home).call(
            "GET", "/v1/status").get("active_turn") is not None), 20, 0.05))
        started = time.monotonic()
        code, done = self.cli("upgrade", "--from", str(self.candidate("0.2.1")), env=env)
        thread.join(60)
        process.communicate(timeout=30)
        self.assertEqual(code, 0, done)
        self.assertTrue(outcome["result"]["ok"], "the running turn finished, not cancelled")
        self.assertTrue(done["drain"]["drain"]["drained"])
        self.assertGreater(time.monotonic() - started, 1.0)
        record = daemon.read_record(self.home)
        self.assertIsNotNone(record, "the daemon runs again")
        self.assertNotEqual(record["pid"], process.pid)
        self.assertEqual(record["workspace"], str(self.workspace))
        version = daemon.connect(self.home).call("GET", "/v1/version")
        self.assertEqual(version["version_id"], done["to"])
        self.assertTrue(done["health"]["live"])
        code, back = self.cli("rollback", env=env)
        self.assertEqual(code, 0, back)
        version = daemon.connect(self.home).call("GET", "/v1/version")
        self.assertEqual(version["version_id"], back["to"])

    @criteria("H5")
    def test_a_new_version_that_does_not_come_up_is_rolled_back_automatically(self):
        code, started = self.cli("serve", "--detach", "--workspace", str(self.workspace))
        self.assertEqual(code, 0, started)
        current = release.identity()["id"]
        result = run_cli(["upgrade", "--from", str(self.candidate("0.3.0", broken_serve=True)),
                          "--json"], self.env(), timeout=240)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is active again", cli_json(result)["error"])
        self.assertEqual(release.read_active(self.home)["active"], current)
        version = daemon.connect(self.home).call("GET", "/v1/version")
        self.assertEqual(version["version_id"], current)
        self.assertTrue(observe.read_events(self.home, event="upgrade.rolled_back"))


    @criteria("H5")
    def test_a_launchagent_daemon_that_never_comes_up_is_rolled_back_automatically(self):
        """Under a LaunchAgent the new version's daemon is started by launchd, not by upgrade:
        upgrade still waits for it to answer as the new version, and switches back when it
        does not (here the stand-in launchctl starts nothing)."""
        agents = private_dir(self)
        launchctl, _calls = fake_launchctl(private_dir(self))
        env = self.env(BRAINSTEM_AGENT_LAUNCH_AGENTS=str(agents),
                       BRAINSTEM_AGENT_LAUNCHCTL=str(launchctl),
                       BRAINSTEM_AGENT_SERVICE_START_TIMEOUT="3")
        code, installed = self.cli("service", "install", env=env)
        self.assertEqual(code, 0, installed)
        code, started = self.cli("serve", "--detach", "--workspace", str(self.workspace),
                                 env=env)
        self.assertEqual(code, 0, started)
        current = release.identity()["id"]
        code, done = self.cli("upgrade", "--from", str(self.candidate("0.1.6")), env=env)
        self.assertEqual(code, 1, done)
        self.assertIn("did not come up", done["error"])
        self.assertIn("is active again", done["error"])
        self.assertEqual(release.read_active(self.home)["active"], current)
        with open(installed["path"], "rb") as handle:
            plist = plistlib.load(handle)
        self.assertTrue(plist["EnvironmentVariables"]["PYTHONPATH"].endswith(current))
        self.assertTrue(observe.read_events(self.home, event="upgrade.rolled_back"))


class IncompatibleRollbackTests(UpgradeCase):
    @criteria("H5", "H4")
    def test_a_rollback_the_store_no_longer_fits_refuses_with_guidance(self):
        original = state.inspect_database(self.home / "state" / "agent.sqlite3")
        code, done = self.cli("upgrade", "--from", str(self.candidate("0.4.0",
                                                                       schema_extension=True)))
        self.assertEqual(code, 0, done)
        self.assertEqual(done["store"]["verdict"], "migrates")
        code, memory = self.cli("memory", "--workspace", str(self.workspace))  # opens: migrates
        self.assertEqual(code, 0, memory)
        migrated = state.inspect_database(self.home / "state" / "agent.sqlite3")
        self.assertNotEqual(migrated["schema_sha256"], original["schema_sha256"])
        self.assertTrue(list((self.home / "state" / "pre-migration").glob("*.sqlite3")))
        code, refused = self.cli("rollback")
        self.assertEqual(code, 1)
        self.assertIn("cannot read", refused["error"])
        self.assertIn(f"rollback --restore {done['pre_upgrade_backup']}", refused["error"])
        self.assertEqual(release.read_active(self.home)["active"], done["to"],
                         "a refused rollback changes nothing")
        code, back = self.cli("rollback", "--restore", done["pre_upgrade_backup"])
        self.assertEqual(code, 0, back)
        self.assertTrue(Path(back["restore"]["safety_backup"]).is_dir())
        restored = state.inspect_database(self.home / "state" / "agent.sqlite3")
        self.assertEqual(restored["schema_sha256"], original["schema_sha256"])
        code, memory = self.cli("memory", "--workspace", str(self.workspace))
        self.assertEqual(code, 0)
        self.assertIn("The owner's favorite color is teal.", [f["text"] for f in memory["facts"]])
