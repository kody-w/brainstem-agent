"""A2/A3/A8/A9 unit specs for Store v2: receipts, facts and run events (nucleus)."""

import sqlite3
import subprocess
import sys
import unittest

from acceptance_support import RUNTIME, criteria, private_dir
from brainstem_agent.state import ConflictError, StateError, Store

# The frozen v1 schema as first shipped (used to prove migration).
V1_SCHEMA = [
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        owner TEXT NOT NULL,
        UNIQUE (owner, session_id)
    )""",
    """CREATE TABLE chats (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        user_input TEXT NOT NULL,
        key_session TEXT NOT NULL,
        idempotency_key TEXT,
        state TEXT NOT NULL CHECK (
            state IN ('reserved', 'running', 'succeeded', 'failed', 'uncertain', 'cancelled')
        ),
        response_json TEXT,
        FOREIGN KEY (owner, session_id) REFERENCES sessions (owner, session_id),
        CHECK (key_session = '' OR key_session = session_id),
        CHECK (state <> 'succeeded' OR response_json IS NOT NULL),
        CHECK (state NOT IN ('reserved', 'running') OR response_json IS NULL)
    )""",
    """CREATE UNIQUE INDEX chat_replay
        ON chats (owner, key_session, idempotency_key)
        WHERE idempotency_key IS NOT NULL""",
    """CREATE UNIQUE INDEX chat_active
        ON chats (owner, session_id) WHERE state IN ('reserved', 'running')""",
    """CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        owner TEXT NOT NULL,
        replay_key TEXT NOT NULL,
        request_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('accepted', 'running', 'succeeded', 'failed', 'uncertain', 'cancelled')
        ),
        result_json TEXT,
        UNIQUE (owner, replay_key),
        CHECK (state NOT IN ('accepted', 'running') OR result_json IS NULL)
    )""",
    """CREATE TABLE grants (
        grant_id TEXT PRIMARY KEY NOT NULL,
        binding_json TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
    )""",
]


class StoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.path = self.dir / "agent.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    @criteria("A2")
    def test_receipt_lifecycle_is_exactly_once(self):
        receipt = self.store.begin_receipt(
            "ns", "turn_1", "call_1", "write_file", "files.write", {"path": "notes/hello.txt"})
        [row] = self.store.list_receipts("ns")
        self.assertEqual(row["state"], "started")
        self.assertEqual(row["tool"], "write_file")
        self.assertEqual(row["request"], {"path": "notes/hello.txt"})
        self.store.finish_receipt(receipt, "succeeded", {"ok": True, "content_chars": 5})
        [row] = self.store.list_receipts("ns", turn_id="turn_1")
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["result"]["content_chars"], 5)
        with self.assertRaises(ConflictError):
            self.store.finish_receipt(receipt, "failed", {})
        with self.assertRaises(StateError):
            self.store.finish_receipt("missing", "succeeded", {})
        second = self.store.begin_receipt("ns", "turn_1", "call_2", "read_file", "files.read", {})
        with self.assertRaises(StateError):
            self.store.finish_receipt(second, "started", {})
        with self.assertRaises(ConflictError):
            self.store.begin_receipt("ns", "turn_1", "call_2", "read_file", "files.read", {})

    @criteria("A2", "A6")
    def test_receipts_are_namespaced_and_filterable(self):
        self.store.begin_receipt("ns", "turn_1", "c1", "read_file", "files.read", {})
        self.store.begin_receipt("ns", "turn_2", "c2", "read_file", "files.read", {})
        self.assertEqual(self.store.list_receipts("other"), [])
        self.assertEqual(len(self.store.list_receipts("ns", turn_id="turn_2")), 1)

    @criteria("A8")
    def test_recovery_marks_started_receipts_uncertain_and_is_durable(self):
        self.store.begin_receipt("ns", "turn_1", "c1", "run_command", "shell.run", {})
        self.store.recover_interrupted()
        self.store.close()
        with Store(self.path) as reopened:
            [row] = reopened.list_receipts("ns")
            self.assertEqual(row["state"], "uncertain")

    @criteria("A3")
    def test_facts_crud_search_isolation_and_durability(self):
        fact = self.store.add_fact("ns", "The user's favorite color is teal.", source_turn="turn_1")
        self.assertEqual(set(fact), {"fact_id", "text", "created_at", "updated_at", "source_turn"})
        self.assertEqual(
            [item["fact_id"] for item in self.store.search_facts("ns", "What is my favorite color?")],
            [fact["fact_id"]])
        self.assertEqual(self.store.search_facts("other", "favorite color"), [])
        self.assertEqual(self.store.search_facts("ns", "unrelated zebra"), [])
        self.store.close()
        with Store(self.path) as reopened:
            self.assertEqual(reopened.list_facts("ns")[0]["text"], "The user's favorite color is teal.")
            updated = reopened.update_fact("ns", fact["fact_id"], "Favorite color: teal (confirmed).")
            self.assertIn("confirmed", updated["text"])
            self.assertFalse(reopened.delete_fact("other", fact["fact_id"]))
            self.assertTrue(reopened.delete_fact("ns", fact["fact_id"]))
            self.assertEqual(reopened.list_facts("ns"), [])
        with sqlite3.connect(self.path) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("confirmed", dump)

    @criteria("A3")
    def test_fact_limits(self):
        with self.assertRaises(StateError):
            self.store.add_fact("ns", "x" * 2001)
        with self.assertRaises(StateError):
            self.store.add_fact("ns", "   ")

    @criteria("A8")
    def test_run_events_are_append_only_and_ordered(self):
        turn = self.store.reserve_chat("ns", "hello")
        self.assertEqual(self.store.append_run_event("ns", turn.turn_id, {"kind": "worker"}), 1)
        self.assertEqual(self.store.append_run_event("ns", turn.turn_id, {"kind": "done"}), 2)
        self.assertEqual(
            self.store.run_events("ns", turn.turn_id),
            [(1, {"kind": "worker"}), (2, {"kind": "done"})])
        self.assertEqual(self.store.run_events("ns", turn.turn_id, after=1), [(2, {"kind": "done"})])
        self.assertEqual(self.store.run_events("other", turn.turn_id), [])

    @criteria("A4", "A9")
    def test_get_chat_and_list_sessions(self):
        turn = self.store.reserve_chat("ns", "My code word is PAPAYA.", idempotency_key="k1")
        self.store.mark_chat_running("ns", turn.turn_id)
        self.store.finish_chat("ns", turn.turn_id, "succeeded", {
            "response": "Noted.", "agent_logs": [], "session_id": turn.session_id})
        self.assertEqual(self.store.get_chat("ns", turn.turn_id).state, "succeeded")
        [session] = self.store.list_sessions("ns")
        self.assertEqual(session["session_id"], turn.session_id)
        self.assertEqual(session["turns"], 1)
        self.assertEqual(session["last_state"], "succeeded")
        self.assertEqual(self.store.list_sessions("other"), [])

    @criteria("A10")
    def test_v1_database_migrates_to_v2_without_loss(self):
        legacy_dir = private_dir(self)
        legacy = legacy_dir / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            for statement in V1_SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA application_id = %d" % 0x4D304653)
            connection.execute("PRAGMA user_version = 1")
            connection.execute("INSERT INTO sessions (owner, session_id) VALUES ('ns', 's1')")
            connection.execute(
                """INSERT INTO chats (turn_id, owner, session_id, user_input, key_session,
                   idempotency_key, state, response_json) VALUES
                   ('turn_old', 'ns', 's1', 'old question', '', NULL, 'succeeded',
                    '{"agent_logs":[],"response":"old answer","session_id":"s1"}')""")
        legacy.chmod(0o600)
        with Store(legacy) as migrated:
            self.assertEqual(
                migrated.history("ns", "s1"),
                [{"role": "user", "content": "old question"},
                 {"role": "assistant", "content": "old answer"}])
            migrated.add_fact("ns", "post-migration fact")
        with sqlite3.connect(legacy) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        with Store(legacy) as reopened:
            self.assertEqual(len(reopened.list_facts("ns")), 1)


# Each child imports first, reports ready, then opens on "go" so all opens start together.
RACE = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
mode, target = sys.argv[2], Path(sys.argv[3])
if mode == "store":
    from brainstem_agent.state import Store

    def open_once():
        with Store(target) as store:
            store.list_facts("probe")
else:
    from brainstem_agent.host import AgentHost

    def open_once():
        AgentHost(target, workspace=Path(sys.argv[4]),
                  environ={"HOME": sys.argv[5], "BRAINSTEM_HOME": sys.argv[5]}).close()
sys.stdout.write("ready\n")
sys.stdout.flush()
sys.stdin.readline()
try:
    open_once()
    print("ok")
except Exception as error:
    print("error: %s: %s" % (type(error).__name__, error))
"""


