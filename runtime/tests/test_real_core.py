"""Real-core acceptance: the unchanged Grail process, without inference.

Skips unless BRAINSTEM_AGENT_REAL_CORE=1. Uses a hash-locked worker venv and a
verified Grail cache prepared once through the public ``setup`` command.
"""

import json
import os
import secrets
import time
import unittest
import urllib.error
import urllib.request

from acceptance_support import (
    CELL_TOOLS,
    KERNEL_SHA256,
    PINNED_COMMIT,
    REAL_CORE,
    base_env,
    cli_json,
    criteria,
    leaks,
    prepared_cache,
    private_dir,
    real_credential_needles,
    record_metric,
    run_cli,
    write_token_file,
)

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def process_gone(pid, wait=5.0):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start the real Grail process")
class RealCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)

    def env(self, **extra):
        return base_env(self.home, BRAINSTEM_AGENT_CACHE=str(self.cache), **extra)

    @criteria("A1")
    def test_doctor_reports_ready_on_this_host(self):
        result = run_cli(["doctor", "--json"], self.env())
        report = cli_json(result)
        self.assertEqual(result.returncode, 0, report.get("problems"))
        self.assertTrue(report["ready"])
        checks = report["checks"]
        self.assertEqual(checks["grail_source"]["commit"], PINNED_COMMIT)
        self.assertEqual(checks["grail_source"]["kernel_sha256"], KERNEL_SHA256)
        self.assertEqual(checks["grail_source"]["files"], 30)
        self.assertTrue(checks["worker_interpreter"]["ok"])
        self.assertTrue(checks["worker_interpreter"]["hash_locked"])
        self.assertEqual(checks["credential"]["source"], "brainstem")
        self.assertTrue(checks["sandbox"]["ok"])
        self.assertEqual(checks["sandbox"]["environment"], "macos-seatbelt")
        self.assertEqual(leaks(real_credential_needles(), texts=[result.stdout, result.stderr]), [])

    @criteria("A1", "A6", "A7")
    def test_doctor_deep_proves_bridge_membrane_and_integrity_inside_grail(self):
        result = run_cli(["doctor", "--deep", "--json", "--workspace", str(self.workspace)],
                         self.env(), timeout=240)
        report = cli_json(result)
        self.assertEqual(result.returncode, 0, report.get("problems"))
        deep = report["deep"]
        self.assertTrue(deep["worker"]["started"])
        self.assertEqual(deep["worker"]["health"], "ok")
        bridge = deep["bridge"]
        self.assertEqual(bridge["no_grant_tools"], [])
        self.assertEqual(set(bridge["granted_tools"]), CELL_TOOLS)
        for kind in ("forged", "revoked", "expired", "other_worker"):
            self.assertEqual(bridge["refused_grants"][kind], ["brainstem_agent_status"], kind)
        membrane = deep["membrane"]
        for probe in ("read_state_db", "read_installed_credential", "read_owner_home",
                      "read_workspace", "write_outside", "write_tracked_grail_file",
                      "write_agents_dir", "connect_other_loopback"):
            self.assertEqual(membrane[probe], "denied", probe)
        self.assertEqual(membrane["connect_broker"], "allowed")
        integrity = deep["integrity"]
        self.assertTrue(integrity["before"]["ok"])
        self.assertTrue(integrity["after"]["ok"])
        self.assertEqual(integrity["after"]["files"], 30)
        self.assertIn(".env", integrity["after"]["untracked"])
        self.assertTrue(deep["stop"]["group_gone"])
        self.assertLess(deep["stop"]["seconds"], 5)
        self.assertTrue(process_gone(deep["worker"]["pid"]))
        self.assertEqual(
            leaks(real_credential_needles(), roots=[self.home], texts=[result.stdout, result.stderr]),
            [])
        record_metric("cold_start_seconds", deep["worker"]["start_seconds"])

    @criteria("A8")
    def test_real_grail_without_a_credential_reports_unauthenticated(self):
        env = self.env(BRAINSTEM_HOME=str(private_dir(self)))
        result = run_cli(["doctor", "--deep", "--json", "--workspace", str(self.workspace)],
                         env, timeout=240)
        self.assertNotEqual(result.returncode, 0)
        report = cli_json(result)
        self.assertFalse(report["ready"])
        self.assertFalse(report["checks"]["credential"]["ok"])
        self.assertEqual(report["deep"]["worker"]["health"], "unauthenticated")

    @criteria("A8", "A11")
    def test_invalid_credential_is_an_explicit_non_success(self):
        bogus = "ghu_" + "0" * 36
        token = write_token_file(private_dir(self), bogus)
        result = run_cli(["chat", "hello", "--workspace", str(self.workspace), "--json"],
                         self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(token)), timeout=240)
        self.assertNotEqual(result.returncode, 0)
        report = cli_json(result)
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "failed")
        self.assertIn("credential", report["error"].lower())
        self.assertIsNone(report["response"])
        self.assertEqual(leaks({"bogus": bogus}, roots=[self.home],
                               texts=[result.stdout, result.stderr]), [])

    @criteria("A7")
    def test_tampering_with_a_worker_copy_is_detected_at_stop(self):
        from brainstem_agent import grail
        from brainstem_agent.worker import GrailWorker, WorkerConfig

        source = grail.ensure_grail_source(self.cache, fetch=False)
        worker = GrailWorker(
            WorkerConfig(worker_id="tamper", home=self.home, source=source,
                         python=grail.worker_venv_python(self.cache),
                         broker_url="http://127.0.0.1:9"),
            register=lambda worker_id, generation: secrets.token_urlsafe(16),
        )
        started = worker.start()
        self.addCleanup(worker.stop)
        self.assertTrue(started["integrity_before"]["ok"])
        target = worker.tree / "rapp_brainstem" / "soul.md"
        os.chmod(target, 0o644)
        target.write_text("mutated from outside")
        stopped = worker.stop()
        self.assertFalse(stopped["integrity_after"]["ok"])
        self.assertIn("soul.md", stopped["integrity_after"]["detail"])
        self.assertTrue(stopped["group_gone"])
        self.assertFalse(worker.tree.exists())

    @criteria("A6", "A7")
    def test_local_http_callers_cannot_rewrite_the_bridge_only_agents_path(self):
        from brainstem_agent import grail
        from brainstem_agent.worker import GrailWorker, WorkerConfig

        source = grail.ensure_grail_source(self.cache, fetch=False)
        worker = GrailWorker(
            WorkerConfig(worker_id="api", home=self.home, source=source,
                         python=grail.worker_venv_python(self.cache),
                         broker_url="http://127.0.0.1:9"),
            register=lambda worker_id, generation: secrets.token_urlsafe(16),
        )
        worker.start()
        self.addCleanup(worker.stop)
        boundary = "ba" + secrets.token_hex(8)
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"evil_agent.py\"\r\nContent-Type: text/x-python\r\n\r\n"
            "from agents.basic_agent import BasicAgent\n"
            "class Evil(BasicAgent):\n    name = 'evil'\n"
            "    metadata = {'description': 'x', 'parameters': {'type': 'object'}}\n"
            "    def perform(self, **kw):\n        return 'evil'\n"
            f"\r\n--{boundary}--\r\n").encode()
        attempts = [
            urllib.request.Request(worker.url + "/agents/import", data=payload, method="POST",
                                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            urllib.request.Request(worker.url + "/agents/rapp_bridge_agent.py", method="DELETE"),
        ]
        for request in attempts:
            try:
                with OPEN.open(request, timeout=30) as response:
                    body = response.read().decode(errors="replace")
                    self.assertTrue(response.status >= 400 or "error" in body, body[:200])
            except urllib.error.HTTPError as error:
                self.assertGreaterEqual(error.code, 400)
        agents = sorted(path.name for path in (worker.tree / "agents").iterdir())
        self.assertEqual(agents, ["rapp_bridge_agent.py"])
        with OPEN.open(urllib.request.Request(worker.url + "/health"), timeout=30) as response:
            self.assertEqual(json.loads(response.read())["agents"], [])
        stopped = worker.stop()
        self.assertTrue(stopped["integrity_after"]["ok"], stopped["integrity_after"])


# Unchanged Grail, imported from a verified copy in its own interpreter (no server, no
# inference): its stream accumulator and tool runner handle three streamed calls.
GRAIL_ARGUMENTS = r"""
import json, sys
sys.path.insert(0, ".")
import brainstem

class Recorder:
    def __init__(self):
        self.calls = []
    def perform(self, **arguments):
        self.calls.append(arguments)
        return "performed"

def streamed(name, *fragments):
    first = {"index": 0, "id": "call_" + name, "type": "function", "function": {"name": name}}
    chunks = [{"choices": [{"index": 0, "delta": {"tool_calls": [first]}}]}]
    chunks += [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": fragment}}]}}]} for fragment in fragments]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})

    class Response:
        def iter_lines(self, decode_unicode=False):
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
            yield "data: [DONE]"
    generator = brainstem._accumulate_stream(Response())
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value["message"]["tool_calls"]

agents = {"schedule_list": Recorder(), "list_files": Recorder()}
report = {}
for label, calls in (("no argument fragments", streamed("schedule_list")),
                     ("an empty fragment", streamed("list_files", "")),
                     ("the required argument", streamed("schedule_list", '{"schedule_id":',
                                                        ' "all"}'))):
    results, _logs = brainstem.run_tool_calls(calls, agents)
    report[label] = {"arguments": calls[0]["function"]["arguments"],
                     "result": results[0]["content"]}
report["performed"] = {name: agent.calls for name, agent in agents.items()}
print("REPORT " + json.dumps(report))
"""


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to run the unchanged Grail code")
class RealGrailToolArgumentTests(unittest.TestCase):
    @criteria("A2", "B4")
    def test_unchanged_grail_refuses_empty_arguments_so_every_cell_tool_requires_one(self):
        import shutil
        import subprocess

        from brainstem_agent import grail
        from brainstem_agent.host import DEFAULT_CAPABILITIES, AgentHost

        cache = prepared_cache()
        source = grail.ensure_grail_source(cache, fetch=False)
        copy = private_dir(self) / "rapp_brainstem"
        for relative in source.inventory:
            (copy / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source.root / relative, copy / relative)
        (copy / ".env").write_text("")
        result = subprocess.run(
            [str(grail.worker_venv_python(cache)), "-I", "-c", GRAIL_ARGUMENTS], cwd=copy,
            capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": str(copy.parent), "LANG": "en_US.UTF-8",
                 "PYTHONDONTWRITEBYTECODE": "1"})
        lines = [line for line in result.stdout.splitlines() if line.startswith("REPORT ")]
        self.assertTrue(lines, result.stderr[-600:])
        report = json.loads(lines[-1][7:])
        refusal = "Error: Tool arguments must be a valid JSON object."
        for label in ("no argument fragments", "an empty fragment"):
            self.assertEqual(report[label], {"arguments": "", "result": refusal}, label)
        self.assertEqual(report["the required argument"]["result"], "performed")
        self.assertEqual(report["performed"], {"schedule_list": [{"schedule_id": "all"}],
                                               "list_files": []})
        grail.verify_tree(copy, source.inventory, allow_extra=True)  # still byte-identical
        record_metric("grail_empty_arguments", report["no argument fragments"]["result"])
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(write_token_file(private_dir(self))),
                   "BRAINSTEM_HOME": str(private_dir(self))}
        host = AgentHost(private_dir(self), workspace=private_dir(self), environ=environ)
        self.addCleanup(host.close)
        unguarded = [spec.name for spec in host.broker.tool_specs(DEFAULT_CAPABILITIES)
                     if not spec.parameters.get("required")]
        self.assertEqual(unguarded, [], "these tools would reach Grail with empty arguments")


if __name__ == "__main__":
    unittest.main()
