"""Private, project-local fixtures only; no core, providers, or real account data."""

import dataclasses
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from brainstem_agent.state import (
    ChatReservation,
    ConflictError,
    JobReservation,
    NotFoundError,
    StateError,
    Store,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(".state-fixture-" + uuid.uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "state.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    @staticmethod
    def response(session, text="fixture answer"):
        return {"response": text, "agent_logs": ["fixture only"], "session_id": session}

    def complete_chat(self, text="original", session="session", key="key", state="succeeded"):
        turn = self.store.reserve_chat("alice", text, session, key)
        self.store.mark_chat_running("alice", turn.turn_id)
        response = self.response(turn.session_id) if state == "succeeded" else None
        self.store.finish_chat("alice", turn.turn_id, state, response)
        return turn

    def race(self, left, right):
        barrier = threading.Barrier(2, timeout=10)

        def run(operation):
            with Store(self.path) as store:
                barrier.wait()
                try:
                    return operation(store)
                except StateError as exc:
                    return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, operation) for operation in (left, right)]
            return [future.result(timeout=15) for future in futures]

    def test_reservations_are_frozen_detached_snapshots(self):
        turn = self.store.reserve_chat("alice", "hello", idempotency_key="chat")
        self.assertIsInstance(turn, ChatReservation)
        self.assertTrue(turn.created)
        self.assertEqual(turn.state, "reserved")
        self.assertIsNone(turn.response)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            turn.state = "succeeded"
        request = {"nested": ["original"]}
        job = self.store.admit_job("alice", "job", request)
        self.assertIsInstance(job, JobReservation)
        self.assertTrue(job.created)
        self.assertEqual(job.state, "accepted")
        self.assertIsNone(job.result)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            job.state = "succeeded"
        request["nested"].append("caller edit")
        job.request["nested"].append("snapshot edit")
        self.assertEqual(self.store.get_job("alice", job.job_id).request, {"nested": ["original"]})
        self.assertFalse(self.store.get_job("alice", job.job_id).created)

    def test_authenticated_owners_have_separate_replay_namespaces(self):
        alice = self.store.reserve_chat("alice", "a", idempotency_key="same")
        bob = self.store.reserve_chat("bob", "b", idempotency_key="same")
        self.assertNotEqual(alice.turn_id, bob.turn_id)
        self.assertNotEqual(alice.session_id, bob.session_id)
        a_job = self.store.admit_job("alice", "same", {"owner": "bob"})
        b_job = self.store.admit_job("bob", "same", {"owner": "bob"})
        self.assertNotEqual(a_job.job_id, b_job.job_id)
        with self.assertRaises(NotFoundError):
            self.store.get_job("bob", a_job.job_id)
        with self.assertRaises(NotFoundError):
            self.store.transition_job("bob", a_job.job_id, "accepted", "running")
        with self.assertRaises(NotFoundError):
            self.store.mark_chat_running("bob", alice.turn_id)
        with self.assertRaises(NotFoundError):
            self.store.finish_chat("bob", alice.turn_id, "cancelled")
        with self.assertRaises(NotFoundError):
            self.store.history("bob", alice.session_id)

    def test_explicit_session_hijack_refused_pending_and_terminal(self):
        turn = self.store.reserve_chat("alice", "original", "bound", "key")
        for key in (None, "key", "different"):
            with self.subTest(key=key), self.assertRaises(ConflictError):
                self.store.reserve_chat("bob", "adopt session", "bound", key)
        self.store.finish_chat("alice", turn.turn_id, "cancelled")
        with self.assertRaises(ConflictError):
            self.store.reserve_chat("bob", "adopt after finish", "bound", "key")
        self.assertEqual(self.store.history("alice", "bound"), [])

    def test_explicit_keys_are_scoped_to_each_session(self):
        first = self.store.reserve_chat("alice", "first", "one", "same")
        second = self.store.reserve_chat("alice", "second", "two", "same")
        self.assertNotEqual(first.turn_id, second.turn_id)
        self.assertEqual(
            self.store.reserve_chat("alice", "first", "one", "same").turn_id, first.turn_id
        )

    def test_omitted_session_replay_retains_original_allocated_session(self):
        first = self.store.reserve_chat("alice", "original", idempotency_key="key")
        again = self.store.reserve_chat("alice", "original", idempotency_key="key")
        self.assertEqual(again.turn_id, first.turn_id)
        self.assertEqual(again.session_id, first.session_id)
        self.assertFalse(again.created)
        self.store.mark_chat_running("alice", first.turn_id)
        original_response = self.response(first.session_id)
        self.store.finish_chat("alice", first.turn_id, "succeeded", original_response)
        self.store.close()
        with Store(self.path) as reopened:
            replay = reopened.reserve_chat("alice", "changed input", idempotency_key="key")
            self.assertEqual(replay.turn_id, first.turn_id)
            self.assertEqual(replay.session_id, first.session_id)
            self.assertEqual(replay.response, original_response)
            self.assertEqual(replay.state, "succeeded")
            self.assertFalse(replay.created)
            explicit = reopened.reserve_chat("alice", "explicit namespace", first.session_id, "key")
            self.assertNotEqual(explicit.turn_id, first.turn_id)
            self.assertTrue(explicit.created)

    def test_terminal_chat_changed_input_replays_original_not_conflict(self):
        for terminal in ("succeeded", "failed", "uncertain", "cancelled"):
            with self.subTest(state=terminal):
                session = "session-" + terminal
                turn = self.store.reserve_chat("alice", "original input", session, "key")
                self.store.mark_chat_running("alice", turn.turn_id)
                original = self.response(session, "retained " + terminal)
                self.store.finish_chat("alice", turn.turn_id, terminal, original)
                original["response"] = "caller mutation"
                replay = self.store.reserve_chat("alice", "changed input", session, "key")
                self.assertEqual(replay.turn_id, turn.turn_id)
                self.assertEqual(replay.state, terminal)
                self.assertEqual(replay.response["response"], "retained " + terminal)
                self.assertFalse(replay.created)
                replay.response["response"] = "snapshot mutation"
                self.assertEqual(
                    self.store.reserve_chat("alice", "third input", session, "key")
                    .response["response"], "retained " + terminal,
                )

    def test_pending_key_returns_same_turn_and_cannot_redispatch(self):
        original = self.store.reserve_chat("alice", "original", "session", "key")
        for text in ("original", "changed pending input"):
            repeated = self.store.reserve_chat("alice", text, "session", "key")
            self.assertEqual(repeated.turn_id, original.turn_id)
            self.assertEqual(repeated.state, "reserved")
            self.assertFalse(repeated.created)
        self.store.mark_chat_running("alice", original.turn_id)
        repeated = self.store.reserve_chat("alice", "changed running input", "session", "key")
        self.assertEqual(repeated.state, "running")
        self.assertFalse(repeated.created)
        with self.assertRaises(ConflictError):
            self.store.mark_chat_running("alice", repeated.turn_id)
        self.store.finish_chat("alice", original.turn_id, "succeeded", self.response("session"))
        self.assertEqual(self.store.history("alice", "session")[0]["content"], "original")

    def test_active_turn_conflicts_and_no_key_always_means_new_request(self):
        turn = self.store.reserve_chat("alice", "original", "session")
        for key in (None, "another-key"):
            for text in ("original", "changed"):
                with self.subTest(key=key, text=text), self.assertRaises(ConflictError):
                    self.store.reserve_chat("alice", text, "session", key)
        self.store.finish_chat("alice", turn.turn_id, "cancelled")
        later = self.store.reserve_chat("alice", "original", "session")
        self.assertTrue(later.created)
        self.assertNotEqual(later.turn_id, turn.turn_id)
        omitted_one = self.store.reserve_chat("alice", "no session or key")
        omitted_two = self.store.reserve_chat("alice", "no session or key")
        self.assertNotEqual(omitted_one.session_id, omitted_two.session_id)

    def test_history_contains_only_successes_in_reservation_order(self):
        self.complete_chat("one", key="one")
        for state in ("failed", "uncertain", "cancelled"):
            self.complete_chat(state, key=state, state=state)
        self.complete_chat("two", key="two")
        pending = self.store.reserve_chat("alice", "pending", "session", "pending")
        self.store.mark_chat_running("alice", pending.turn_id)
        self.assertEqual(self.store.history("alice", "session"), [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "fixture answer"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "fixture answer"},
        ])
        with self.assertRaises(NotFoundError):
            self.store.history("alice", "unknown")

    def test_chat_start_is_exactly_once_and_terminals_are_immutable(self):
        for terminal in ("succeeded", "failed", "uncertain", "cancelled"):
            with self.subTest(terminal=terminal):
                turn = self.store.reserve_chat("alice", "input", terminal, "key")
                running = self.store.mark_chat_running("alice", turn.turn_id)
                self.assertEqual(running.state, "running")
                self.assertFalse(running.created)
                with self.assertRaises(ConflictError):
                    self.store.mark_chat_running("alice", turn.turn_id)
                response = self.response(terminal) if terminal == "succeeded" else None
                completed = self.store.finish_chat("alice", turn.turn_id, terminal, response)
                self.assertFalse(completed.created)
                with self.assertRaises(ConflictError):
                    self.store.mark_chat_running("alice", turn.turn_id)
                for replacement in ("succeeded", "failed", "uncertain", "cancelled"):
                    with self.assertRaises(ConflictError):
                        self.store.finish_chat(
                            "alice", turn.turn_id, replacement, self.response(terminal, "overwrite")
                        )
                replay = self.store.reserve_chat("alice", "changed", terminal, "key")
                self.assertEqual(replay.state, terminal)
                self.assertEqual(replay.response, response)

    def test_success_response_is_exact_normalized_and_session_bound(self):
        turn = self.store.reserve_chat("alice", "input", "session", "key")
        self.store.mark_chat_running("alice", turn.turn_id)
        malformed = [
            None, "answer", {}, {"response": "answer"},
            self.response("other"),
            {**self.response("session"), "extra": True},
            {**self.response("session"), "response": ""},
            {**self.response("session"), "response": " "},
            {**self.response("session"), "response": 2},
            {**self.response("session"), "agent_logs": "line"},
            {**self.response("session"), "agent_logs": [False]},
            {**self.response("session"), "agent_logs": [float("nan")]},
            {**self.response("session"), "response": "x" * 1_000_000},
        ]
        for response in malformed:
            with self.subTest(response_type=type(response).__name__), self.assertRaises(StateError):
                self.store.finish_chat("alice", turn.turn_id, "succeeded", response)
        for state in ("running", "reserved", "typo", [], None):
            with self.subTest(state=state), self.assertRaises(StateError):
                self.store.finish_chat("alice", turn.turn_id, state)
        self.assertEqual(
            self.store.reserve_chat("alice", "input", "session", "key").state, "running"
        )
        self.store.finish_chat("alice", turn.turn_id, "succeeded", self.response("session"))

    def test_jobs_compare_deterministic_local_json_not_input_object_order(self):
        first = self.store.admit_job("alice", "key", {"z": [1, True], "a": {"b": "é"}})
        repeat = self.store.admit_job("alice", "key", {"a": {"b": "é"}, "z": [1, True]})
        self.assertEqual(repeat.job_id, first.job_id)
        self.assertFalse(repeat.created)
        for changed in (
            {"z": [True, 1], "a": {"b": "é"}},
            {"z": [1.0, True], "a": {"b": "é"}},
            {"z": [1, True], "a": {"b": "e\u0301"}},
            {"z": [1, True]},
        ):
            with self.subTest(changed=changed), self.assertRaises(ConflictError):
                self.store.admit_job("alice", "key", changed)

    def test_job_changed_input_conflicts_even_after_terminal_result(self):
        job = self.store.admit_job("alice", "key", {"tool": "fixture-write", "value": "first"})
        self.store.transition_job("alice", job.job_id, "accepted", "running")
        self.store.transition_job("alice", job.job_id, "running", "succeeded", {"written": "first"})
        with self.assertRaises(ConflictError):
            self.store.admit_job("alice", "key", {"tool": "fixture-write", "value": "changed"})
        replay = self.store.admit_job("alice", "key", {"value": "first", "tool": "fixture-write"})
        self.assertEqual(replay.result, {"written": "first"})
        self.assertFalse(replay.created)

    def test_pending_job_duplicate_does_not_start_another_run(self):
        accepted = self.store.admit_job("alice", "key", {"tool": "fixture"})
        repeat = self.store.admit_job("alice", "key", {"tool": "fixture"})
        self.assertEqual(repeat.job_id, accepted.job_id)
        self.assertEqual(repeat.state, "accepted")
        self.assertFalse(repeat.created)
        self.store.transition_job("alice", accepted.job_id, "accepted", "running")
        running = self.store.admit_job("alice", "key", {"tool": "fixture"})
        self.assertEqual(running.state, "running")
        self.assertFalse(running.created)
        with self.assertRaises(ConflictError):
            self.store.transition_job("alice", running.job_id, "accepted", "running")

    def test_job_transition_graph_and_expected_state(self):
        job = self.store.admit_job("alice", "key", {"fixture": True})
        for new_state in ("succeeded", "failed", "uncertain", "accepted"):
            with self.subTest(new_state=new_state), self.assertRaises(ConflictError):
                self.store.transition_job("alice", job.job_id, "accepted", new_state)
        with self.assertRaises(ConflictError):
            self.store.transition_job("alice", job.job_id, "running", "succeeded")
        with self.assertRaises(StateError):
            self.store.transition_job("alice", job.job_id, "accepted", "running", {"early": True})
        for expected, new in ((None, "running"), ("accepted", []), ("typo", "running")):
            with self.subTest(expected=expected, new=new), self.assertRaises(StateError):
                self.store.transition_job("alice", job.job_id, expected, new)
        cancelled = self.store.transition_job("alice", job.job_id, "accepted", "cancelled")
        self.assertEqual(cancelled.state, "cancelled")
        self.assertFalse(cancelled.created)

    def test_job_terminal_results_are_immutable_for_all_terminal_states(self):
        for terminal in ("succeeded", "failed", "uncertain", "cancelled"):
            with self.subTest(terminal=terminal):
                job = self.store.admit_job("alice", terminal, {"terminal": terminal})
                self.store.transition_job("alice", job.job_id, "accepted", "running")
                result = {"status": terminal, "logs": ["original"]}
                self.store.transition_job("alice", job.job_id, "running", terminal, result)
                result["logs"].append("caller mutation")
                for new in ("accepted", "running", "succeeded", "failed", "uncertain", "cancelled"):
                    with self.assertRaises(ConflictError):
                        self.store.transition_job("alice", job.job_id, terminal, new, {"changed": True})
                with self.assertRaises(ConflictError):
                    self.store.transition_job("alice", job.job_id, "running", "succeeded", {})
                snapshot = self.store.get_job("alice", job.job_id)
                self.assertEqual(snapshot.state, terminal)
                self.assertEqual(snapshot.result, {"status": terminal, "logs": ["original"]})
                snapshot.result["logs"].append("snapshot mutation")
                self.assertEqual(self.store.get_job("alice", job.job_id).result["logs"], ["original"])

    def test_missing_records_refuse_with_explicit_error_subclasses(self):
        self.assertTrue(issubclass(ConflictError, StateError))
        self.assertTrue(issubclass(NotFoundError, StateError))
        for operation in (
            lambda: self.store.get_job("alice", "missing"),
            lambda: self.store.transition_job("alice", "missing", "accepted", "running"),
            lambda: self.store.mark_chat_running("alice", "missing"),
            lambda: self.store.finish_chat("alice", "missing", "cancelled"),
            lambda: self.store.revoke_grant("missing"),
        ):
            with self.subTest(operation=operation), self.assertRaises(NotFoundError):
                operation()

    def test_second_connection_job_admission_is_atomic(self):
        left, right = self.race(
            lambda store: store.admit_job("alice", "shared", {"fixture": "same"}),
            lambda store: store.admit_job("alice", "shared", {"fixture": "same"}),
        )
        self.assertIsInstance(left, JobReservation)
        self.assertIsInstance(right, JobReservation)
        self.assertEqual(left.job_id, right.job_id)
        self.assertEqual(sorted([left.created, right.created]), [False, True])

    def test_second_connection_changed_job_admission_conflicts(self):
        outcomes = self.race(
            lambda store: store.admit_job("alice", "shared", {"value": "one"}),
            lambda store: store.admit_job("alice", "shared", {"value": "two"}),
        )
        self.assertEqual(sum(isinstance(item, JobReservation) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)

    def test_second_connection_omitted_session_admission_is_atomic(self):
        left, right = self.race(
            lambda store: store.reserve_chat("alice", "same", idempotency_key="shared"),
            lambda store: store.reserve_chat("alice", "same", idempotency_key="shared"),
        )
        self.assertIsInstance(left, ChatReservation)
        self.assertIsInstance(right, ChatReservation)
        self.assertEqual(left.turn_id, right.turn_id)
        self.assertEqual(left.session_id, right.session_id)
        self.assertEqual(sorted([left.created, right.created]), [False, True])

    def test_second_connection_enforces_one_active_turn(self):
        outcomes = self.race(
            lambda store: store.reserve_chat("alice", "one", "shared", "one"),
            lambda store: store.reserve_chat("alice", "two", "shared", "two"),
        )
        self.assertEqual(sum(isinstance(item, ChatReservation) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)

    def test_second_connection_cannot_race_to_adopt_another_owners_session(self):
        outcomes = self.race(
            lambda store: store.reserve_chat("alice", "one", "shared", "key"),
            lambda store: store.reserve_chat("bob", "two", "shared", "key"),
        )
        self.assertEqual(sum(isinstance(item, ChatReservation) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)
        loser = "bob" if isinstance(outcomes[0], ChatReservation) else "alice"
        with self.assertRaises(ConflictError):
            self.store.reserve_chat(loser, "try again", "shared", "another-key")
        with self.assertRaises(NotFoundError):
            self.store.history(loser, "shared")

    def test_second_connection_job_compare_and_set_has_one_winner(self):
        job = self.store.admit_job("alice", "key", {"fixture": True})
        operation = lambda store: store.transition_job("alice", job.job_id, "accepted", "running")
        outcomes = self.race(operation, operation)
        self.assertEqual(sum(isinstance(item, JobReservation) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)
        self.assertEqual(self.store.get_job("alice", job.job_id).state, "running")

    def test_second_connection_chat_start_has_one_winner(self):
        turn = self.store.reserve_chat("alice", "input", "session", "key")
        operation = lambda store: store.mark_chat_running("alice", turn.turn_id)
        outcomes = self.race(operation, operation)
        self.assertEqual(sum(isinstance(item, ChatReservation) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)

    def test_second_connection_terminal_compare_and_set_retains_winning_result(self):
        job = self.store.admit_job("alice", "key", {})
        self.store.transition_job("alice", job.job_id, "accepted", "running")
        outcomes = self.race(
            lambda store: store.transition_job(
                "alice", job.job_id, "running", "succeeded", {"winner": "success"}
            ),
            lambda store: store.transition_job(
                "alice", job.job_id, "running", "failed", {"winner": "failure"}
            ),
        )
        winners = [outcome for outcome in outcomes if isinstance(outcome, JobReservation)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(isinstance(item, ConflictError) for item in outcomes), 1)
        retained = self.store.get_job("alice", job.job_id)
        self.assertEqual(retained.state, winners[0].state)
        self.assertEqual(retained.result, winners[0].result)

    def test_restart_persists_successful_job_result_without_new_admission(self):
        job = self.store.admit_job("alice", "key", {"fixture": "write"})
        self.store.transition_job("alice", job.job_id, "accepted", "running")
        self.store.transition_job(
            "alice", job.job_id, "running", "succeeded", {"path": "synthetic.txt", "bytes": 3}
        )
        self.store.close()
        with Store(self.path) as restarted:
            replay = restarted.admit_job("alice", "key", {"fixture": "write"})
            self.assertEqual(replay.job_id, job.job_id)
            self.assertEqual(replay.state, "succeeded")
            self.assertEqual(replay.result, {"path": "synthetic.txt", "bytes": 3})
            self.assertFalse(replay.created)

    def test_open_does_not_recover_work_running_in_another_connection(self):
        turn = self.store.reserve_chat("alice", "input", "session", "key")
        job = self.store.admit_job("alice", "key", {})
        self.store.mark_chat_running("alice", turn.turn_id)
        self.store.transition_job("alice", job.job_id, "accepted", "running")
        with Store(self.path) as second:
            self.assertEqual(second.get_job("alice", job.job_id).state, "running")
            self.assertEqual(second.reserve_chat("alice", "input", "session", "key").state, "running")
        self.store.finish_chat("alice", turn.turn_id, "succeeded", self.response("session"))
        self.store.transition_job("alice", job.job_id, "running", "succeeded", {"complete": True})

    def test_explicit_recovery_preserves_uncertainty_and_not_started_distinction(self):
        reserved = self.store.reserve_chat("alice", "not started", "reserved", "reserved")
        running = self.store.reserve_chat("alice", "started", "running", "running")
        accepted = self.store.admit_job("alice", "accepted", {"fixture": "not started"})
        job_running = self.store.admit_job("alice", "running", {"fixture": "started"})
        self.store.mark_chat_running("alice", running.turn_id)
        self.store.transition_job("alice", job_running.job_id, "accepted", "running")
        succeeded = self.complete_chat("complete", "succeeded", "succeeded")
        self.store.close()
        with Store(self.path) as restarted:
            self.assertEqual(restarted.get_job("alice", job_running.job_id).state, "running")
            self.assertIsNone(restarted.recover_interrupted())
            self.assertIsNone(restarted.recover_interrupted())
            chat_replay = restarted.reserve_chat("alice", "changed", "running", "running")
            job_replay = restarted.admit_job("alice", "running", {"fixture": "started"})
            self.assertEqual(chat_replay.state, "uncertain")
            self.assertEqual(job_replay.state, "uncertain")
            self.assertFalse(chat_replay.created)
            self.assertFalse(job_replay.created)
            self.assertIsNone(chat_replay.response)
            self.assertIsNone(job_replay.result)
            with self.assertRaises(ConflictError):
                restarted.mark_chat_running("alice", running.turn_id)
            with self.assertRaises(ConflictError):
                restarted.transition_job("alice", job_running.job_id, "accepted", "running")
            self.assertEqual(
                restarted.reserve_chat("alice", "not started", "reserved", "reserved").state,
                "reserved",
            )
            self.assertEqual(restarted.get_job("alice", accepted.job_id).state, "accepted")
            self.assertEqual(
                restarted.reserve_chat("alice", "complete", "succeeded", "succeeded").response,
                self.response(succeeded.session_id),
            )
            self.assertEqual(restarted.history("alice", running.session_id), [])
        with Store(self.path) as restarted_again:
            self.assertEqual(restarted_again.get_job("alice", job_running.job_id).state, "uncertain")
            self.assertEqual(
                restarted_again.reserve_chat("alice", "changed", "running", "running").state,
                "uncertain",
            )
            self.assertEqual(
                restarted_again.mark_chat_running("alice", reserved.turn_id).state, "running"
            )

    def test_recovery_never_replays_an_interrupted_fixture_effect(self):
        effect = self.directory / "fixture-effect.txt"
        effects = []
        job = self.store.admit_job("alice", "write", {"path": "fixture-effect.txt"})
        self.store.transition_job("alice", job.job_id, "accepted", "running")
        effect.write_text("original effect", encoding="utf-8")
        effects.append("executed")
        self.store.close()
        with Store(self.path) as restarted:
            restarted.recover_interrupted()
            replay = restarted.admit_job("alice", "write", {"path": "fixture-effect.txt"})
            if replay.created:
                effects.append("incorrect redispatch")
            self.assertEqual(replay.state, "uncertain")
            self.assertEqual(effects, ["executed"])
            self.assertEqual(effect.read_text(encoding="utf-8"), "original effect")
            with self.assertRaises(ConflictError):
                restarted.transition_job("alice", job.job_id, "running", "succeeded", {"guessed": True})

    def test_abrupt_process_exit_persists_results_and_requires_explicit_recovery(self):
        self.store.close()
        effect = self.directory / "interrupted-effect.txt"
        script = """
import os
import sys
from pathlib import Path
from brainstem_agent.state import Store

store = Store(sys.argv[1])
turn = store.reserve_chat("alice", "original", "child-session", "child-chat")
store.mark_chat_running("alice", turn.turn_id)
store.reserve_chat("alice", "not started", "reserved-session", "reserved-chat")
store.admit_job("alice", "accepted-job", {})
complete = store.admit_job("alice", "complete-job", {})
store.transition_job("alice", complete.job_id, "accepted", "running")
store.transition_job("alice", complete.job_id, "running", "succeeded", {"retained": True})
running = store.admit_job("alice", "running-job", {})
store.transition_job("alice", running.job_id, "accepted", "running")
Path(sys.argv[2]).write_text("effect before result commit", encoding="utf-8")
os._exit(23)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.path), str(effect)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 23, completed.stderr)
        with Store(self.path) as restarted:
            running = restarted.admit_job("alice", "running-job", {})
            self.assertEqual(running.state, "running")
            self.assertFalse(running.created)
            restarted.recover_interrupted()
            self.assertEqual(restarted.get_job("alice", running.job_id).state, "uncertain")
            self.assertIsNone(restarted.get_job("alice", running.job_id).result)
            complete = restarted.admit_job("alice", "complete-job", {})
            self.assertEqual(complete.state, "succeeded")
            self.assertEqual(complete.result, {"retained": True})
            self.assertFalse(complete.created)
            chat = restarted.reserve_chat("alice", "changed", "child-session", "child-chat")
            self.assertEqual(chat.state, "uncertain")
            self.assertFalse(chat.created)
            self.assertEqual(
                restarted.reserve_chat("alice", "not started", "reserved-session", "reserved-chat")
                .state, "reserved",
            )
            self.assertEqual(restarted.admit_job("alice", "accepted-job", {}).state, "accepted")
            self.assertEqual(effect.read_text(encoding="utf-8"), "effect before result commit")
            with self.assertRaises(ConflictError):
                restarted.mark_chat_running("alice", chat.turn_id)
            with self.assertRaises(ConflictError):
                restarted.transition_job("alice", running.job_id, "uncertain", "running")

    def test_grants_return_flat_binding_and_revocation_persists(self):
        binding = {
            "owner": "alice", "workspace": "fixture", "session": "s", "turn": "t",
            "worker": "w", "generation": 1, "capabilities": ["write"], "expires_at": 123456,
        }
        self.assertIsNone(self.store.get_grant("missing"))
        self.assertIsNone(self.store.store_grant("opaque-handle", binding))
        self.assertEqual(self.store.get_grant("opaque-handle"), {**binding, "revoked": False})
        self.assertIs(self.store.get_grant("opaque-handle")["revoked"], False)
        binding["capabilities"].append("caller edit")
        snapshot = self.store.get_grant("opaque-handle")
        snapshot["capabilities"].append("snapshot edit")
        self.assertEqual(self.store.get_grant("opaque-handle")["capabilities"], ["write"])
        with self.assertRaises(ConflictError):
            self.store.store_grant("opaque-handle", {"owner": "bob"})
        self.assertIsNone(self.store.revoke_grant("opaque-handle"))
        self.assertIsNone(self.store.revoke_grant("opaque-handle"))
        self.store.close()
        with Store(self.path) as restarted:
            retained = restarted.get_grant("opaque-handle")
            self.assertIs(retained["revoked"], True)
            self.assertEqual(retained["owner"], "alice")
            self.assertEqual(retained["capabilities"], ["write"])
            with self.assertRaises(ConflictError):
                restarted.store_grant("opaque-handle", {"owner": "bob"})
            restarted.revoke_grant("opaque-handle")

    def test_revocation_flag_is_store_controlled_not_binding_controlled(self):
        self.store.store_grant("grant", {"owner": "alice", "revoked": "untrusted field"})
        self.assertIs(self.store.get_grant("grant")["revoked"], False)
        self.store.revoke_grant("grant")
        self.assertIs(self.store.get_grant("grant")["revoked"], True)

    def test_second_connection_grant_binding_is_immutable_and_revocation_visible(self):
        self.store.store_grant("grant", {"owner": "alice"})
        with Store(self.path) as second:
            with self.assertRaises(ConflictError):
                second.store_grant("grant", {"owner": "bob"})
            second.revoke_grant("grant")
            self.assertIs(self.store.get_grant("grant")["revoked"], True)

    def test_invalid_identifiers_and_text_refuse_before_admission(self):
        invalid = (None, "", " ", 4, True, [], "x" * 513, "\ud800", "with\ncontrol")
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                for operation in (
                    lambda: self.store.reserve_chat(value, "input"),
                    lambda: self.store.admit_job("alice", value, {}),
                    lambda: self.store.store_grant(value, {}),
                ):
                    with self.assertRaises(StateError):
                        operation()
        for value in ("", " ", None, 3, "x" * 1_000_000, "\ud800"):
            with self.subTest(input_type=type(value).__name__), self.assertRaises(StateError):
                self.store.reserve_chat("alice", value)
        for value in ("", [], "\ud800"):
            with self.assertRaises(StateError):
                self.store.reserve_chat("alice", "input", value, "key")
            with self.assertRaises(StateError):
                self.store.reserve_chat("alice", "input", "session", value)

    def test_malformed_nonfinite_oversize_and_recursive_json_refuse(self):
        recursive = []
        recursive.append(recursive)
        nested = []
        for _ in range(40):
            nested = [nested]
        malformed = (
            float("nan"), float("inf"), float("-inf"), {"n": float("nan")},
            {1: "integer key"}, {"tuple": (1, 2)}, {"set": {1, 2}}, {"bytes": b"x"},
            {"object": object()}, {"string": "\ud800"}, {"data": "x" * 1_000_000},
            [0] * 10_001, recursive, nested, 10 ** 5000,
        )
        job = self.store.admit_job("alice", "valid", {})
        self.store.transition_job("alice", job.job_id, "accepted", "running")
        for index, value in enumerate(malformed):
            with self.subTest(index=index):
                with self.assertRaises(StateError):
                    self.store.admit_job("alice", "bad-" + str(index), value)
                with self.assertRaises(StateError):
                    self.store.store_grant("bad-" + str(index), {"payload": value})
                with self.assertRaises(StateError):
                    self.store.transition_job("alice", job.job_id, "running", "succeeded", value)
        self.assertEqual(self.store.get_job("alice", job.job_id).state, "running")
        for value in (None, "binding", [], 1):
            with self.assertRaises(StateError):
                self.store.store_grant("bad-binding", value)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM grants").fetchone()[0], 0)

    def test_json_escaped_size_is_bounded_not_only_unescaped_characters(self):
        with self.assertRaises(StateError):
            self.store.admit_job("alice", "escaped", {"text": "\0" * 60_000})
        with self.assertRaises(StateError):
            self.store.admit_job("alice", "utf8", {"text": "😀" * 70_000})

    def test_valid_json_scalar_array_and_null_requests_are_retained(self):
        for index, value in enumerate((None, True, 4, 2.5, "fixture", ["a", {"b": False}])):
            with self.subTest(value=value):
                job = self.store.admit_job("alice", str(index), value)
                self.assertEqual(self.store.get_job("alice", job.job_id).request, value)

    def test_context_manager_closes_and_close_is_idempotent(self):
        other = self.directory / "other.sqlite3"
        with Store(other) as store:
            store.admit_job("alice", "key", {})
        store.close()
        with self.assertRaises(StateError):
            store.admit_job("alice", "another", {})
        with self.assertRaises(StateError):
            store.__enter__()
        with self.assertRaisesRegex(RuntimeError, "fixture exception"):
            with Store(other) as raised:
                raise RuntimeError("fixture exception")
        with self.assertRaises(StateError):
            raised.get_grant("anything")
        with Store(other) as reopened:
            self.assertFalse(reopened.admit_job("alice", "key", {}).created)

    def test_private_directory_and_database_permissions(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.store.admit_job("alice", "key", {})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_uid, os.geteuid())

    def test_existing_unsafe_database_permissions_are_rejected_not_repaired(self):
        self.store.close()
        self.path.chmod(0o644)
        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            Store(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.chmod(0o600)

    def test_private_path_is_rechecked_before_mutation(self):
        self.path.chmod(0o644)
        try:
            with self.assertRaises(StateError):
                self.store.admit_job("alice", "key", {})
        finally:
            self.path.chmod(0o600)
        self.assertTrue(self.store.admit_job("alice", "key", {}).created)

    def test_unsafe_parent_permissions_and_missing_parent_are_refused(self):
        for mode in (0o755, 0o770, 0o777):
            directory = self.directory / ("unsafe-" + oct(mode))
            directory.mkdir(mode=0o700)
            directory.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(StateError):
                Store(directory / "state.sqlite3")
            self.assertFalse((directory / "state.sqlite3").exists())
        with self.assertRaises(StateError):
            Store(self.directory / "missing" / "state.sqlite3")
        self.assertFalse((self.directory / "missing").exists())

    def test_database_symlink_and_hardlink_are_rejected(self):
        self.store.close()
        before = self.path.read_bytes()
        symlink = self.directory / "symlink.sqlite3"
        symlink.symlink_to(self.path.name)
        with self.assertRaises(StateError):
            Store(symlink)
        hardlink = self.directory / "hardlink.sqlite3"
        os.link(self.path, hardlink)
        try:
            with self.assertRaises(StateError):
                Store(hardlink)
            with self.assertRaises(StateError):
                Store(self.path)
        finally:
            hardlink.unlink()
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_directory_components_are_rejected(self):
        actual = self.directory / "actual"
        actual.mkdir(mode=0o700)
        nested = actual / "nested"
        nested.mkdir(mode=0o700)
        link = self.directory / "link"
        link.symlink_to("actual", target_is_directory=True)
        for candidate in (link / "state.sqlite3", link / "nested" / "state.sqlite3"):
            with self.subTest(candidate=candidate), self.assertRaises(StateError):
                Store(candidate)
        self.assertFalse((actual / "state.sqlite3").exists())
        self.assertFalse((nested / "state.sqlite3").exists())

    def test_symlink_sidecars_are_rejected_before_sqlite_opens(self):
        self.store.close()
        target = self.directory / "unrelated.txt"
        target.write_text("must not change", encoding="utf-8")
        target.chmod(0o600)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            sidecar.symlink_to(target.name)
            try:
                with self.subTest(suffix=suffix), self.assertRaises(StateError):
                    Store(self.path)
            finally:
                sidecar.unlink()
        self.assertEqual(target.read_text(encoding="utf-8"), "must not change")

    def test_nonfile_paths_and_sqlite_uri_forms_are_refused(self):
        for path in (
            None, 3, b"database", "", " ", ":memory:", "file:database?mode=memory",
            "bad\0path", "\ud800", self.directory, self.directory / ".." / "escape.sqlite3",
        ):
            with self.subTest(path=path), self.assertRaises(StateError):
                Store(path)
        fifo = self.directory / "fifo"
        os.mkfifo(fifo, mode=0o600)
        with self.assertRaises(StateError):
            Store(fifo)

    def test_replaced_path_is_detected_before_using_open_store(self):
        original = self.directory / "original.sqlite3"
        self.path.rename(original)
        try:
            with Store(self.path):
                pass
            with self.assertRaises(StateError):
                self.store.admit_job("alice", "key", {})
        finally:
            self.store.close()
        with Store(original) as retained:
            self.assertTrue(retained.admit_job("alice", "key", {}).created)

    def test_unknown_schema_version_is_explicit_failure_without_reset(self):
        self.store.admit_job("alice", "retained", {"important": True})
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 99")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(StateError, "Unknown.*schema"):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_unknown_application_or_changed_schema_is_not_adopted(self):
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(StateError, "schema"):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        other = self.directory / "unrelated.sqlite3"
        with sqlite3.connect(other) as connection:
            connection.execute("CREATE TABLE user_data (value TEXT)")
            connection.execute("PRAGMA user_version = 1")
        other.chmod(0o600)
        before = other.read_bytes()
        with self.assertRaises(StateError):
            Store(other)
        self.assertEqual(other.read_bytes(), before)

    def test_empty_existing_file_and_corrupt_database_are_not_reset(self):
        for name, content in (("empty", b""), ("corrupt", b"not a SQLite database\n")):
            path = self.directory / (name + ".sqlite3")
            path.write_bytes(content)
            path.chmod(0o600)
            with self.subTest(name=name), self.assertRaises(StateError):
                Store(path)
            self.assertEqual(path.read_bytes(), content)

    def test_stored_malformed_duplicate_and_nonfinite_json_fail_explicitly(self):
        job = self.store.admit_job("alice", "key", {})
        malformed = (
            '{', '{"a":1,"a":2}', '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}',
            '"' + ("x" * 1_000_000) + '"',
        )
        for index, encoded in enumerate(malformed):
            with self.subTest(index=index):
                with sqlite3.connect(self.path) as connection:
                    connection.execute(
                        "UPDATE jobs SET request_json = ? WHERE job_id = ?", (encoded, job.job_id)
                    )
                with self.assertRaises(StateError):
                    self.store.get_job("alice", job.job_id)
                before = self.path.read_bytes()
                with self.assertRaises(StateError):
                    Store(self.path)
                self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE jobs SET request_json = '{}'")

    def test_stored_nonobject_grant_binding_is_explicit_failure(self):
        self.store.store_grant("grant", {"owner": "alice"})
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE grants SET binding_json = '[]' WHERE grant_id = 'grant'")
        with self.assertRaises(StateError):
            self.store.get_grant("grant")
        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stored_response_session_corruption_is_explicit_failure(self):
        turn = self.complete_chat()
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """UPDATE chats SET response_json =
                   '{"response":"wrong","agent_logs":[],"session_id":"another"}' WHERE turn_id = ?""",
                (turn.turn_id,),
            )
        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_sql_like_binding_values_are_data_not_queries(self):
        session = "session'); DROP TABLE jobs; --"
        key = "key' OR '1'='1"
        turn = self.store.reserve_chat("alice", "fixture", session, key)
        self.store.finish_chat("alice", turn.turn_id, "succeeded", self.response(session))
        self.assertEqual(self.store.reserve_chat("alice", "changed", session, key).turn_id, turn.turn_id)
        self.assertTrue(self.store.admit_job("alice", key, {"text": session}).created)


if __name__ == "__main__":
    unittest.main()
