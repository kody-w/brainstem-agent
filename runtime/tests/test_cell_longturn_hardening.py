"""Long-horizon hardening specs, written test-first.

Each spec was written before its fix. Only the Grail process is replaced
(``longturn_support.GrailEmulator``, Grail's own three-round loop around a scripted model);
the host, broker, organs, store, sandbox and CLI are the real product.

1. Child depth: default 1, the configured depth is the depth helpers actually reach, within
   a hard bound.
2. The model's voice: the continuation record is never placed in the assistant role, and an
   answer that writes tool calls out in the cell's log format without receipts is never
   accepted.
3. Grail's logs: large tool results copied into ``agent_logs`` never fail a turn.
4. The soul's newline rule for files that will receive appended lines.
5. The crash-injection hook is inert unless the owner's environment arms it.
7. Seven review findings: continuations and sessions that exceed one request, a script's
   exact reads and inner calls, settling every receipt, forgetting a session's helpers,
   scripts and processes, and a turn's time limit inside every grant's lifetime.
"""

import json
import os
import signal
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from acceptance_support import (cli_json, criteria, isolated_env, private_dir, run_cli,
                                write_token_file)
from brainstem_agent import daemon, longturn, policy, sandbox
from brainstem_agent.adapter import DEFAULT_LIMITS, normalize_sse
from brainstem_agent.host import AgentHost
from brainstem_agent.longturn import LIMITS, TurnBudget
from brainstem_agent.state import StateError
from longturn_support import GRAIL_FALLBACK, GrailEmulator, journal_calls, scripted_policy
from test_cell_host import done, sse

FAKE_LOG = ("I continued the chain:\n"
            "4. write_file {\"path\": \"chain/4.txt\", \"content\": \"4\"} -> ok: Wrote 1 bytes\n"
            "5. write_file {\"path\": \"chain/5.txt\", \"content\": \"5\"} -> ok: Wrote 1 bytes\n"
            "The final value is 5.")


