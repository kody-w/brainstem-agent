"""Operable-cell hardening: failure classes found by fault-first review of backups, restores,
upgrades, status, the CLI's error path and the evidence runner. Each spec names the failure it
keeps out."""

import json
import os
import sqlite3
import stat
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from acceptance_support import (cli_json, criteria, isolated_env, private_dir, run_cli,
                                write_token_file)
from brainstem_agent import backup as backups
from brainstem_agent import cli, daemon, health, lifecycle, release, state
from brainstem_agent.host import OWNER, AgentHost, cell_namespace
from brainstem_agent.state import StateError, Store
from ops_support import FakeWorker, seed_home, tool_turn


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def bounded(test, function, seconds: float = 30.0):
    """Run ``function`` on a thread; fail (instead of hanging the suite) if it never returns."""
    outcome = {}

    def run():
        try:
            outcome["value"] = function()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            outcome["error"] = error
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    test.assertFalse(thread.is_alive(), f"still running after {seconds:g}s (it hangs)")
    return outcome


class BackupNeverHangsTests(unittest.TestCase):
    @criteria("H3")
    def test_a_store_held_mid_transaction_fails_the_copy_in_bounded_time(self):
        directory = private_dir(self)
        path = directory / "agent.sqlite3"
        Store(path).close()
        holder = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        target = directory / "copy.sqlite3"
        started = time.monotonic()
        outcome = bounded(self, lambda: state.backup_database(path, target, timeout=1.0))
        self.assertIsInstance(outcome.get("error"), StateError, outcome)
        self.assertIn("locked", str(outcome["error"]))
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse(target.exists(), "no partial copy is left behind")
        holder.execute("ROLLBACK")
        self.assertEqual(state.backup_database(path, target)["integrity_check"], "ok")

    @criteria("H3")
    def test_a_backup_of_a_locked_home_leaves_no_partial_backup_directory(self):
        home, _workspace, _environ = seed_home(self, turns=1)
        holder = sqlite3.connect(home / "state" / "agent.sqlite3", isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        output = private_dir(self) / "backup"
        outcome = bounded(self, lambda: backups.create_backup(home, output, lock_timeout=1.0))
        self.assertIsInstance(outcome.get("error"), StateError, outcome)
        self.assertFalse(output.exists())
        holder.execute("ROLLBACK")
        self.assertTrue(backups.create_backup(home, output)["ok"])


class OwnerOnlyBackupTests(unittest.TestCase):
    @criteria("H3")
    def test_every_directory_a_backup_or_export_creates_is_owner_only(self):
        home, workspace, environ = seed_home(self, turns=1)
        shared = private_dir(self) / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        output = shared / "new" / "nested" / "backup"
        backups.create_backup(home, output)
        self.assertEqual(mode(shared), 0o755, "an existing parent keeps its permissions")
        for directory in (shared / "new", shared / "new" / "nested", output):
            self.assertEqual(mode(directory), 0o700, directory)
        for root, directories, files in os.walk(output):
            for name in directories:
                self.assertEqual(mode(Path(root) / name), 0o700, name)
            for name in files:
                self.assertEqual(mode(Path(root) / name), 0o600, name)
        default = Path(backups.create_backup(home)["path"])
        self.assertEqual(mode(home / "backups"), 0o700)
        self.assertEqual(mode(default), 0o700)
        exported = shared / "export" / "nested"
        host = AgentHost(home, workspace=workspace, environ=environ,
                         worker_factory=lambda **options: FakeWorker(tool_turn(), **options))
        try:
            backups.export_data(host, exported)
        finally:
            host.close()
        for directory in (shared / "export", exported):
            self.assertEqual(mode(directory), 0o700, directory)
        for root, directories, _files in os.walk(exported):
            for name in directories:
                self.assertEqual(mode(Path(root) / name), 0o700, name)


class NewerStoreTests(unittest.TestCase):
    """A store written by a later version is recognized by its schema version, not only by
    its table layout: the same tables with a higher version are still a newer store."""

    def newer_home(self) -> Path:
        home, _workspace, _environ = seed_home(self, turns=1)
        connection = sqlite3.connect(home / "state" / "agent.sqlite3")
        connection.execute(f"PRAGMA user_version = {state.SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        return home

    @criteria("H4", "H3", "H5")
    def test_a_higher_schema_version_with_the_same_tables_is_newer_everywhere(self):
        home = self.newer_home()
        store = home / "state" / "agent.sqlite3"
        found = state.inspect_database(store)
        self.assertEqual(found["compatibility"], "newer")
        self.assertEqual(found["schema_sha256"], state.SCHEMA_DIGEST, "same tables")
        verdict = lifecycle.store_compatibility(home, release.build_manifest())
        self.assertEqual(verdict["verdict"], "incompatible")
        self.assertEqual(health.store_check(home).ok, False)
        # A backup of it cannot be restored by this version (nothing is written).
        copy = private_dir(self) / "backup"
        (copy / "state").mkdir(parents=True, mode=0o700)
        state.backup_database(store, copy / "state" / "agent.sqlite3")
        target = private_dir(self) / "home"
        with self.assertRaisesRegex(backups.BackupError, "newer|nothing was restored"):
            backups.restore_backup(self._sealed(copy), target)
        self.assertFalse((target / "state" / "agent.sqlite3").exists())

    def _sealed(self, directory: Path) -> Path:
        """A backup directory whose manifest matches its files (as ``backup`` writes it)."""
        home, _workspace, _environ = seed_home(self, turns=1)
        made = Path(backups.create_backup(home, private_dir(self) / "real")["path"])
        manifest = json.loads((made / backups.MANIFEST).read_text())
        data = (directory / "state" / "agent.sqlite3").read_bytes()
        (made / "state" / "agent.sqlite3").write_bytes(data)
        for item in manifest["files"]:
            if item["path"] == backups.STORE:
                item["sha256"] = backups._sha256(made / backups.STORE)
                item["bytes"] = len(data)
        (made / backups.MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
        self.assertTrue(backups.verify_backup(made)["ok"], backups.verify_backup(made))
        return made


class RejectedAtWorkerStartTests(unittest.TestCase):
    @criteria("H8")
    def test_a_token_rejected_when_the_warm_worker_starts_is_recorded_and_not_retried(self):
        from ops_support import failing_start

        home, workspace = private_dir(self), private_dir(self)
        token = write_token_file(private_dir(self), "ghu_" + "Revoked" + "x" * 30)
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token),
                   "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(home)}
        starts = []
        rejected = failing_start("The Copilot credential was rejected: Grail reports "
                                 "invalid_credentials.")

        def factory(**options):
            starts.append(1)
            return rejected(tool_turn(), **options)
        host = AgentHost(home, workspace=workspace, environ=environ, worker_factory=factory)
        self.addCleanup(host.close)
        with self.assertRaises(Exception):
            host.warm()
        record = json.loads((home / "state" / "credential.json").read_text())
        self.assertEqual(record["kind"], "invalid")
        for _ in range(5):  # no retries: nothing starts until the token file changes
            self.assertIsNone(host.warm())
        refused = host.chat("hello")
        self.assertEqual(refused.evidence.get("refused"), "credential-invalid")
        self.assertEqual(len(starts), 1)


class JsonErrorTests(unittest.TestCase):
    @criteria("H7")
    def test_a_store_that_cannot_open_is_reported_as_json_not_a_traceback(self):
        home, scratch = private_dir(self), private_dir(self)
        (home / "state").mkdir(mode=0o700)
        store = home / "state" / "agent.sqlite3"
        store.write_bytes(b"this is not a database " * 64)
        os.chmod(store, 0o600)
        env = isolated_env(home, scratch)
        for command in (["status"], ["memory"], ["sessions", "list"], ["chat", "hi"],
                        ["schedules", "list"], ["inbox"], ["stats"],
                        ["export", "--output", str(scratch / "export")]):
            with self.subTest(command=command):
                result = run_cli([*command, "--json"], env)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertNotEqual(result.returncode, 0)
                document = cli_json(result)
                self.assertFalse(document["ok"])
                self.assertIn("store", document["error"].lower())
        human = run_cli(["memory"], env)
        self.assertNotIn("Traceback", human.stderr)
        self.assertIn("Brainstem Agent:", human.stderr)

    @criteria("H7")
    def test_doctor_deep_reports_a_refused_workspace_as_a_failing_check(self):
        outer = private_dir(self)
        home = outer / "home"
        home.mkdir(mode=0o700)
        environ = {"BRAINSTEM_AGENT_HOME": str(home), "BRAINSTEM_AGENT_WORKSPACE": str(outer),
                   "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(home)}
        ready = [health.Check(name, True, "ok") for name in
                 ("grail_source", "worker_interpreter", "sandbox")]
        with mock.patch.object(health, "installation_checks", return_value=ready):
            report = cli.doctor_report(environ, deep=True)
        self.assertFalse(report["deep"]["ok"])
        self.assertIn("contains it", report["deep"]["error"])
        self.assertFalse(report["ready"])
        self.assertTrue(any(item.startswith("deep: ") for item in report["problems"]))


class StatusAgreesWithItselfTests(unittest.TestCase):
    @criteria("H7")
    def test_the_readiness_worker_check_is_the_worker_list_it_is_shown_with(self):
        home, workspace = private_dir(self), private_dir(self)
        token = write_token_file(private_dir(self))
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token),
                   "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(home)}
        cell = daemon.Daemon(home, workspace=workspace, environ=environ,
                             worker_factory=lambda **options: FakeWorker(tool_turn(), **options))
        self.addCleanup(cell.host.close)
        warm = {"worker_id": "w-1", "state": "warm", "pid": os.getpid()}
        for sequence in ([warm, None, None, None], [None, warm, warm, warm]):
            with self.subTest(first=sequence[0]):
                with mock.patch.object(cell.host, "worker_status", side_effect=sequence):
                    status = cell.status()
                workers = status["workers"]
                check = next(item for item in status["readiness"]["checks"]
                             if item["id"] == "worker")
                self.assertEqual(check["ok"], bool(workers) and workers[0]["state"] in
                                 ("warm", "busy"), (workers, check))


class RestoreIntoAMovedHomeTests(unittest.TestCase):
    @criteria("H3")
    def test_restore_into_another_home_keeps_the_default_workspaces_data_visible(self):
        home, scratch = private_dir(self), private_dir(self)
        token = write_token_file(private_dir(self))
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token),
                   "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(home)}
        script = tool_turn(("remember", {"text": "The project codename is heron."}))
        host = AgentHost(home, environ=environ,
                         worker_factory=lambda **options: FakeWorker(script, **options))
        try:
            self.assertTrue(host.chat("remember the codename").ok)
            old_namespace, old_workspace = host.namespace, str(host.workspace)
            self.assertTrue(old_workspace.startswith(str(home)))
            from brainstem_agent import schedules
            schedules.create_schedule(
                host.store, namespace=host.namespace, workspace=str(host.workspace),
                prompt="Summarize notes/", when={"every_seconds": 3600},
                capabilities=["files.read"], allowed=host.known_capabilities(),
                default=host.turn_capabilities(), created_by="owner", now=time.time(),
                name="hourly")
        finally:
            host.close()
        made = backups.create_backup(home, scratch / "backup")
        moved = private_dir(self) / "moved-home"
        result = backups.restore_backup(made["path"], moved)
        self.assertTrue(result["ok"])
        self.assertGreater(result["remapped"]["rows_changed"], 0)
        environ = {**environ, "HOME": str(moved)}
        again = AgentHost(moved, environ=environ,
                          worker_factory=lambda **options: FakeWorker(script, **options))
        try:
            self.assertEqual(again.namespace, cell_namespace(OWNER, str(again.workspace)))
            self.assertNotEqual(again.namespace, old_namespace)
            facts = [fact["text"] for fact in again.store.list_facts(again.namespace)]
            self.assertIn("The project codename is heron.", facts)
            self.assertTrue(again.store.list_sessions(again.namespace))
            restored = again.store.list_schedules(again.namespace)
            self.assertEqual([item["workspace"] for item in restored], [str(again.workspace)])
        finally:
            again.close()
        connection = sqlite3.connect(moved / "state" / "agent.sqlite3")
        try:
            for (table,) in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE "
                    "'sqlite_%'").fetchall():
                for column in [row[1] for row in connection.execute(
                        f'PRAGMA table_info("{table}")')]:
                    left = connection.execute(
                        f'SELECT count(*) FROM "{table}" WHERE "{column}" IN (?, ?)',
                        (old_namespace, old_workspace)).fetchone()[0]
                    self.assertEqual(left, 0, f"{table}.{column} still names the old home")
        finally:
            connection.close()


