"""Real-core D1/D4/D6/D7/D9 specs: the long-horizon cell against unchanged Grail.

Skips unless BRAINSTEM_AGENT_REAL_CORE=1. No inference: Grail runs without a credential,
or its unchanged code is imported from a verified copy with the model call replaced by a
test double in that test process only (the product never does this).
"""

import json
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from acceptance_support import (REAL_CORE, RUNTIME, criteria, group_exists, kill_quietly,
                                prepared_cache, private_dir, record_metric, wait_until,
                                write_token_file)
from brainstem_agent.adapter import DEFAULT_LIMITS, CoreContractError, normalize_sse
from brainstem_agent.longturn import bounded_stream, segment_rounds

HERE = Path(__file__).resolve().parent

# Unchanged Grail's /chat/stream with a scripted model: it asks for a tool in every round
# it may (or stops after STOP rounds). The cell reads the stream it produces.
GRAIL_ROUNDS = r"""
import json, os, sys
sys.path.insert(0, ".")
import brainstem

stop_after = int(sys.argv[1])
calls = []

def model(messages, tools=None, model=None):
    calls.append(bool(tools))
    wants_tool = tools and len(calls) <= stop_after
    if wants_tool:
        n = len(calls)
        yield ("done", {"message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{n}", "type": "function",
            "function": {"name": "step", "arguments": json.dumps({"n": n})}}]},
            "model": "double", "finish_reason": "tool_calls"})
    else:
        text = "Now let me continue:" if not tools else "Finished."
        yield ("delta", text)
        yield ("done", {"message": {"role": "assistant", "content": text}, "model": "double",
                        "finish_reason": "stop"})

brainstem.call_copilot_stream = model
client = brainstem.app.test_client()
response = client.post("/chat/stream", json={"user_input": "go", "session_id": "s1",
                                              "conversation_history": []})
print("REPORT " + json.dumps({"body": response.get_data(as_text=True), "tools": calls}))
"""
STEP_AGENT = '''from agents.basic_agent import BasicAgent


class StepAgent(BasicAgent):
    def __init__(self):
        super().__init__(name="step", metadata={
            "name": "step", "description": "One step.",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}},
                           "required": ["n"]}})

    def perform(self, **arguments):
        return "step %s done" % arguments.get("n")
'''
# The same tool with a large result: one 60,000-character line, 400 short lines and a wide
# (non-ASCII) line, which unchanged Grail copies into its agent frames and agent_logs.
BIG_AGENT = STEP_AGENT.replace(
    'return "step %s done" % arguments.get("n")',
    'return ("x" * 60000 + "\\n" + "\\n".join("row %d" % i for i in range(400))\n'
    '                + "\\n" + "\\u00e9" * 30000 + " end %s" % arguments.get("n"))')

# A host whose helpers run on real, credential-less Grail workers; it is SIGKILLed right
# after the first helper worker started (the second is starting).
HELPER_HOST = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from brainstem_agent.host import AgentHost
home, workspace, cache = map(Path, sys.argv[2:5])
holder = {}

def real_worker(**options):
    return holder["host"]._grail_worker(**{**options, "credential": None})

host = AgentHost(home, workspace=workspace, cache=cache, environ=dict(os.environ),
                 worker_factory=real_worker)
holder["host"] = host
print(json.dumps({"watchdog": None}), flush=True)
with host.exclusive():
    host.invoke_tool("delegate_tasks", {"tasks": [{"task": "HELPER a"}, {"task": "HELPER b"}]})
