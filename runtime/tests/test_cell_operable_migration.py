"""H4 unit specs: migration safety.

An older store is migrated only after an automatic pre-migration backup; a migration killed
mid-way (``BRAINSTEM_AGENT_CRASH_AT=store.migrating``, SIGKILL inside the migration
transaction) leaves the original store intact and usable; a store from a newer version is
refused clearly, without writing a byte.
"""

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from acceptance_support import RUNTIME, criteria, private_dir, record_metric
from brainstem_agent import state
from brainstem_agent.state import StateError, Store

LAYOUTS = {"cell-v1 (schema 1)": (1, state._V1_TABLES),
           "cell-v1 (schema 2)": (2, state._V2_TABLES),
           "scheduling (schema 2)": (2, state._V2S_TABLES),
           "learning (schema 2)": (2, state._V2L_TABLES)}
OPEN = r"""
import sys
sys.path.insert(0, sys.argv[1])
from brainstem_agent.state import Store
with Store(sys.argv[2]) as store:
    print(len(store.list_facts("ns")))
"""


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def older_store(directory: Path, version: int, tables) -> Path:
    """A store with exactly an older layout and some data in it."""
    path = Path(directory) / "agent.sqlite3"
    connection = sqlite3.connect(path)
    for name, statement in state._SCHEMA.items():
        if name in tables:
            connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {state._APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {version}")
    connection.execute("INSERT INTO sessions (owner, session_id) VALUES ('ns', 's1')")
    connection.execute(
        """INSERT INTO chats (turn_id, owner, session_id, user_input, key_session,
           idempotency_key, state, response_json) VALUES ('turn_old', 'ns', 's1', 'old question',
           '', NULL, 'succeeded', '{"agent_logs":[],"response":"old answer","session_id":"s1"}')""")
    if "facts" in tables:
        connection.execute("INSERT INTO facts VALUES ('fact_1', 'ns', 'kept fact', 1, 1, NULL)")
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)
    return path


def layout_of(path: Path) -> tuple[int, set]:
    connection = sqlite3.connect(path)  # read-write: rolls back a hot journal, like any open
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE substr(name, 1, 7) <> 'sqlite_'")}
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return version, names
    finally:
        connection.close()


class PreMigrationBackupTests(unittest.TestCase):
    @criteria("H4")
    def test_every_older_layout_is_copied_aside_before_it_is_migrated(self):
        for label, (version, tables) in LAYOUTS.items():
            with self.subTest(label):
                directory = private_dir(self)
                path = older_store(directory, version, tables)
                original = state.inspect_database(path)
                self.assertEqual(original["compatibility"], "migrates")
                with Store(path) as store:
                    backup = store.pre_migration_backup
                    self.assertIsNotNone(backup)
                    self.assertEqual(store.history("ns", "s1")[1]["content"], "old answer")
                copy = Path(backup["path"])
                self.assertEqual(copy.parent, directory / "pre-migration")
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
                self.assertEqual(state.inspect_database(copy)["schema_sha256"],
                                 original["schema_sha256"])
                self.assertEqual(backup["integrity_check"], "ok")
                self.assertEqual(backup["from_schema_version"], version)
                self.assertEqual(state.inspect_database(path)["compatibility"], "current")
                with Store(path) as again:  # a current store is never copied again
                    self.assertIsNone(again.pre_migration_backup)
                self.assertEqual(len(list((directory / "pre-migration").iterdir())), 1)

    @criteria("H4")
    def test_a_backup_that_cannot_be_written_stops_the_migration_before_any_write(self):
        directory = private_dir(self)
        path = older_store(directory, 2, state._V2L_TABLES)
        (directory / "pre-migration").write_text("a file where the backup folder goes")
        before = digest(path)
        with self.assertRaisesRegex(StateError, "pre-migration backup could not be written"):
            Store(path)
        self.assertEqual(digest(path), before)
        self.assertEqual(state.inspect_database(path)["compatibility"], "migrates")