class TestDaemonsNeverOutliveTheirHomeTests(unittest.TestCase):
    @criteria("H11")
    def test_removing_a_test_home_stops_the_daemon_that_still_serves_it(self):
        from acceptance_support import pid_running, remove_tree
        from daemon_support import daemon_env, spawn_fake_daemon

        root = private_dir(self)
        home, workspace, scratch = root / "home", private_dir(self), private_dir(self)
        home.mkdir(mode=0o700)
        token = write_token_file(private_dir(self))
        env = daemon_env(isolated_env(home, scratch), token, scratch)
        process = spawn_fake_daemon(home, workspace, env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        remove_tree(root)  # the home first, as a cleanup registered too early would
        process.communicate(timeout=30)
        self.assertFalse(pid_running(process.pid))
        self.assertFalse(root.exists())


class EvidenceRunnerFlagsTests(unittest.TestCase):
    @criteria("H12")
    def test_repeated_only_exclude_and_merge_flags_accumulate(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import run_acceptance

        arguments = run_acceptance.build_parser().parse_args(
            ["--only", "a", "--only", "b", "c", "--exclude", "m1", "--exclude", "m2",
             "--merge", "one.json", "--merge", "two.json", "--output", "out.json"])
        self.assertEqual(arguments.only, ["a", "b", "c"])
        self.assertEqual(arguments.exclude, ["m1", "m2"])
        self.assertEqual([path.name for path in arguments.merge], ["one.json", "two.json"])
        plain = run_acceptance.build_parser().parse_args(["--output", "out.json"])
        self.assertEqual((plain.only, plain.exclude, plain.merge), ([], [], []))


if __name__ == "__main__":
    unittest.main()
