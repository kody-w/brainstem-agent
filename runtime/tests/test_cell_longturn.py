"""D1/D2/D3/D8/D10 unit specs: long turns continue across Grail requests (segments).

The fake worker is ``GrailEmulator``: Grail's own loop (three tool rounds, then a forced
tools-disabled answer) around a scripted model, calling the host's real broker and organs.
Everything else (store, journal, grants, receipts, budgets, governance) is the product.
"""

import json
import threading
import time
import unittest

from acceptance_support import criteria, private_dir, record_metric, write_token_file
from brainstem_agent.host import AgentHost
from brainstem_agent.longturn import TurnBudget, continuation_input, journal_text
from longturn_support import chain_policy, factory_for, journal_calls, scripted_policy

CHAIN = ("Create chain/1.txt containing 1. Then repeatedly read the latest file and write the "
         "next file containing the value plus one, up to chain/6.txt. Then report the final "
         "value.")
GREET = ("Create notes/greet.txt containing the word hello, then use the shell to append a "
         "second line containing world, then show me the file. After that, save how you did "
         "this as a skill called greet-file.")


class LongTurnCase(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        self.workers = []

    def host(self, policy, **environ):
        host = AgentHost(self.home, workspace=self.workspace,
                         environ={**self.environ, **environ},
                         worker_factory=factory_for(policy, self.workers))
        self.addCleanup(host.close)
        return host

    def requests(self):
        return [request for worker in self.workers for request in worker.requests]


class ContinuationTests(LongTurnCase):
    @criteria("D1", "D8", "D12")
    def test_a_six_step_dependent_chain_finishes_in_one_command_across_segments(self):
        events = []
        host = self.host(chain_policy())
        started = time.monotonic()
        result = host.chat(CHAIN, progress=events.append)
        self.assertTrue(result.ok, result.error)
        for number in range(1, 7):
            self.assertEqual((self.workspace / "chain" / f"{number}.txt").read_text(),
                             str(number))
        self.assertIn("6", result.response["response"])
        segments = result.evidence["long_turn"]["segments"]
        self.assertEqual(len(segments), 4)  # 11 dependent calls, 3 rounds per Grail request
        self.assertEqual([s["rounds"] for s in segments], [3, 3, 3, 2])
        self.assertEqual([s["exhausted"] for s in segments], [True, True, True, False])
        self.assertEqual(len(self.requests()), 4)
        self.assertEqual(len(self.workers), 1, "every segment ran on the same warm worker")
        # Every Grail request is journaled as a segment of this one turn.
        journal = host.store.journal(host.namespace, result.turn_id)
        steps = [(s["kind"], s["seq"], s["state"]) for s in journal["steps"]]
        self.assertEqual(steps, [("turn", 0, "succeeded")] + [("segment", n, "succeeded")
                                                              for n in range(1, 5)])
        self.assertEqual([r["tool"] for r in journal["receipts"]],
                         ["write_file"] + ["read_file", "write_file"] * 5)
        self.assertTrue(all(r["state"] == "succeeded" for r in journal["receipts"]))
        self.assertEqual(result.evidence["grail_calls"], 4)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("segment.started"), 4)
        self.assertEqual(kinds.count("turn.continuing"), 3)
        self.assertEqual(kinds[-1], "turn.finished")
        self.assertTrue(all(isinstance(s["seconds"], float) and s["started_at"] > 0
                            for s in segments), "per-segment timing for the evidence")
        record_metric("d1_unit_segments", len(segments))
        record_metric("d1_unit_seconds", round(time.monotonic() - started, 3))

    @criteria("D1")
    def test_the_continuation_resends_the_request_and_the_whole_journal_not_the_calls(self):
        host = self.host(chain_policy())
        result = host.chat(CHAIN)
        self.assertTrue(result.ok, result.error)
        first, second, third = self.requests()[:3]
        self.assertEqual(first["user_input"], CHAIN)
        self.assertTrue(second["user_input"].startswith("[Brainstem Agent continuation: step 2"))
        self.assertIn(CHAIN, second["user_input"])
        self.assertEqual([tool for tool, _ in journal_calls(second)],
                         ["write_file", "read_file", "write_file"])
        self.assertEqual(len(journal_calls(third)), 6, "every earlier segment is resent")
        self.assertEqual(second["conversation_history"][-2:],
                         [{"role": "user", "content": CHAIN},
                          {"role": "assistant", "content": "Now let me continue with the next "
                                                           "file:"}])
        self.assertTrue(all(m["role"] in ("user", "assistant")
                            for m in second["conversation_history"]))
        # The session keeps the owner's words and the final answer only.
        self.assertEqual(host.store.history(host.namespace, result.session_id),
                         [{"role": "user", "content": CHAIN},
                          {"role": "assistant", "content": result.response["response"]}])

    @criteria("D1", "D10")
    def test_every_segment_gets_a_fresh_grant_with_exactly_the_turn_capabilities(self):
        seen = []

        def policy(request, results, tools=(), final=False):
            seen.append(tuple(sorted(tools)))
            return scripted_policy([[("list_files", {"path": "."})]] * 4)(
                request, results, tools, final)
        host = self.host(policy)
        result = host.chat("list four times", capabilities=["files.read"])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.evidence["long_turn"]["segments"]), 2)
        grants = self.workers[0].grants
        self.assertEqual(len(grants), 2)
        self.assertNotEqual(grants[0], grants[1])
        self.assertTrue(all(tools in ((), ("list_files", "read_file")) for tools in seen), seen)
        for grant in grants:  # both revoked once their segment ended
            status, _ = self.workers[0].post("/v1/bind", {"grant": grant})
            self.assertEqual(status, 403)

    @criteria("D1")
    def test_a_request_that_ends_before_the_round_limit_is_one_segment(self):
        host = self.host(scripted_policy([[("write_file", {"path": "a.txt", "content": "a"})]],
                                         "Wrote a.txt."))
        result = host.chat("write a")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.evidence["long_turn"]["segments"]), 1)
        self.assertEqual(result.response["response"], "Wrote a.txt.")

    @criteria("D1")
    def test_exactly_three_needed_rounds_cost_one_confirming_segment(self):
        rounds = [[("write_file", {"path": f"{n}.txt", "content": str(n)})] for n in range(3)]
        host = self.host(scripted_policy(rounds, "All three are written."))
        result = host.chat("write three files")
        self.assertTrue(result.ok, result.error)
        self.assertEqual([s["rounds"] for s in result.evidence["long_turn"]["segments"]], [3, 0])
        self.assertEqual(len(host.receipts(result.turn_id)), 3, "no call was repeated")

    @criteria("D1", "D9")
    def test_a_replayed_long_turn_never_calls_grail_again(self):
        host = self.host(chain_policy())
        first = host.chat(CHAIN, idempotency_key="k1")
        self.assertTrue(first.ok, first.error)
        before = len(self.requests())
        again = host.chat("something else", idempotency_key="k1")
        self.assertTrue(again.replayed)
        self.assertEqual(again.response, first.response)
        self.assertEqual(len(self.requests()), before)


