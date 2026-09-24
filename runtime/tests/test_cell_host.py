"""A2/A3/A4/A6/A8/A9/A11 unit specs for AgentHost turn orchestration.

The fake worker replaces only the Grail process: it talks to the host's real
broker over loopback HTTP exactly as the bridge does (bind, then invoke) and
returns Grail-shaped SSE bytes. Everything else (store, grants, organs, receipts,
history, memory, replay, failure semantics) is the real product.
"""

import json
import secrets
import threading
import time
import unittest
import urllib.error
import urllib.request

from acceptance_support import (
    CANARY_TOKEN, criteria, kill_quietly, leaks, pid_running, private_dir, record_metric,
    wait_until, write_token_file,
)
from brainstem_agent import sandbox
from brainstem_agent.host import AgentHost

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sse(payload):
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def done(request, text, logs=""):
    return sse({"type": "done", "response": text, "session_id": request["session_id"],
                "agent_logs": logs, "voice_mode": False, "model": "fake-model",
                "requested_model": "fake-model", "streamed": True})


class FakeWorker:
    def __init__(self, script, *, worker_id, broker_url, credential, model, register, **_):
        self.script = script
        self.worker_id = worker_id
        self.broker_url = broker_url
        self.credential = credential
        self.register = register
        self.generation = None
        self.key = None
        self.pid = self.pgid = None
        self.requests = []
        self.grants = []
        self.stopped = threading.Event()
        self.contexts = []

    def start(self):
        self.generation = "g" + secrets.token_hex(6)
        self.key = self.register(self.worker_id, self.generation)
        return {"generation": self.generation, "integrity_before": {"ok": True, "files": 30}}

    def alive(self):
        return not self.stopped.is_set()

    def stop(self, **_options):
        self.stopped.set()
        return {"integrity_after": {"ok": True, "untracked": [".env"]}, "group_gone": True}

    def stream_chat(self, request, grant, *, read_timeout=300.0):
        self.requests.append(request)
        self.grants.append(grant)
        return self.script(self, request, grant)

    def post(self, path, body):
        request = urllib.request.Request(self.broker_url + path, data=json.dumps(body).encode(),
                                         method="POST", headers={
            "Content-Type": "application/json",
            "X-Brainstem-Agent-Worker": self.worker_id,
            "X-Brainstem-Agent-Generation": self.generation,
            "Authorization": "Bearer " + self.key})
        try:
            with OPEN.open(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def bind(self, grant):
        status, body = self.post("/v1/bind", {"grant": grant})
        assert status == 200, (status, body)
        self.contexts.append(body["context"])
        return body

    def invoke(self, grant, bound, tool, arguments):
        status, body = self.post("/v1/invoke", {
            "grant": grant, "bind_id": bound["bind_id"], "call_id": "call_" + secrets.token_hex(4),
            "tool": tool, "arguments": arguments})
        return status, body


def write_turn(worker, request, grant):
    bound = worker.bind(grant)
    status, result = worker.invoke(grant, bound, "write_file",
                                   {"path": "notes/hello.txt", "content": "hi from the cell."})
    assert status == 200 and result["ok"], result
    yield sse({"type": "agent", "logs": "[write_file] " + result["content"]})
    yield sse({"type": "delta", "text": "Done"})
    yield done(request, "Created notes/hello.txt; it is 17 bytes.", "[write_file] ok")


def answer_turn(text):
    def script(worker, request, grant):
        worker.bind(grant)
        yield done(request, text)
    return script


class HostCase(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        self.workers = []
        self.script = answer_turn("ok")

    def factory(self, **options):
        worker = FakeWorker(lambda *args: self.script(*args), **options)
        self.workers.append(worker)
        return worker

    def host(self, workspace=None, environ=None):
        host = AgentHost(self.home, workspace=workspace or self.workspace,
                         environ=environ or self.environ, worker_factory=self.factory)
        self.addCleanup(host.close)
        return host


class TurnTests(HostCase):
    @criteria("A2")
    def test_tool_turn_succeeds_with_exact_envelope_receipt_and_revoked_grant(self):
        self.script = write_turn
        host = self.host()
        result = host.chat("Create a file named notes/hello.txt containing exactly: hi from the cell.")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.state, "succeeded")
        self.assertFalse(result.replayed)
        self.assertEqual(set(result.response), {"response", "agent_logs", "session_id"})
        self.assertIsInstance(result.response["agent_logs"], list)
        self.assertTrue(all(isinstance(line, str) for line in result.response["agent_logs"]))
        self.assertEqual((self.workspace / "notes" / "hello.txt").read_text(), "hi from the cell.")
        [receipt] = host.receipts(turn_id=result.turn_id)
        self.assertEqual((receipt["tool"], receipt["state"]), ("write_file", "succeeded"))
        self.assertEqual(result.evidence["binds"], 1)
        self.assertEqual(result.evidence["history_messages"], 0)
        self.assertEqual([r["tool"] for r in result.evidence["receipts"]], ["write_file"])
        worker = self.workers[0]
        status, _ = worker.post("/v1/bind", {"grant": worker.grants[0]})
        self.assertEqual(status, 403, "grant must be revoked once the turn ends")
        request = worker.requests[0]
        self.assertEqual(set(request), {"user_input", "session_id", "conversation_history"})

    @criteria("A8")
    def test_turn_without_a_bind_is_never_success(self):
        def script(worker, request, grant):
            yield done(request, "I answered without the cell's tools.")
        self.script = script
        result = self.host().chat("hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "failed")
        self.assertIn("tool", result.error.lower())
        self.assertIsNone(result.response)

    @criteria("A8")
    def test_error_event_and_eof_without_done_are_failures(self):
        def error_script(worker, request, grant):
            worker.bind(grant)
            yield sse({"type": "error", "error": "Copilot auth failed (401): Bad credentials."})

        def eof_script(worker, request, grant):
            worker.bind(grant)
            yield sse({"type": "delta", "text": "partial answer"})

        for script, detail in ((error_script, "401"), (eof_script, "without")):
            with self.subTest(script.__name__):
                self.script = script
                # A fresh home each: a 401 records the credential as rejected in its home, so a
                # later turn there is refused before Grail (see test_cell_operable_credential).
                self.home = private_dir(self)
                result = self.host().chat("hello")
                self.assertFalse(result.ok)
                self.assertEqual(result.state, "failed")
                self.assertIsNone(result.response)
                self.assertIn(detail, result.error)

    @criteria("A8")
    def test_worker_crash_is_failed_before_tools_and_uncertain_after(self):
        def crash_before(worker, request, grant):
            worker.bind(grant)
            raise ConnectionResetError("worker died")
            yield b""

        def crash_after(worker, request, grant):
            bound = worker.bind(grant)
            worker.invoke(grant, bound, "write_file", {"path": "partial.txt", "content": "x"})
            raise ConnectionResetError("worker died")
            yield b""

        self.script = crash_before
        first = self.host().chat("hello")
        self.assertEqual((first.ok, first.state), (False, "failed"))
        self.script = crash_after
        second = self.host().chat("write then die")
        self.assertEqual((second.ok, second.state), (False, "uncertain"))
        self.assertIn("uncertain", second.error.lower())

    @criteria("A8")
    def test_a_dead_worker_is_replaced_for_the_next_turn(self):
        def crash(worker, request, grant):
            worker.bind(grant)
            worker.stopped.set()
            raise ConnectionResetError("worker died")
            yield b""

        self.script = crash
        host = self.host()
        self.assertFalse(host.chat("first").ok)
        self.script = answer_turn("fresh")
        result = host.chat("second")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.workers), 2)
        self.assertNotEqual(self.workers[0].generation, self.workers[1].generation)
        self.assertEqual(result.evidence["worker"]["generation"], self.workers[1].generation)