class ConcurrentFirstOpenTests(unittest.TestCase):
    """Two or more processes opening a brand-new home at the same instant must all succeed."""

    def race(self, mode, *, rounds=6, processes=6):
        failures = []
        for _ in range(rounds):
            base = private_dir(self)
            if mode == "store":
                (base / "state").mkdir(mode=0o700)
                extra = [str(base / "state" / "agent.sqlite3")]
            else:
                extra = [str(base / "home"), str(private_dir(self)), str(private_dir(self))]
            children = [subprocess.Popen([sys.executable, "-c", RACE, str(RUNTIME), mode, *extra],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                        for _ in range(processes)]
            try:
                for child in children:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                for child in children:
                    child.stdin.write("go\n")
                    child.stdin.flush()
                for child in children:
                    out, err = child.communicate(timeout=60)
                    if out.strip() != "ok":
                        failures.append(out.strip() or err.strip()[-300:])
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()
        return failures

    @criteria("A3", "A10")
    def test_concurrent_first_open_of_a_new_store_never_fails(self):
        self.assertEqual(self.race("store"), [])

    @criteria("A3", "A10")
    def test_concurrent_first_use_of_a_new_home_never_fails(self):
        self.assertEqual(self.race("host", rounds=4), [])


if __name__ == "__main__":
    unittest.main()