class LearningRequestTests(LongTurnCase):
    @criteria("D2")
    def test_the_lead_request_saves_the_skill_after_its_three_tool_rounds(self):
        greet = scripted_policy([
            [("write_file", {"path": "notes/greet.txt", "content": "hello"})],
            [("run_command", {"command": "printf '\\nworld' >> notes/greet.txt"})],
            [("read_file", {"path": "notes/greet.txt"})],
            [("skill_save", {"name": "greet-file", "description": "Create a greeting file",
                             "when_to_use": "When asked for a greeting file",
                             "steps": ["write_file notes/greet.txt with hello",
                                       "append world with the shell", "read it back"]})]],
            "Saved the greet-file skill.", final_text="Now let me save this as a skill:")
        host = self.host(greet)
        result = host.chat(GREET)
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.workspace / "notes" / "greet.txt").read_text(), "hello\nworld")
        self.assertEqual([s["rounds"] for s in result.evidence["long_turn"]["segments"]], [3, 1])
        [skill] = host.store.list_skills([host.namespace])
        self.assertEqual(skill["name"], "greet-file")
        # Tainted (it read a file and shell output) but the owner asked for exactly this
        # skill in the original words: unreviewed and offered, not quarantined.
        self.assertEqual((skill["review"], skill["tainted"]), ("unreviewed", True))
        self.assertEqual(skill["turn_id"], result.turn_id)