class ReplayAndSessionTests(HostCase):
    @criteria("A9")
    def test_same_idempotency_key_replays_without_a_new_grail_call(self):
        self.script = answer_turn("original answer")
        host = self.host()
        first = host.chat("question", idempotency_key="key-1")
        dispatched = sum(len(w.requests) for w in self.workers)
        again = host.chat("a different question", idempotency_key="key-1")
        self.assertTrue(again.ok)
        self.assertTrue(again.replayed)
        self.assertEqual(again.response, first.response)
        self.assertEqual(again.turn_id, first.turn_id)
        self.assertEqual(sum(len(w.requests) for w in self.workers), dispatched)
        self.assertIsNone(again.evidence.get("worker"))

    @criteria("A9")
    def test_replay_survives_a_new_process_without_a_credential(self):
        self.script = answer_turn("durable answer")
        first = self.host().chat("question", idempotency_key="key-2")
        no_credential = {"BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        again = self.host(environ=no_credential).chat("question", idempotency_key="key-2")
        self.assertTrue(again.replayed)
        self.assertEqual(again.response, first.response)

    @criteria("A8", "A9")
    def test_a_retained_failed_turn_is_not_redispatched(self):
        def failing(worker, request, grant):
            worker.bind(grant)
            yield sse({"type": "error", "error": "Model returned 502."})
        self.script = failing
        host = self.host()
        self.assertFalse(host.chat("q", idempotency_key="key-3").ok)
        self.script = answer_turn("should never run")
        count = sum(len(w.requests) for w in self.workers)
        again = host.chat("q", idempotency_key="key-3")
        self.assertFalse(again.ok)
        self.assertEqual(again.state, "failed")
        self.assertIn("not redispatched", again.error)
        self.assertEqual(sum(len(w.requests) for w in self.workers), count)

    @criteria("A4")
    def test_session_history_is_owned_and_resent_by_the_cell(self):
        self.script = answer_turn("Got it.")
        host = self.host()
        first = host.chat("My code word is PAPAYA.")
        self.script = answer_turn("PAPAYA")
        second = host.chat("What code word did I tell you?", session_id=first.session_id)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.session_id, first.session_id)
        history = self.workers[-1].requests[-1]["conversation_history"]
        self.assertEqual(history, [
            {"role": "user", "content": "My code word is PAPAYA."},
            {"role": "assistant", "content": "Got it."},
        ])
        self.assertEqual(second.evidence["history_messages"], 2)

    @criteria("A3")
    def test_new_session_sends_no_history_and_memory_arrives_as_context(self):
        host = self.host()
        host.invoke_tool("remember", {"text": "The user's favorite color is teal."})
        self.script = answer_turn("Teal")
        result = host.chat("What is my favorite color? Answer with one word.")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.workers[-1].requests[-1]["conversation_history"], [])
        self.assertIn("teal", self.workers[-1].contexts[-1])
        self.assertGreaterEqual(result.evidence["memory_facts_in_context"], 1)

    @criteria("A6")
    def test_workspaces_do_not_share_memory_files_or_sessions(self):
        host_a = self.host()
        other = private_dir(self)
        host_b = self.host(workspace=other)
        host_a.invoke_tool("remember", {"text": "The user's favorite color is teal."})
        host_a.invoke_tool("write_file", {"path": "a.txt", "content": "only A"})
        self.assertEqual(host_b.memory(), [])
        self.assertNotIn("teal", host_b.invoke_tool("recall", {"query": "favorite color"})["content"])
        self.assertFalse(host_b.invoke_tool("read_file", {"path": "a.txt"})["ok"])
        first = host_a.chat("hello from A")
        crossed = host_b.chat("continue A's session", session_id=first.session_id)
        self.assertFalse(crossed.ok)
        self.assertEqual(len(host_a.memory()), 1)


