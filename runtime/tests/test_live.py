"""Live acceptance: real Copilot inference through the brainstem's existing connection.

Skips unless BRAINSTEM_AGENT_LIVE=1. A full run spends about 12 live turns:
A2 1, A3 2, A4 2, A5 1, A8 5 (two kills, one cancel, one fresh turn, one CLI SIGINT),
A9 1.
Every test uses a fresh temporary home and workspace and ends with a secret scan.
BRAINSTEM_AGENT_LIVE_REQUEST_BUDGET=<n> (optional) caps the Grail requests all chats of one
test process may spend: each chat gets ``--max-segments`` of at most what is left.
"""

import os
import signal
import threading
import time
import unittest

from acceptance_support import (
    LIVE,
    PINNED_COMMIT,
    base_env,
    cli_json,
    criteria,
    leaks,
    prepared_cache,
    private_dir,
    real_credential_needles,
    record_metric,
    run_cli,
)

A2_PROMPT = ("Create a file named notes/hello.txt containing exactly: hi from the cell. "
             "Then tell me the file's size in bytes.")
# Grail requests left for this test process (None: no cap). Helpers' requests count too.
REQUEST_BUDGET = {"left": int(os.environ["BRAINSTEM_AGENT_LIVE_REQUEST_BUDGET"])
                  if os.environ.get("BRAINSTEM_AGENT_LIVE_REQUEST_BUDGET") else None,
                  "used": 0}


def group_gone(pgid, wait):
    deadline = time.monotonic() + wait
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class LiveCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not LIVE:
            raise unittest.SkipTest("set BRAINSTEM_AGENT_LIVE=1 to spend real Copilot turns")
        cls.cache = prepared_cache()

    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.outputs = []
        self.addCleanup(self.assert_no_secret_leaks)

    def env(self, **extra):
        return base_env(self.home, BRAINSTEM_AGENT_CACHE=str(self.cache), **extra)

    def chat(self, message, *extra, env=None, timeout=300):
        cap = REQUEST_BUDGET["left"]
        if cap is not None:
            if cap < 1:
                self.fail("the live request budget (BRAINSTEM_AGENT_LIVE_REQUEST_BUDGET) is spent")
            extra = (*extra, "--max-segments", str(min(8, cap)))
        result = run_cli(["chat", message, "--workspace", str(self.workspace), "--json", *extra],
                         env or self.env(), timeout=timeout)
        self.outputs += [result.stdout, result.stderr]
        report = cli_json(result)
        if cap is not None:
            used = ((report.get("evidence") or {}).get("long_turn") or {}).get("grail_requests")
            used = used if isinstance(used, int) else min(8, cap)  # unknown: count the cap
            REQUEST_BUDGET["left"] -= used
            REQUEST_BUDGET["used"] += used
            record_metric("live_grail_requests_used", REQUEST_BUDGET["used"])
        return result, report

    def cli(self, *arguments):
        result = run_cli([*arguments, "--workspace", str(self.workspace), "--json"], self.env())
        self.outputs += [result.stdout, result.stderr]
        return cli_json(result)

    def assert_no_secret_leaks(self):
        self.assertEqual(leaks(real_credential_needles(), roots=[self.home], texts=self.outputs), [])

    def assert_success(self, result, report):
        self.assertEqual(result.returncode, 0, report.get("error"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], "succeeded")
        self.assertEqual(set(report["response"]), {"response", "agent_logs", "session_id"})
        self.assertEqual(report["evidence"]["class"], "live")