class InterruptedMigrationTests(unittest.TestCase):
    @criteria("H4", "H12")
    def test_a_migration_killed_mid_transaction_leaves_the_original_intact_and_usable(self):
        directory = private_dir(self)
        path = older_store(directory, 2, state._V2S_TABLES)
        version, names = layout_of(path)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(directory),
               "BRAINSTEM_AGENT_CRASH_AT": "store.migrating"}
        killed = subprocess.run([sys.executable, "-c", OPEN, str(RUNTIME), str(path)],
                                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr[-500:])
        journal = Path(str(path) + "-journal")
        hot_journal = journal.exists()
        # The original layout and data are all there (SQLite rolls the hot journal back).
        self.assertEqual(layout_of(path), (version, names))
        connection = sqlite3.connect(path)
        self.assertEqual(connection.execute("SELECT count(*) FROM chats").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT text FROM facts").fetchone()[0], "kept fact")
        connection.close()
        copies = list((directory / "pre-migration").glob("*.sqlite3"))
        self.assertEqual(len(copies), 1, "the pre-migration backup was taken before the kill")
        # ... and usable: the next open migrates it for real.
        env.pop("BRAINSTEM_AGENT_CRASH_AT")
        again = subprocess.run([sys.executable, "-c", OPEN, str(RUNTIME), str(path)],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(again.returncode, 0, again.stderr[-500:])
        self.assertEqual(again.stdout.strip(), "1")
        self.assertEqual(state.inspect_database(path)["compatibility"], "current")
        record_metric("h4_fault_injected_migration", {
            "from_layout": "scheduling (schema 2)", "fault": "SIGKILL inside the migration "
            "transaction (BRAINSTEM_AGENT_CRASH_AT=store.migrating)",
            "exit": killed.returncode, "hot_journal_left": hot_journal,
            "original_intact": True, "pre_migration_backup": copies[0].name,
            "reopened_and_migrated": True})

    @criteria("H4")
    def test_the_crash_hook_is_inert_without_the_owners_variable(self):
        directory = private_dir(self)
        path = older_store(directory, 1, state._V1_TABLES)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(directory),
               "BRAINSTEM_AGENT_CRASH_AT": "segment.started"}
        result = subprocess.run([sys.executable, "-c", OPEN, str(RUNTIME), str(path)],
                                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])


class NewerStoreTests(unittest.TestCase):
    def assert_refused_without_writing(self, path: Path, pattern: str):
        before = (digest(path), path.stat().st_mtime_ns)
        with self.assertRaisesRegex(StateError, pattern):
            Store(path)
        self.assertEqual((digest(path), path.stat().st_mtime_ns), before)
        self.assertFalse(Path(str(path) + "-journal").exists())
        self.assertFalse((path.parent / "pre-migration").exists())

    @criteria("H4")
    def test_a_higher_schema_version_is_refused_clearly_without_writing(self):
        directory = private_dir(self)
        path = directory / "agent.sqlite3"
        Store(path).close()
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
        connection.close()
        self.assertEqual(state.inspect_database(path)["compatibility"], "newer")
        self.assert_refused_without_writing(path, "newer Brainstem Agent.*Nothing was changed")

    @criteria("H4")
    def test_a_later_additive_layout_is_refused_as_newer(self):
        directory = private_dir(self)
        path = directory / "agent.sqlite3"
        Store(path).close()
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE future_feature (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        self.assert_refused_without_writing(path, "unknown tables future_feature")

    @criteria("H4")
    def test_the_cli_reports_a_newer_store_and_version_reads_it_without_opening(self):
        from acceptance_support import cli_json, isolated_env, run_cli

        home, scratch = private_dir(self), private_dir(self)
        (home / "state").mkdir(mode=0o700)
        path = home / "state" / "agent.sqlite3"
        Store(path).close()
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
        connection.close()
        before = digest(path)
        env = isolated_env(home, scratch)
        version = cli_json(run_cli(["version", "--json"], env))
        self.assertEqual(version["store"]["home_store"]["compatibility"], "newer")
        doctor = cli_json(run_cli(["doctor", "--json"], env))
        self.assertFalse(doctor["checks"]["store"]["ok"])
        self.assertIn("upgrade", doctor["checks"]["store"]["fix"].lower())
        chat = run_cli(["chat", "hi", "--json"], env)
        self.assertNotEqual(chat.returncode, 0)
        self.assertIn("newer Brainstem Agent", chat.stdout + chat.stderr)
        self.assertEqual(digest(path), before)