class FailureAndSecretTests(HostCase):
    @criteria("A8")
    def test_missing_credential_fails_before_any_worker_exists(self):
        environ = {"BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        result = self.host(environ=environ).chat("hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "failed")
        self.assertIn("credential", result.error.lower())
        self.assertEqual(self.workers, [])

    @criteria("A8")
    def test_cancel_stops_the_worker_and_the_next_turn_gets_a_fresh_one(self):
        def hang(worker, request, grant):
            worker.bind(grant)
            yield sse({"type": "delta", "text": "thinking"})
            worker.stopped.wait(30)
            raise ConnectionResetError("killed")

        self.script = hang
        host = self.host()
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(result=host.chat("long task")))
        thread.start()
        deadline = time.monotonic() + 10
        while not (self.workers and self.workers[0].contexts) and time.monotonic() < deadline:
            time.sleep(0.05)
        started = time.monotonic()
        cancelled = host.cancel()
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(cancelled["cancelled"])
        thread.join(10)
        self.assertEqual(outcome["result"].state, "cancelled")
        self.assertFalse(outcome["result"].ok)
        self.assertFalse(self.workers[0].alive())
        self.script = answer_turn("fresh worker answer")
        after = host.chat("next")
        self.assertTrue(after.ok, after.error)
        self.assertEqual(len(self.workers), 2)
        self.assertNotEqual(after.evidence["worker"]["generation"], self.workers[0].generation)

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A8")
    def test_cancel_during_a_running_tool_marks_it_uncertain_and_stops_it(self):
        def slow_tool(worker, request, grant):
            bound = worker.bind(grant)
            threading.Thread(target=worker.invoke, daemon=True, args=(
                grant, bound, "run_command", {"command": "sleep 30", "timeout_seconds": 60})).start()
            yield sse({"type": "delta", "text": "running"})
            worker.stopped.wait(30)
            raise ConnectionResetError("killed")

        self.script = slow_tool
        host = self.host()
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(result=host.chat("run it")))
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
                r["state"] == "started" for r in host.receipts()):
            time.sleep(0.05)
        host.cancel()
        thread.join(10)
        self.assertEqual(outcome["result"].state, "cancelled")
        self.assertTrue(outcome["result"].evidence["tool_started"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
                r["state"] == "started" for r in host.receipts()):
            time.sleep(0.1)
        states = {r["state"] for r in host.receipts()}
        self.assertNotIn("started", states)
        self.assertNotIn("succeeded", states)

    def start_shell_then(self, finish):
        """A fake worker that starts a long sandboxed command, then ends its stream via ``finish``."""
        marker = self.workspace / "leader.pid"

        def script(worker, request, grant):
            bound = worker.bind(grant)
            threading.Thread(target=worker.invoke, daemon=True, args=(
                grant, bound, "run_command",
                {"command": "echo $$ > leader.pid; exec sleep 30", "timeout_seconds": 60})).start()
            if not wait_until(lambda: marker.exists() and marker.read_text().strip(), 10.0):
                raise AssertionError("the shell tool never started")
            self.addCleanup(kill_quietly, int(marker.read_text()))
            yield from finish(worker, request, grant)

        self.script = script
        return marker

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A8")
    def test_a_turn_ends_only_after_its_cancelled_tools_have_stopped(self):
        def crash(worker, request, grant):
            raise ConnectionResetError("worker killed mid-tool")
            yield b""

        marker = self.start_shell_then(crash)
        host = self.host()
        result = host.chat("run it")
        pid = int(marker.read_text())
        self.assertEqual(result.state, "uncertain")
        self.assertFalse(pid_running(pid), "the turn ended while its shell command still ran")
        states = [r["state"] for r in host.receipts(turn_id=result.turn_id)]
        self.assertEqual(states, ["failed"], "no receipt may stay 'started' after the turn")

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A8")
    def test_close_stops_in_flight_tools_before_it_returns(self):
        def hang(worker, request, grant):
            yield sse({"type": "delta", "text": "running"})
            worker.stopped.wait(30)
            raise ConnectionResetError("killed")

        marker = self.start_shell_then(hang)
        host = self.host()
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(result=host.chat("run it")),
                                  daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: marker.exists() and marker.read_text().strip(), 10.0))
        self.assertTrue(wait_until(lambda: any(r["state"] == "started" for r in host.receipts()),
                                   10.0))
        pid = int(marker.read_text())
        host.close()
        self.assertFalse(pid_running(pid), "close() returned while a shell command still ran")
        self.assertTrue(wait_until(lambda: "result" in outcome, 10.0))
        self.assertEqual(outcome["result"].state, "cancelled")

    @criteria("A8")
    def test_cancel_requested_before_the_turn_starts_is_honoured(self):
        cancel = threading.Event()
        cancel.set()
        host = self.host()
        result = host.chat("never runs", cancel_event=cancel)
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(self.workers, [], "no worker may start for a cancelled turn")
        self.assertEqual(sum(len(w.requests) for w in self.workers), 0)

    @criteria("A11")
    def test_no_secret_material_is_persisted_or_returned(self):
        self.script = write_turn
        host = self.host()
        result = host.chat("write it")
        host.close()
        worker = self.workers[0]
        needles = {"credential": CANARY_TOKEN, "credential-prefix8": CANARY_TOKEN[:8],
                   "worker-key": worker.key, "grant": worker.grants[0]}
        self.assertEqual(leaks(needles, roots=[self.home], texts=[json.dumps(result.to_json())]), [])
        self.assertNotIn(CANARY_TOKEN, repr(worker.credential))