class Case(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        self.workers = []

    def host(self, policy_, worker_class=GrailEmulator, **environ):
        def factory(**options):
            worker = worker_class(policy_, **options)
            self.workers.append(worker)
            return worker
        host = AgentHost(self.home, workspace=self.workspace,
                         environ={**self.environ, **environ}, worker_factory=factory)
        self.addCleanup(host.close)
        return host

    def requests(self):
        return [request for worker in self.workers for request in worker.requests]


def written(request, results):
    """Paths the turn has written so far: the cell's journal plus this request's results."""
    paths = [json.loads(arguments)["path"] for tool, arguments in journal_calls(request)
             if tool == "write_file"]
    return paths + [item["arguments"]["path"] for item in results
                    if item["tool"] == "write_file" and item["ok"]]


# -- 1. child depth ------------------------------------------------------------------------
def depth_policy(seen):
    """ROOT delegates to MID, MID delegates to LEAF, LEAF writes deep/leaf.txt."""
    def policy_(request, results, tools=(), final=False):
        text = request["user_input"]
        if final:
            return "continuing"
        role = "LEAF" if text.startswith("LEAF") else "MID" if text.startswith("MID") else "ROOT"
        if not results:
            seen.append((role, "delegate_tasks" in tools))
        if role == "LEAF":
            if not results:
                return [("write_file", {"path": "deep/leaf.txt", "content": "leaf"})]
            return "leaf wrote deep/leaf.txt"
        if role == "MID":
            if not results:
                return [("delegate_tasks", {"tasks": [{"task": "LEAF writes deep/leaf.txt"}]})]
            return "mid: " + results[0]["content"][:800]
        if not results:
            return [("delegate_tasks", {"tasks": [{"task": "MID delegates to a leaf"}]})]
        return "root: " + results[0]["content"][:1500]
    return policy_


class DepthTests(Case):
    @criteria("D5", "D10")
    def test_the_default_depth_is_one_and_a_helper_is_never_offered_delegation(self):
        seen = []
        host = self.host(depth_policy(seen))
        self.assertEqual(host.budget_defaults.max_depth, 1)
        result = host.chat("ROOT")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(seen, [("ROOT", True), ("MID", False)])
        self.assertFalse((self.workspace / "deep" / "leaf.txt").exists())
        [child] = result.evidence["long_turn"]["children"]
        self.assertEqual(child["state"], "succeeded")

    @criteria("D5", "D10")
    def test_a_configured_depth_is_the_depth_helpers_actually_reach(self):
        seen = []
        host = self.host(depth_policy(seen), BRAINSTEM_AGENT_MAX_DEPTH="2")
        result = host.chat("ROOT")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(seen, [("ROOT", True), ("MID", True), ("LEAF", False)])
        self.assertEqual((self.workspace / "deep" / "leaf.txt").read_text(), "leaf")
        journal = host.store.journal(host.namespace, result.turn_id)
        depths = sorted(step["detail"]["depth"] for step in journal["steps"]
                        if step["kind"] == "child")
        self.assertEqual(depths, [1, 2])

    @criteria("D5", "D10")
    def test_depth_zero_offers_no_delegation_at_all(self):
        seen = []
        host = self.host(depth_policy(seen), BRAINSTEM_AGENT_MAX_DEPTH="0")
        result = host.chat("ROOT")
        self.assertEqual(seen[0], ("ROOT", False))
        self.assertNotIn("agents.delegate", result.evidence["capabilities"])
        self.assertEqual(result.evidence["long_turn"]["children"], [])

    @criteria("D3", "D5")
    def test_the_depth_is_configurable_only_within_a_hard_bound(self):
        self.assertEqual(LIMITS["max_depth"], 2)
        self.assertEqual(TurnBudget.from_env({"BRAINSTEM_AGENT_MAX_DEPTH": "2"}).max_depth, 2)
        for bad in ("3", "9", "-1"):
            with self.assertRaises(ValueError):
                TurnBudget.from_env({"BRAINSTEM_AGENT_MAX_DEPTH": bad})
        scratch = private_dir(self)
        result = run_cli(["chat", "hello", "--workspace", str(self.workspace), "--json"],
                         isolated_env(self.home, scratch, BRAINSTEM_AGENT_MAX_DEPTH="3"))
        self.assertEqual(result.returncode, 2, result.stderr[-400:])
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("max_depth", cli_json(result)["error"])


# -- 2. the model's voice and fabricated tool logs -------------------------------------------
class VoiceTests(Case):
    @criteria("D1", "D10")
    def test_every_assistant_message_of_a_continuation_is_the_models_own_reply(self):
        answers = []

        def policy_(request, results, tools=(), final=False):
            paths = written(request, results)
            if final:
                answer = "" if len(paths) == 3 else "Now let me continue:"
                answers.append(answer)
                return answer
            if len(paths) < 6:
                return [("write_file", {"path": f"f/{len(paths) + 1}.txt", "content": "x"})]
            return "All six files are written."
        host = self.host(policy_)
        result = host.chat("Write f/1.txt to f/6.txt, one per step.")
        self.assertTrue(result.ok, result.error)
        own = {answer for answer in answers if answer}
        continuations = self.requests()[1:]
        self.assertGreaterEqual(len(continuations), 2)
        for request in continuations:
            for message in request["conversation_history"]:
                if message["role"] == "assistant":
                    self.assertIn(message["content"], own, "only the model's own words")
                    self.assertNotIn("Brainstem Agent", message["content"])
                    self.assertNotIn(" -> ok", message["content"])
        # Grail's own stand-in for an empty completion is not the model's words either.
        second = continuations[0]
        self.assertFalse(any(GRAIL_FALLBACK in m["content"]
                             for m in second["conversation_history"]
                             if m["role"] == "assistant"))
        self.assertIn("Owner's request:", second["user_input"])

    @criteria("D1", "D3")
    def test_an_answer_that_writes_tool_calls_out_as_a_log_is_never_accepted(self):
        state = {"fabricated": 0}

        def policy_(request, results, tools=(), final=False):
            paths = written(request, results)
            if final:
                return "Now let me continue:"
            if len(paths) < 3:
                return [("write_file", {"path": f"chain/{len(paths) + 1}.txt",
                                        "content": str(len(paths) + 1)})]
            if len(paths) == 3 and not results and not state["fabricated"]:
                state["fabricated"] += 1
                return FAKE_LOG
            if len(paths) < 5:
                return [("write_file", {"path": f"chain/{len(paths) + 1}.txt",
                                        "content": str(len(paths) + 1)})]
            return "chain/1.txt to chain/5.txt hold 1 to 5; the final value is 5."
        events = []
        host = self.host(policy_)
        result = host.chat("Write chain/1.txt to chain/5.txt holding 1 to 5, one per step.",
                           progress=events.append)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(state["fabricated"], 1)
        for number in range(1, 6):  # the claimed writes really happened, later
            self.assertEqual((self.workspace / "chain" / f"{number}.txt").read_text(),
                             str(number))
        self.assertNotEqual(result.response["response"], FAKE_LOG)
        segments = result.evidence["long_turn"]["segments"]
        self.assertEqual([bool(s.get("imitated")) for s in segments], [False, True, False])
        journal = host.store.journal(host.namespace, result.turn_id)
        [rejected] = [s for s in journal["steps"] if s["kind"] == "segment" and s["seq"] == 2]
        self.assertTrue(rejected["result"]["imitated"])
        # The next step is told those calls did not run, and the fake log is never resent,
        # least of all in the model's own voice.
        third = self.requests()[2]
        self.assertIn("did not run", third["user_input"])
        self.assertNotIn("chain/5.txt\", \"content\": \"5\"} -> ok", third["user_input"])
        self.assertFalse(any("-> ok" in m["content"] for m in third["conversation_history"]
                             if m["role"] == "assistant"))
        self.assertIn("unreceipted-tool-log",
                      [e.get("reason") for e in events if e["event"] == "turn.continuing"])
        from brainstem_agent.cli import progress_line
        [line] = [progress_line(e) for e in events if e.get("reason") == "unreceipted-tool-log"]
        self.assertIn("not accepted", line)

    @criteria("D3")
    def test_a_fabricated_log_at_the_last_allowed_step_ends_partial_never_success(self):
        def policy_(request, results, tools=(), final=False):
            paths = written(request, results)
            if final:
                return "Now let me continue:"
            if len(paths) < 3:
                return [("write_file", {"path": f"chain/{len(paths) + 1}.txt",
                                        "content": str(len(paths) + 1)})]
            return FAKE_LOG
        host = self.host(policy_)
        result = host.chat("Write chain/1.txt to chain/5.txt.", budget=TurnBudget(max_segments=2))
        self.assertEqual((result.ok, result.state), (False, "partial"), result.error)
        self.assertIsNone(result.response)
        self.assertIn("no receipt", result.error)
        self.assertNotIn("The final value is 5", result.error)
        self.assertFalse((self.workspace / "chain" / "4.txt").exists())
        self.assertEqual(host.store.get_chat(host.namespace, result.turn_id).state, "failed")

    @criteria("D1")
    def test_an_honest_summary_of_calls_that_really_ran_is_accepted(self):
        summary = ("Here is what I did:\n"
                   "1. write_file {\"path\": \"a.txt\", \"content\": \"a\"} -> ok\n"
                   "2. read_file {\"path\": \"a.txt\"} -> ok\nBoth steps worked.")
        host = self.host(scripted_policy([[("write_file", {"path": "a.txt", "content": "a"})],
                                          [("read_file", {"path": "a.txt"})]], summary))
        result = host.chat("write a.txt and read it back")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.response["response"], summary)
        self.assertEqual(len(result.evidence["long_turn"]["segments"]), 1)

    @criteria("D4", "D5")
    def test_a_helper_that_fabricates_its_work_is_never_reported_as_done(self):
        faked = []

        def policy_(request, results, tools=(), final=False):
            text = request["user_input"]
            if final:
                return "continuing"
            if "HELPER" in text:
                if "did not run" not in text and not faked:
                    faked.append(1)
                    return "1. write_file {\"path\": \"par/h.txt\", \"content\": \"h\"} -> ok"
                if not results:
                    return [("write_file", {"path": "par/h.txt", "content": "h"})]
                return "helper wrote par/h.txt"
            if not results:
                return [("delegate_tasks", {"tasks": [{"task": "HELPER writes par/h.txt"}]})]
            return "joined: " + results[0]["content"][:500]
        host = self.host(policy_)
        result = host.chat("delegate one helper")
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.workspace / "par" / "h.txt").read_text(), "h")
        [child] = result.evidence["long_turn"]["children"]
        self.assertEqual((child["state"], child["segments"]), ("succeeded", 2))