print("NOT KILLED", flush=True)
"""


def processes_under(path: Path) -> list[int]:
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True,
                             text=True).stdout.splitlines()
    return [int(line.split(None, 1)[0]) for line in listing
            if str(path) in line and "ps -axo" not in line]


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start the real Grail process")
class RealLongTurnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    def grail_copy(self) -> Path:
        from brainstem_agent import grail

        source = grail.ensure_grail_source(self.cache, fetch=False)
        copy = private_dir(self) / "rapp_brainstem"
        for relative in source.inventory:
            (copy / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.root / relative, copy / relative)
        (copy / ".env").write_text("")
        self.source = source
        return copy

    def stream_of(self, stop_after: int, agent: str = STEP_AGENT) -> tuple[str, list]:
        from brainstem_agent import grail

        copy = self.grail_copy()
        agents = private_dir(self)
        (agents / "step_agent.py").write_text(agent)
        result = subprocess.run(
            [str(grail.worker_venv_python(self.cache)), "-I", "-c", GRAIL_ROUNDS,
             str(stop_after)], cwd=copy, capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": str(copy.parent), "LANG": "en_US.UTF-8",
                 "PYTHONDONTWRITEBYTECODE": "1", "AGENTS_PATH": str(agents),
                 "SOUL_PATH": str(copy / "soul.md")})
        lines = [line for line in result.stdout.splitlines() if line.startswith("REPORT ")]
        self.assertTrue(lines, result.stderr[-800:])
        report = json.loads(lines[-1][7:])
        grail.verify_tree(copy, self.source.inventory, allow_extra=True)  # still unchanged
        return report["body"], report["tools"]

    @criteria("D1")
    def test_the_cell_detects_unchanged_grails_round_limit_from_its_stream(self):
        body, tools = self.stream_of(stop_after=10)
        lines = body.splitlines()
        rounds, logs = segment_rounds(lines)
        self.assertEqual(rounds, 3)
        self.assertEqual(tools, [True, True, True, False], "3 tool rounds, then tools off")
        self.assertEqual(logs, ["[step] step 1 done", "[step] step 2 done", "[step] step 3 done"])
        done = json.loads([line for line in lines if '"done"' in line][-1][5:])
        self.assertEqual(done["response"], "Now let me continue:")
        body, tools = self.stream_of(stop_after=2)
        rounds, _ = segment_rounds(body.splitlines())
        self.assertEqual((rounds, tools), (2, [True, True, True]))
        record_metric("d1_grail_round_limit", 3)

    @criteria("D3", "D11")
    def test_unchanged_grails_logs_of_large_results_are_bounded_never_refused(self):
        body, tools = self.stream_of(stop_after=10, agent=BIG_AGENT)
        chunks = body.splitlines(keepends=True)
        self.assertEqual(tools, [True, True, True, False])
        with self.assertRaises(CoreContractError):
            normalize_sse(chunks, "s1")  # Grail's raw logs break the RAPP/1 envelope
        bounded = bounded_stream(chunks)
        self.assertEqual(bounded, bounded_stream(chunks), "deterministic")
        response = normalize_sse(bounded, "s1")
        self.assertEqual(response["response"], "Now let me continue:")
        self.assertEqual(segment_rounds(bounded)[0], 3, "every round is still counted")
        logs = response["agent_logs"]
        self.assertLessEqual(len(logs), DEFAULT_LIMITS.max_log_lines)
        self.assertTrue(all(len(line.encode()) <= DEFAULT_LIMITS.max_log_line_bytes
                            for line in logs))
        self.assertLessEqual(sum(len(line.encode()) for line in logs),
                             DEFAULT_LIMITS.max_log_bytes)
        self.assertTrue(logs[0].startswith("[Brainstem Agent:") and "omitted" in logs[0])
        self.assertIn("more characters", logs[-1])
        record_metric("d3_real_grail_log_lines_kept", len(logs))

    @criteria("D4", "D6", "D7", "D10")
    def test_unchanged_grail_loads_every_long_turn_tool_for_a_grant(self):
        from brainstem_agent.host import (CORE_CAPABILITIES, DEFAULT_CAPABILITIES, OWNER,
                                          TURN_CAPABILITIES, AgentHost)
        from brainstem_agent.policy import RunBinding

        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(write_token_file(private_dir(self))),
                   "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(private_dir(self))}
        host = AgentHost(private_dir(self), workspace=private_dir(self), cache=self.cache,
                         environ=environ)
        self.addCleanup(host.close)
        worker = host._grail_worker(worker_id="real", broker_url=host.broker.url,
                                    credential=None, model="auto",
                                    register=host.broker.register_worker)
        worker.start()
        self.addCleanup(worker.stop)

        def advertised(capabilities):
            grant = host.authority.issue(RunBinding(
                OWNER, str(host.workspace), "s", "t" + str(len(capabilities)), worker.worker_id,
                worker.generation, tuple(capabilities)), ttl=60)
            return sorted(worker.health({"X-Brainstem-Agent-Grant": grant})["agents"])
        everything = advertised(TURN_CAPABILITIES)
        for tool in ("delegate_tasks", "run_script", "process_start", "process_status",
                     "process_read", "process_write", "process_stop"):
            self.assertIn(tool, everything)
        self.assertEqual(everything, sorted(s.name for s in host.broker.tool_specs(
            TURN_CAPABILITIES)), "Grail validated and loaded every tool")
        self.assertNotIn("delegate_tasks", advertised(DEFAULT_CAPABILITIES))
        core = advertised(CORE_CAPABILITIES)
        self.assertNotIn("delegate_tasks", core)
        self.assertNotIn("run_script", core)
        stopped = worker.stop()
        self.assertTrue(stopped["integrity_after"]["ok"], stopped["integrity_after"])

    @criteria("D9", "D5")
    def test_sigkill_of_a_host_with_real_helper_workers_leaves_nothing_behind(self):
        home, workspace = private_dir(self), private_dir(self)
        token = write_token_file(private_dir(self))
        env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "HOME": str(private_dir(self)),
               "BRAINSTEM_HOME": str(private_dir(self)),
               "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token),
               "BRAINSTEM_AGENT_CRASH_AT": "child.started#1"}
        process = subprocess.Popen(
            [sys.executable, "-c", HELPER_HOST, str(RUNTIME), str(home), str(workspace),
             str(self.cache)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            kill_quietly(process.pid)
            stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, -signal.SIGKILL, stderr[-600:])
        self.assertNotIn("NOT KILLED", stdout)
        started = time.monotonic()
        from longturn_support import recorded_groups
        workers = [pid for _kind, pid in recorded_groups(home, ("worker",))]
        self.assertGreaterEqual(len(workers), 1, "the dead host recorded its helpers' workers")
        self.assertTrue(wait_until(lambda: not any(group_exists(pid) for pid in workers), 5),
                        f"left running: {[pid for pid in workers if group_exists(pid)]}")
        self.assertEqual(processes_under(home), [])
        record_metric("d9_real_helper_orphans_gone_seconds", round(time.monotonic() - started, 3))
        from brainstem_agent.host import AgentHost

        host = AgentHost(home, workspace=workspace, cache=self.cache,
                         environ={key: value for key, value in env.items()
                                  if key != "BRAINSTEM_AGENT_CRASH_AT"})
        self.addCleanup(host.close)
        with host.exclusive():
            pass
        self.assertTrue(any(entry["kind"] == "worker" for entry in host.recovered),
                        host.recovered)
        receipts = host.receipts()
        [delegate] = [r for r in receipts if r["tool"] == "delegate_tasks"]
        self.assertEqual(delegate["state"], "uncertain")
        steps = host.store.journal(host.namespace, delegate["turn_id"])["steps"]
        children = [s for s in steps if s["kind"] == "child"]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(s["state"] == "failed" for s in children), children)
        self.assertEqual(list((home / "workers").iterdir()) if (home / "workers").exists()
                         else [], [])
        listed = host.invoke_tool("list_files", {"path": "."})  # the next command works
        self.assertTrue(listed["ok"], listed)


if __name__ == "__main__":
    unittest.main()