if __name__ == "__main__":
    unittest.main()


class AgentFrameTests(unittest.TestCase):
    """Grail 0.6.16 emits {"type": "agent"} frames after tool rounds (brainstem.py chat_stream)."""

    @criteria("A2", "A8")
    def test_agent_frames_are_accepted_but_never_terminal(self):
        from brainstem_agent.adapter import CoreContractError, normalize_sse

        request = {"session_id": "s1"}
        agent = sse({"type": "agent", "logs": "[write_file] Wrote 17 bytes"}).decode()
        final = done(request, "All done.").decode()
        self.assertEqual(normalize_sse([agent, final], "s1")["response"], "All done.")
        with self.assertRaises(CoreContractError):
            normalize_sse([agent], "s1")
        with self.assertRaises(CoreContractError):
            normalize_sse([sse({"type": "mystery"}).decode(), final], "s1")


class OverheadTests(HostCase):
    @criteria("A2")
    def test_warm_turn_reuses_the_worker_and_cell_overhead_is_small(self):
        host = self.host()
        self.assertTrue(host.chat("warm up").ok)
        started = time.monotonic()
        result = host.chat("second turn")
        elapsed = time.monotonic() - started
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.evidence["worker"]["reused"])
        self.assertEqual(len(self.workers), 1)
        record_metric("warm_turn_overhead_seconds", round(elapsed, 4))
        self.assertLess(elapsed, 2.0)