class BudgetTests(LongTurnCase):
    @criteria("D3")
    def test_the_segment_limit_ends_with_an_explicit_partial_result(self):
        host = self.host(chain_policy())
        result = host.chat(CHAIN, budget=TurnBudget(max_segments=2), idempotency_key="p1")
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "partial")
        self.assertIsNone(result.response)
        self.assertEqual(result.partial["limit"], "segments")
        self.assertIn("Partial result", result.error)
        self.assertIn("Done (6 tool calls succeeded)", result.error)
        self.assertIn("Not done", result.error)
        self.assertIn("chain/3.txt", result.error)
        self.assertEqual(len(self.requests()), 2)
        self.assertFalse((self.workspace / "chain" / "4.txt").exists())
        # The last allowed step was told it was the last.
        self.assertIn("This is the last step", self.requests()[1]["user_input"])
        chat = host.store.get_chat(host.namespace, result.turn_id)
        self.assertEqual(chat.state, "failed")  # never success in the store either
        self.assertEqual(host.store.get_step("journal_" + result.turn_id)["state"], "partial")
        self.assertEqual(host.store.history(host.namespace, result.session_id), [])
        replay = host.chat("again", idempotency_key="p1")  # reported partial, never re-run
        self.assertEqual((replay.replayed, replay.state), (True, "partial"))
        self.assertEqual(replay.partial["limit"], "segments")
        self.assertEqual(len(self.requests()), 2)

    @criteria("D3")
    def test_the_tool_call_limit_refuses_further_calls_and_ends_partial(self):
        host = self.host(chain_policy())
        result = host.chat(CHAIN, budget=TurnBudget(max_tool_calls=4))
        self.assertEqual(result.state, "partial")
        self.assertEqual(result.partial["limit"], "tool_calls")
        receipts = host.receipts(result.turn_id)
        self.assertEqual([r["state"] for r in receipts].count("succeeded"), 4)
        self.assertEqual(receipts[4]["state"], "denied")
        self.assertIn("limit", receipts[4]["result"]["reason"])
        self.assertFalse((self.workspace / "chain" / "4.txt").exists())

    @criteria("D3")
    def test_the_wall_time_limit_stops_the_segment_and_ends_partial(self):
        def slow(request, results, tools=(), final=False):
            if final:
                return "continuing"
            if not results:
                return [("write_file", {"path": "a.txt", "content": "a"})]
            time.sleep(0.2)
            return [("run_command", {"command": "sleep 20"})]
        host = self.host(slow)
        started = time.monotonic()
        result = host.chat("slow", budget=TurnBudget(max_seconds=2))
        self.assertLess(time.monotonic() - started, 9)
        self.assertEqual(result.state, "partial", result.error)
        self.assertEqual(result.partial["limit"], "seconds")
        self.assertIn("run_command", result.partial["uncertain_calls"])
        self.assertIn("uncertain", result.error)

    @criteria("D3")
    def test_budget_defaults_are_documented_and_bounded(self):
        budget = TurnBudget.from_env({})
        self.assertEqual((budget.max_segments, budget.max_tool_calls, budget.max_seconds),
                         (8, 100, 900.0))
        self.assertEqual(TurnBudget.from_env({"BRAINSTEM_AGENT_MAX_SEGMENTS": "3"}).max_segments,
                         3)
        for bad in ({"max_segments": 0}, {"max_segments": 99}, {"max_seconds": -1},
                    {"max_depth": 9}):
            with self.assertRaises(ValueError):
                TurnBudget(**bad)


class CancellationTests(LongTurnCase):
    @criteria("D8")
    def test_cancel_during_a_continuation_stops_it_within_five_seconds(self):
        cancel = threading.Event()

        def policy(request, results, tools=(), final=False):
            if final:
                return "continuing"
            if "continuation" in request["user_input"] and results:
                cancel.set()
                return [("run_command", {"command": "sleep 30"})]
            return [("list_files", {"path": "."})]
        host = self.host(policy)
        started = [0.0]

        def mark():
            cancel.wait(30)
            started[0] = time.monotonic()
        threading.Thread(target=mark, daemon=True).start()
        result = host.chat("go", cancel_event=cancel)
        elapsed = time.monotonic() - started[0]
        self.assertEqual(result.state, "cancelled")
        self.assertLess(elapsed, 5.0)
        self.assertIn("uncertain", result.error)
        segments = [s for s in host.store.journal(host.namespace, result.turn_id)["steps"]
                    if s["kind"] == "segment"]
        self.assertEqual([s["state"] for s in segments], ["succeeded", "cancelled"])
        self.assertTrue(all(r["state"] != "started" for r in host.receipts(result.turn_id)))
        record_metric("d8_unit_cancel_seconds", round(elapsed, 3))