@unittest.skipUnless(LIVE, "set BRAINSTEM_AGENT_LIVE=1 to spend real Copilot turns")
class LiveCliTests(LiveCase):
    @criteria("A2", "A7", "A11")
    def test_a2_live_tool_turn_writes_the_file_and_leaves_a_receipt(self):
        started = time.monotonic()
        result, report = self.chat(A2_PROMPT)
        record_metric("a2_turn_seconds", round(time.monotonic() - started, 2))
        self.assert_success(result, report)
        target = self.workspace / "notes" / "hello.txt"
        self.assertTrue(target.is_file())
        content = target.read_bytes()
        # The prompt's sentence-final period is ambiguous; both readings are exact content.
        self.assertIn(content, (b"hi from the cell", b"hi from the cell."))
        self.assertIn(str(len(content)), report["response"]["response"])
        record_metric("a2_response", report["response"]["response"][:300])
        record_metric("a2_file_bytes", len(content))
        record_metric("a2_worker_start_seconds", report["evidence"]["worker"]["start_seconds"])
        durable = self.cli("receipts")["receipts"]
        self.assertTrue(any(r["tool"] == "write_file" and r["state"] == "succeeded"
                            and r["turn_id"] == report["turn_id"] for r in durable), durable)
        evidence = report["evidence"]
        self.assertEqual(evidence["grail"]["commit"], PINNED_COMMIT)
        self.assertTrue(evidence["worker"]["integrity_before"]["ok"])
        self.assertTrue(evidence["worker"]["integrity_after"]["ok"])
        self.assertIn(".env", evidence["worker"]["integrity_after"]["untracked"])
        self.assertGreaterEqual(evidence["binds"], 1)

    @criteria("A3")
    def test_a3_memory_survives_across_processes_and_sessions(self):
        result, first = self.chat("Remember that my favorite color is teal.")
        self.assert_success(result, first)
        self.assertTrue(any(r["tool"] == "remember" and r["state"] == "succeeded"
                            for r in first["evidence"]["receipts"]), first["evidence"]["receipts"])
        facts = self.cli("memory")["facts"]
        self.assertTrue(any("teal" in fact["text"].lower() for fact in facts), facts)
        result, second = self.chat("What is my favorite color? Answer with one word.")
        self.assert_success(result, second)
        self.assertNotEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["evidence"]["history_messages"], 0)
        self.assertIn("teal", second["response"]["response"].lower())
        record_metric("a3_answer", second["response"]["response"][:200])
        self.assertTrue(second["evidence"]["memory_facts_in_context"] >= 1 or any(
            r["tool"] == "recall" for r in second["evidence"]["receipts"]))

    @criteria("A4")
    def test_a4_session_history_is_owned_and_resent_by_the_cell(self):
        no_memory = ("--capabilities", "files.read")
        result, first = self.chat("My code word is PAPAYA.", *no_memory)
        self.assert_success(result, first)
        result, second = self.chat("What code word did I tell you?",
                                   "--session", first["session_id"], *no_memory)
        self.assert_success(result, second)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["evidence"]["history_messages"], 2)
        self.assertIn("PAPAYA", second["response"]["response"].upper())
        record_metric("a4_answer", second["response"]["response"][:200])
        self.assertEqual(self.cli("memory")["facts"], [])

    @criteria("A5")
    def test_a5_grail_uses_the_sandboxed_shell_tool(self):
        command = ("echo cell-shell-ok > shell.txt; /usr/bin/curl -sS -m 5 -o /dev/null "
                   "https://example.com && echo NET_OK || echo NET_BLOCKED")
        result, report = self.chat(
            "Use the run_command tool to run exactly this shell command:\n" + command +
            "\nThen tell me whether its output said NET_OK or NET_BLOCKED.")
        self.assert_success(result, report)
        self.assertEqual((self.workspace / "shell.txt").read_text(), "cell-shell-ok\n")
        self.assertTrue(any(r["tool"] == "run_command" for r in report["evidence"]["receipts"]))
        self.assertIn("NET_BLOCKED", report["response"]["response"])
        record_metric("a5_answer", report["response"]["response"][:300])

    @criteria("A9")
    def test_a9_same_key_replays_without_a_new_grail_call(self):
        prompt = "Reply with a random four-digit number and nothing else."
        result, first = self.chat(prompt, "--idempotency-key", "a9-key")
        self.assert_success(result, first)
        receipts_before = self.cli("receipts")["receipts"]
        unreachable = self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(self.home / "absent-token"))
        result, again = self.chat(prompt, "--idempotency-key", "a9-key", env=unreachable)
        self.assertEqual(result.returncode, 0, again.get("error"))
        self.assertTrue(again["replayed"])
        self.assertEqual(again["response"], first["response"])
        self.assertEqual(again["turn_id"], first["turn_id"])
        self.assertIsNone(again["evidence"]["worker"])
        self.assertEqual(self.cli("receipts")["receipts"], receipts_before)
        record_metric("a9_replayed_without_credential", again["replayed"])


    @criteria("A8")
    def test_a8_sigint_cancels_a_cli_turn_and_kills_the_worker(self):
        import json
        import subprocess
        import sys

        process = subprocess.Popen(
            [sys.executable, "-m", "brainstem_agent", "chat",
             "Use the run_command tool to run exactly: sleep 45\nThen report its output.",
             "--workspace", str(self.workspace), "--json"],
            env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 150
        started = False
        while time.monotonic() < deadline and process.poll() is None:
            if any(r["tool"] == "run_command" and r["state"] == "started"
                   for r in self.cli("receipts")["receipts"]):
                started = True
                break
            time.sleep(0.3)
        if not started:
            process.kill()
            process.communicate()
            self.fail("the CLI turn never started run_command")
        signalled = time.monotonic()
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=30)
        elapsed = time.monotonic() - signalled
        self.outputs += [out, err]
        record_metric("a8_cli_sigint_seconds", round(elapsed, 3))
        self.assertEqual(process.returncode, 4, err[-300:])
        report = json.loads(out)
        self.assertEqual(report["state"], "cancelled")
        self.assertLess(elapsed, 5)
        pid = report["evidence"]["worker"]["pid"]
        self.assertTrue(group_gone(pid, 1.0))
        self.assertTrue(report["evidence"]["worker"]["stop"]["group_gone"])