# -- 3. Grail's logs ---------------------------------------------------------------------
def envelope_ok(logs):
    return (len(logs) <= DEFAULT_LIMITS.max_log_lines
            and all(len(line.encode()) <= DEFAULT_LIMITS.max_log_line_bytes for line in logs)
            and sum(len(line.encode()) for line in logs) <= DEFAULT_LIMITS.max_log_bytes)


class LogOverflowTests(Case):
    def read_turn(self, name, text, rounds=1):
        (self.workspace / name).write_text(text, encoding="utf-8")
        host = self.host(scripted_policy([[("read_file", {"path": name})]] * rounds,
                                         f"Read {name}."))
        return host.chat(f"read {name}")

    @criteria("D3", "D11")
    def test_a_huge_one_line_tool_result_never_fails_the_turn(self):
        result = self.read_turn("big.txt", "x" * 60_000)
        self.assertTrue(result.ok, result.error)
        logs = result.response["agent_logs"]
        self.assertTrue(envelope_ok(logs), [len(line) for line in logs][:5])
        self.assertTrue(any("more characters" in line for line in logs), logs[:3])

    @criteria("D3", "D11")
    def test_a_result_with_more_lines_than_the_envelope_keeps_the_newest_with_a_marker(self):
        result = self.read_turn("lines.txt", "".join(f"line {n}\n" for n in range(1, 401)),
                                rounds=2)
        self.assertTrue(result.ok, result.error)
        logs = result.response["agent_logs"]
        self.assertTrue(envelope_ok(logs))
        self.assertIn("line 400", logs[-1])
        self.assertTrue(logs[0].startswith("[Brainstem Agent:") and "omitted" in logs[0],
                        logs[0])

    @criteria("D3", "D11")
    def test_non_ascii_results_larger_than_an_sse_frame_never_fail_the_turn(self):
        result = self.read_turn("wide.txt", "é" * 60_000, rounds=2)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(envelope_ok(result.response["agent_logs"]))

    @criteria("D3")
    def test_trimming_is_deterministic_and_leaves_other_frames_alone(self):
        session = "s1"
        big = "[read_file] " + "y" * 30_000 + "\n" + "\n".join(f"row {n}" for n in range(500))
        chunks = [sse({"type": "delta", "text": "Reading"}).decode(),
                  sse({"type": "agent", "logs": big}).decode(),
                  sse({"type": "done", "response": "ok", "session_id": session,
                       "agent_logs": big, "voice_mode": False, "model": "m",
                       "requested_model": "m", "streamed": True}).decode()]
        first, second = longturn.bounded_stream(chunks), longturn.bounded_stream(chunks)
        self.assertEqual(first, second)
        self.assertEqual(first[0:2], ["data: " + json.dumps({"type": "delta",
                                                             "text": "Reading"}) + "\n", "\n"])
        # A stream split mid-line (the worker reads at most 1 MiB per line) is reassembled.
        joined = "".join(chunks)
        pieces = [joined[i:i + 4096] for i in range(0, len(joined), 4096)]
        self.assertEqual(longturn.bounded_stream(pieces), first)
        response = normalize_sse(first, session)
        self.assertTrue(envelope_ok(response["agent_logs"]))
        self.assertIn("row 499", response["agent_logs"][-1])

    @criteria("D3", "D11")
    def test_a_long_streamed_answer_never_fails_the_turn(self):
        class Chatty(GrailEmulator):
            def _loop(self, worker, request, grant):
                worker.bind(grant)
                for n in range(1500):  # Grail streams one delta per fragment
                    yield sse({"type": "delta", "text": f"w{n} "})
                yield done(request, "A long answer.")
        host = self.host(None, Chatty)
        result = host.chat("say a lot")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.response["response"], "A long answer.")