class DaemonCliTests(LongTurnCase):
    """The real CLI and a real daemon process (its workers follow a scripted policy)."""

    def start_daemon(self, scenario):
        import os
        import subprocess
        import sys
        from pathlib import Path

        from acceptance_support import RUNTIME, kill_quietly, wait_until
        from brainstem_agent import daemon

        here = Path(__file__).resolve().parent
        self.env = {**self.environ, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
                    "PYTHONPATH": os.pathsep.join([str(RUNTIME), str(here)]),
                    "BRAINSTEM_AGENT_HOME": str(self.home)}
        process = subprocess.Popen(
            [sys.executable, str(here / "longturn_support.py"), str(self.home),
             str(self.workspace), scenario, "--daemon"], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(kill_quietly, process.pid)
        self.assertTrue(wait_until(lambda: daemon.read_record(self.home) is not None, 30))
        return process

    def stop_daemon(self, process):
        from acceptance_support import run_cli
        run_cli(["stop", "--json"], self.env, timeout=60)
        process.wait(30)

    @criteria("D8", "D5")
    def test_ctrl_c_on_the_cli_stops_every_helper_through_the_daemon_within_five_seconds(self):
        import signal
        import subprocess
        import sys

        from acceptance_support import group_exists, wait_until
        from longturn_support import recorded_groups
        daemon_process = self.start_daemon("children")
        chat = subprocess.Popen([sys.executable, "-m", "brainstem_agent", "chat", "Delegate.",
                                 "--json", "--workspace", str(self.workspace)], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        par = self.workspace / "par"
        self.assertTrue(wait_until(lambda: (par / "a.txt").exists() and (par / "b.txt").exists()
                                   and len(recorded_groups(self.home, ("shell",))) == 2, 30))
        shells = [pid for _kind, pid in recorded_groups(self.home, ("shell",))]
        self.assertTrue(all(group_exists(pid) for pid in shells))
        time.sleep(0.3)
        started = time.monotonic()
        chat.send_signal(signal.SIGINT)
        # The bound is on the effect (every helper's group gone), observed directly; the
        # CLI's own exit afterwards may take longer under load (interpreter teardown).
        gone = wait_until(lambda: not any(group_exists(pid) for pid in shells), 5,
                          interval=0.02)
        elapsed = time.monotonic() - started
        self.assertTrue(gone, [pid for pid in shells if group_exists(pid)])
        self.assertLess(elapsed, 5.0)
        stdout, stderr = chat.communicate(timeout=60)
        self.assertEqual(chat.returncode, 4, stderr[-500:])
        document = json.loads(stdout)
        self.assertEqual(document["state"], "cancelled")
        events = [json.loads(line)["event"] for line in stderr.splitlines()
                  if line.startswith("{")]
        self.assertIn("child.started", events)  # progress came through the daemon
        record_metric("d8_cli_sigint_helpers_gone_seconds", round(elapsed, 3))
        self.stop_daemon(daemon_process)

    @criteria("D8", "D7")
    def test_a_background_process_outlives_its_turn_in_the_daemon_and_stop_ends_it(self):
        from acceptance_support import cli_json, group_exists, run_cli, wait_until
        daemon_process = self.start_daemon("process")
        result = run_cli(["chat", "Start it.", "--json", "--workspace", str(self.workspace),
                          "--max-seconds", "4"], self.env, timeout=60)
        listed = cli_json(run_cli(["processes", "--workspace", str(self.workspace), "--json"],
                                  self.env))
        [record] = listed["processes"]
        self.assertEqual(record["state"], "running", (result.stdout[-300:], record))
        status = cli_json(run_cli(["status", "--json"], self.env))
        self.assertEqual([p["process_id"] for p in status["processes"]], [record["process_id"]])
        self.stop_daemon(daemon_process)
        self.assertTrue(wait_until(lambda: not group_exists(record["pid"]), 5))
        listed = cli_json(run_cli(["processes", "--workspace", str(self.workspace), "--json"],
                                  self.env))
        self.assertEqual(listed["processes"][0]["state"], "stopped")


class ProgressTextTests(unittest.TestCase):
    @criteria("D8")
    def test_progress_events_have_human_lines_and_json_lines(self):
        import contextlib
        import io
        import types

        from brainstem_agent.cli import _progress_sink, progress_line
        self.assertEqual(progress_line({"event": "segment.started", "segment": 2,
                                        "continuation": True}),
                         "step 2 started (continuing: Grail ran out of tool rounds)")
        self.assertEqual(progress_line({"event": "child.finished", "index": 1,
                                        "state": "succeeded", "seconds": 3.2}),
                         "helper 1 succeeded in 3.2s")
        self.assertEqual(progress_line({"event": "tool.finished", "tool": "write_file",
                                        "ok": True, "inner_of": "call_1"}),
                         "  (script) write_file ok")
        self.assertIsNone(progress_line({"event": "turn.started"}))
        for as_json, expected in ((True, '{"event": "limit.reached", "limit": "segments"}'),
                                  (False, "· limit reached: segments")):
            buffer = io.StringIO()
            sink = _progress_sink(types.SimpleNamespace(json=as_json, quiet=False))
            with contextlib.redirect_stderr(buffer):
                sink({"event": "limit.reached", "limit": "segments"})
            self.assertEqual(buffer.getvalue().strip(), expected)
        self.assertIsNone(_progress_sink(types.SimpleNamespace(json=True, quiet=True)))


class ToolSurfaceTests(LongTurnCase):
    @criteria("D4", "D6", "D7")
    def test_every_long_turn_tool_requires_an_argument_unchanged_grail_accepts(self):
        from brainstem_agent.host import ALL_CAPABILITIES, DEFAULT_CAPABILITIES, TURN_CAPABILITIES
        host = self.host(scripted_policy([]))
        specs = host.broker.tool_specs(ALL_CAPABILITIES)
        names = {spec.name for spec in specs}
        self.assertLessEqual({"delegate_tasks", "run_script", "process_start", "process_status",
                              "process_read", "process_write", "process_stop"}, names)
        for spec in specs:
            with self.subTest(tool=spec.name):
                wire = spec.to_wire()["parameters"]
                self.assertTrue(wire.get("required"), spec.name)
                self.assertLessEqual(set(wire["required"]), set(wire["properties"]))
        # An owner chat turn gets the long-turn tools; an unattended schedule only on request.
        self.assertEqual(TURN_CAPABILITIES[:len(DEFAULT_CAPABILITIES)], DEFAULT_CAPABILITIES)
        self.assertNotIn("agents.delegate", DEFAULT_CAPABILITIES)


class MigrationTests(unittest.TestCase):
    @criteria("D11")
    def test_a_cycle_three_store_gains_the_journal_tables_in_place(self):
        import os
        import sqlite3

        from brainstem_agent import state
        path = private_dir(self) / "agent.sqlite3"
        connection = sqlite3.connect(path)
        for name, statement in state._SCHEMA.items():
            if name in state._V2L_TABLES:
                connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {state._APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 2")
        connection.execute("INSERT INTO facts VALUES ('fact_1', 'ns', 'kept', 1, 1, NULL)")
        connection.commit()
        connection.close()
        os.chmod(path, 0o600)
        with state.Store(path) as store:
            self.assertEqual([f["text"] for f in store.list_facts("ns")], ["kept"])
            step = store.begin_step("ns", "turn_1", "turn", 0, {"budget": {}})
            store.finish_step(step, "partial", {"limit": "segments"})
            self.assertEqual(store.get_step(step)["state"], "partial")
            store.create_process("ns", "proc_1", turn_id="turn_1", call_id="c1", name="n",
                                 command="sleep 1")
            self.assertEqual(store.list_processes("ns")[0]["state"], "starting")

    @criteria("D11")
    def test_the_earlier_milestones_suites_are_still_part_of_discovery(self):
        import importlib

        import run_acceptance
        for name in (*run_acceptance.PREEXISTING, *run_acceptance.ALWAYS_ON,
                     *run_acceptance.LEARNING):
            importlib.import_module(name)
        self.assertEqual(set(run_acceptance.LONG_HORIZON) & set(run_acceptance.LEARNING), set())


class EvidenceTests(unittest.TestCase):
    @criteria("D12")
    def test_the_evidence_runner_traces_every_d_criterion_and_its_transcripts(self):
        import run_acceptance
        for number in range(1, 13):
            self.assertIn(f"D{number}", run_acceptance.CRITERIA)
        self.assertEqual(run_acceptance.CRITERIA["D1"][1], "live")
        self.assertEqual(run_acceptance.CRITERIA["D9"][1], "real-core")
        self.assertEqual(run_acceptance.evidence_class("test_live_longturn"), "live")
        self.assertEqual(run_acceptance.evidence_class("test_real_longturn"), "real-core")


class JournalTextTests(unittest.TestCase):
    @criteria("D1")
    def test_the_journal_shortens_the_oldest_results_first_and_never_drops_a_call(self):
        calls = [{"tool": "read_file", "arguments": {"path": f"f{n}.txt"}, "ok": True,
                  "content": "x" * 1400} for n in range(40)]
        text = journal_text(calls, limit=8000)
        self.assertLessEqual(len(text), 8000)
        self.assertEqual(text.count("read_file"), 40)
        self.assertIn("xxxx", text.splitlines()[-1])
        request = continuation_input("do it", calls, "next", segment=8, max_segments=8)
        self.assertIn("This is the last step", request)
        self.assertLess(len(request.encode()), 64 * 1024)


if __name__ == "__main__":
    unittest.main()