@unittest.skipUnless(LIVE, "set BRAINSTEM_AGENT_LIVE=1 to spend real Copilot turns")
class LiveHostFailureTests(LiveCase):
    def host(self):
        from brainstem_agent.host import AgentHost

        host = AgentHost(self.home, workspace=self.workspace, cache=self.cache, environ=self.env())
        self.addCleanup(host.close)
        return host

    def run_in_thread(self, host, message):
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.update(result=host.chat(message, timeout=240)), daemon=True)
        thread.start()
        return thread, outcome

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def tool_started(self, host, tool="run_command"):
        return any(r["tool"] == tool and r["state"] == "started" for r in host.receipts())

    @criteria("A8")
    def test_a8_killing_the_worker_during_a_tool_is_uncertain(self):
        host = self.host()
        thread, outcome = self.run_in_thread(
            host, "Use the run_command tool to run exactly: sleep 20; echo finished\n"
                  "Then report its output.")
        self.assertTrue(self.wait_for(lambda: self.tool_started(host), 150), "no tool started")
        worker = host.active_worker
        os.killpg(worker["pgid"], signal.SIGKILL)
        thread.join(90)
        result = outcome["result"]
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "uncertain")
        self.assertIsNone(result.response)
        record_metric("a8_kill_during_tool_state", result.state)

    @criteria("A8")
    def test_a8_killing_the_worker_before_any_tool_is_never_success(self):
        host = self.host()
        thread, outcome = self.run_in_thread(
            host, "Without using any tools, write a 300-word story about a lighthouse keeper.")
        self.assertTrue(self.wait_for(
            lambda: (host.active_turn or {}).get("phase") == "streaming", 150))
        os.killpg(host.active_worker["pgid"], signal.SIGKILL)
        thread.join(90)
        result = outcome["result"]
        self.assertFalse(result.ok)
        started = any(r["turn_id"] == result.turn_id for r in host.receipts())
        self.assertEqual(result.state, "uncertain" if started else "failed")
        record_metric("a8_kill_before_tool_state", result.state)

    @criteria("A8")
    def test_a8_cancel_stops_the_process_group_within_5s_and_next_turn_is_fresh(self):
        host = self.host()
        thread, outcome = self.run_in_thread(
            host, "Use the run_command tool to run exactly: sleep 45\nThen report its output.")
        self.assertTrue(self.wait_for(lambda: self.tool_started(host), 150), "no tool started")
        worker = host.active_worker
        started = time.monotonic()
        cancelled = host.cancel()
        self.assertTrue(cancelled["cancelled"])
        self.assertTrue(group_gone(worker["pgid"], max(0.0, 5 - (time.monotonic() - started))))
        record_metric("cancel_seconds", round(time.monotonic() - started, 3))
        self.assertLess(time.monotonic() - started, 5)
        thread.join(30)
        self.assertEqual(outcome["result"].state, "cancelled")
        record_metric("a8_cancel_state", outcome["result"].state)
        self.assertTrue(self.wait_for(lambda: not self.tool_started(host), 5))
        started = time.monotonic()
        after = host.chat("Reply with exactly the word: ready", timeout=240)
        record_metric("fresh_worker_turn_seconds", round(time.monotonic() - started, 2))
        self.assertTrue(after.ok, after.error)
        self.assertNotEqual(after.evidence["worker"]["generation"], worker["generation"])


if __name__ == "__main__":
    unittest.main()