# -- 4. the soul's newline rule ----------------------------------------------------------
class SoulTests(unittest.TestCase):
    @criteria("D2", "A2")
    def test_files_that_will_receive_appended_lines_end_their_lines_with_a_newline(self):
        import brainstem_agent

        soul = (Path(brainstem_agent.__file__).parent / "data" / "soul.md").read_text()
        bullets = soul.split("\n- ")
        [rule] = [bullet for bullet in bullets if "trailing newline" in bullet]
        flat = " ".join(rule.split())
        # Cell v1 (A2): exact content, no trailing newline unless asked ...
        self.assertIn("Write file contents exactly as the user specifies", flat)
        self.assertIn("Do not add a trailing newline", flat)
        # ... except where lines will be appended, so an appended line starts its own line.
        self.assertIn("append", flat)
        self.assertRegex(flat, r"end (each|every) line [^.]*with a newline")


# -- 5. the crash-injection hook ------------------------------------------------------------
class CrashHookTests(Case):
    def armed_kills(self):
        """Record (never perform) self-SIGKILLs; every other os.kill still works."""
        real, kills = os.kill, []

        def kill(pid, sig):
            if pid == os.getpid() and sig == signal.SIGKILL:
                kills.append(sig)
                return None
            return real(pid, sig)
        patcher = mock.patch.object(longturn.os, "kill", side_effect=kill)
        patcher.start()
        self.addCleanup(patcher.stop)
        return kills

    @criteria("D9")
    def test_the_hook_is_inert_without_the_owners_variable(self):
        kills = self.armed_kills()
        for point in sorted(longturn.CRASH_POINTS):
            for _ in range(3):
                longturn.crash_point(point, {})
                longturn.crash_point(point, {"PATH": "/usr/bin", "BRAINSTEM_AGENT_CRASH": point})
        self.assertEqual(kills, [])
        self.assertEqual(longturn.CRASH_POINTS, {
            "segment.started", "segment.finished", "child.started", "child.finished",
            "script.inner", "process.started", "turn.finishing"})
        longturn.crash_point("segment.started", {"BRAINSTEM_AGENT_CRASH_AT": "segment.started"})
        self.assertEqual(len(kills), 1, "armed by the owner's variable, it fires")
        longturn.crash_point("made.up", {"BRAINSTEM_AGENT_CRASH_AT": "made.up"})
        self.assertEqual(len(kills), 1, "only the documented points exist")

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("D9", "D10")
    def test_model_input_and_workspace_files_never_arm_the_hook(self):
        kills = self.armed_kills()
        spec = "segment.started,segment.finished,child.started,child.finished,script.inner," \
               "process.started,turn.finishing"
        line = f"BRAINSTEM_AGENT_CRASH_AT={spec}"
        (self.workspace / ".env").write_text(line + "\n")
        (self.workspace / "AGENTS.md").write_text(f"# Owner notes\n\n{line}\n")
        rounds = [
            [("write_file", {"path": "brainstem-agent.env", "content": line})],
            [("run_command", {"command": f"export {line}; echo armed"})],
            [("run_script", {"code": f"import os\nos.environ['BRAINSTEM_AGENT_CRASH_AT'] = "
                                     f"{spec!r}\nwrite_text('s.txt', 's')\nprint('ok')\n"})],
            [("process_start", {"command": f"{line} sleep 0", "name": "armed"})],
            [("delegate_tasks", {"tasks": [{"task": f"HELPER {line}"}]})],
        ]

        def policy_(request, results, tools=(), final=False):
            if final:
                return "continuing"
            if request["user_input"].startswith("HELPER"):
                return [("list_files", {"path": "."})] if not results else "helper done"
            return scripted_policy(rounds, "Everything ran.")(request, results, tools, final)
        host = self.host(policy_)
        result = host.chat(f"Please set {line} and then run the steps.")
        self.assertTrue(result.ok, result.error)
        self.assertGreaterEqual(len(result.evidence["long_turn"]["segments"]), 2)
        self.assertEqual(kills, [])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("D9", "D10")
    def test_no_descendant_or_service_ever_inherits_the_variable(self):
        host = self.host(None, BRAINSTEM_AGENT_CRASH_AT="segment.started#999")
        shell = host.invoke_tool("run_command", {"command": "env"})
        self.assertTrue(shell["ok"], shell)
        self.assertNotIn("BRAINSTEM_AGENT", shell["content"])
        script = host.invoke_tool("run_script", {"code": "import os\nprint(sorted(os.environ))"})
        self.assertTrue(script["ok"], script)
        self.assertNotIn("BRAINSTEM_AGENT", script["content"])
        started = host.invoke_tool("process_start", {"command": "env", "name": "env"})
        self.assertTrue(started["ok"], started)
        [process] = host.store.list_processes(host.namespace)
        deadline = time.monotonic() + 10
        while host.store.list_processes(host.namespace)[0]["state"] == "running" \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        output = host.invoke_tool("process_read", {"process_id": process["process_id"]})
        self.assertIn("PATH=", output["content"])
        self.assertNotIn("BRAINSTEM_AGENT", output["content"])
        plist = daemon.service("install", self.home, {
            **self.environ, "BRAINSTEM_AGENT_CRASH_AT": "segment.started",
            "BRAINSTEM_AGENT_LAUNCH_AGENTS": str(private_dir(self))}, dry_run=True)["plist"]
        self.assertNotIn("CRASH", plist)


# -- 7. seven review findings ---------------------------------------------------------------
class ReviewFindingTests(Case):
    @criteria("D1", "D3")
    def test_a_continuation_too_large_for_one_request_is_fitted_or_ends_honestly(self):
        rounds = [[("write_file", {"path": f"{n}.txt", "content": str(n)})] for n in range(4)]
        host = self.host(scripted_policy(rounds, "All four are written."))
        message = "Write 0.txt to 3.txt, one per step. Context follows. " + "z" * 65_000
        grants = []
        issue = host.authority.issue
        host.authority.issue = lambda binding, **options: grants.append(
            issue(binding, **options)) or grants[-1]
        result = host.chat(message)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.evidence["long_turn"]["segments"]), 2)
        for request in self.requests():
            self.assertLessEqual(len(request["user_input"].encode()),
                                 DEFAULT_LIMITS.max_text_bytes)
        journal = host.store.journal(host.namespace, result.turn_id)
        self.assertEqual([s["state"] for s in journal["steps"]], ["succeeded"] * 3)
        for grant in grants:  # every segment's grant was revoked
            status, _ = self.workers[0].post("/v1/bind", {"grant": grant})
            self.assertEqual(status, 403)

    @criteria("A4", "D1")
    def test_a_session_longer_than_one_request_keeps_its_newest_history(self):
        host = self.host(scripted_policy([], "Fine."))
        first = host.chat("turn 0")
        self.assertTrue(first.ok, first.error)
        for number in range(1, 70):
            reservation = host.store.reserve_chat(host.namespace, f"turn {number}",
                                                  first.session_id, None)
            host.store.mark_chat_running(host.namespace, reservation.turn_id)
            host.store.finish_chat(host.namespace, reservation.turn_id, "succeeded", {
                "response": f"answer {number}", "agent_logs": [],
                "session_id": first.session_id})
        result = host.chat("the newest question", session_id=first.session_id)
        self.assertTrue(result.ok, result.error)
        history = self.requests()[-1]["conversation_history"]
        self.assertLessEqual(len(history), DEFAULT_LIMITS.max_history_messages)
        self.assertEqual(history[-1], {"role": "assistant", "content": "answer 69"})
        self.assertEqual(history[0]["role"], "user")
        journal = host.store.journal(host.namespace, result.turn_id)
        self.assertTrue(all(s["state"] == "succeeded" for s in journal["steps"]))

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("D6")
    def test_a_scripts_read_text_is_the_exact_file_or_an_error_never_a_part(self):
        (self.workspace / "a.txt").write_text("hello\nworld\n")
        (self.workspace / "u.txt").write_text("naïve ✓ café\n", encoding="utf-8")
        (self.workspace / "big.txt").write_text("q" * 100_000)
        (self.workspace / "bad.bin").write_bytes(b"ok\xff\xfe\n")
        host = self.host(None)
        code = ("for name in ('a.txt', 'u.txt'):\n"
                "    write_text('copy-' + name, read_text(name))\n"
                "for name in ('big.txt', 'bad.bin'):\n"
                "    try:\n"
                "        text = read_text(name)\n"
                "        print(name, 'returned', len(text))\n"
                "    except ToolError as error:\n"
                "        print(name, 'refused:', str(error)[:120])\n")
        result = host.invoke_tool("run_script", {"code": code})
        self.assertTrue(result["ok"], result)
        for name in ("a.txt", "u.txt"):
            self.assertEqual((self.workspace / f"copy-{name}").read_bytes(),
                             (self.workspace / name).read_bytes())
        self.assertIn("big.txt refused:", result["content"])
        self.assertIn("bad.bin refused:", result["content"])
        self.assertNotIn("big.txt returned", result["content"])
        self.assertNotIn("bad.bin returned", result["content"])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("D6", "D8")
    def test_a_scripts_inner_calls_end_with_the_script(self):
        from brainstem_agent.organs.files import FilesOrgan

        original = FilesOrgan.invoke

        def slow(organ, context, tool, arguments):
            if arguments.get("path") == "slow.txt":
                time.sleep(2.0)
            return original(organ, context, tool, arguments)
        (self.workspace / "slow.txt").write_text("s")
        host = self.host(None)
        with mock.patch.object(FilesOrgan, "invoke", slow):
            started = time.monotonic()
            result = host.invoke_tool("run_script", {
                "code": "read_text('slow.txt')\nwrite_text('after.txt', 'x')\n",
                "timeout_seconds": 1})
            elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["content"])
        self.assertLess(elapsed, 8)
        receipts = host.receipts(result["turn_id"])
        [outer] = [r for r in receipts if r["tool"] == "run_script"]
        inner = [r for r in receipts if r["tool"] != "run_script"]
        self.assertEqual([r["tool"] for r in inner], ["read_file"])  # nothing after the end
        self.assertTrue(all(r["state"] != "started" for r in receipts), receipts)
        self.assertLessEqual(inner[0]["finished_at"], outer["finished_at"])
        self.assertFalse((self.workspace / "after.txt").exists())

    @criteria("A8", "D3")
    def test_settling_reads_every_receipt_of_a_turn_not_the_first_hundred(self):
        rounds = [[("list_files", {"path": "."})] * 60,
                  [("list_files", {"path": "."})] * 45 + [
                      ("write_file", {"path": "late.txt", "content": "late"})]]
        host = self.host(scripted_policy(rounds, "Listed and wrote."))
        original = host.store.finish_receipt
        begun = {}
        begin = host.store.begin_receipt

        def remember(namespace, turn_id, call_id, tool, capability, request):
            receipt = begin(namespace, turn_id, call_id, tool, capability, request)
            begun[receipt] = tool
            return receipt

        def failing(receipt_id, state, result):
            if begun.get(receipt_id) == "write_file" and state in ("succeeded", "failed"):
                raise StateError("Fixture database transaction failed")
            return original(receipt_id, state, result)
        with mock.patch.object(host.store, "begin_receipt", remember), \
                mock.patch.object(host.store, "finish_receipt", failing):
            result = host.chat("list a lot, then write late.txt",
                               budget=TurnBudget(max_tool_calls=200))
        self.assertEqual((result.ok, result.state), (False, "uncertain"), result.error)
        self.assertIn("could not be recorded", result.error)
        self.assertEqual(len(result.evidence["receipts"]), 106)
        self.assertEqual((self.workspace / "late.txt").read_text(), "late")

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("C6", "D4", "D10")
    def test_forgetting_a_session_forgets_its_helpers_scripts_and_processes(self):
        def policy_(request, results, tools=(), final=False):
            text = request["user_input"]
            if final:
                return "continuing"
            if text.startswith("HELPER"):
                return [("write_file", {"path": "lemon.txt", "content": "zestylemon"})] \
                    if not results else "helper wrote zestylemon"
            return scripted_policy([
                [("delegate_tasks", {"tasks": [{"task": "HELPER zestylemon"}]})],
                [("run_script", {"code": "write_text('s.txt', 'zestylemon')\nprint('ok')"})],
                [("process_start", {"command": "echo zestylemon", "name": "zestylemon"})],
            ], "done with zestylemon")(request, results, tools, final)
        host = self.host(policy_)
        result = host.chat("do the zestylemon things")
        self.assertTrue(result.ok, result.error)
        host.forget_session(result.session_id)
        database = sqlite3.connect(host.home / "state" / "agent.sqlite3")
        self.addCleanup(database.close)
        for table in ("chats", "receipts", "turn_steps", "processes", "run_events"):
            for row in database.execute(f"SELECT * FROM {table}"):
                self.assertNotIn("zestylemon", json.dumps(row, default=str), table)

    @criteria("D3", "D10")
    def test_the_turn_time_limit_fits_inside_every_grants_lifetime(self):
        margin = 120
        self.assertLessEqual(LIMITS["max_seconds"] + margin, policy._LOCAL_TTL)
        self.assertLessEqual(LIMITS["child_seconds"] + margin, policy._LOCAL_TTL)
        host = self.host(scripted_policy([[("list_files", {"path": "."})]], "Listed."))
        ttls = []
        issue = host.authority.issue

        def recording(binding, *, ttl=60):
            ttls.append(ttl)
            return issue(binding, ttl=ttl)
        host.authority.issue = recording
        result = host.chat("list", budget=TurnBudget(max_seconds=LIMITS["max_seconds"]))
        self.assertTrue(result.ok, result.error)
        self.assertGreaterEqual(ttls[0], LIMITS["max_seconds"] + margin - 1)


if __name__ == "__main__":
    unittest.main()
