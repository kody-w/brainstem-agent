"""D6/D7 unit specs: programmatic tool calls (run_script) and background processes.

These use the real Seatbelt sandbox and real processes; only Grail is emulated.
"""

import dataclasses
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from acceptance_support import (criteria, group_exists, pid_running, private_dir,
                                record_metric, wait_until, write_token_file)
from brainstem_agent import sandbox
from brainstem_agent.host import AgentHost
from brainstem_agent.organs import processes as process_module
from brainstem_agent.organs.scripts import ScriptOrgan, standard_library
from longturn_support import factory_for, scripted_policy

HAS_SANDBOX = sandbox.available()


class Case(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        self.workers = []

    def host(self, policy=None, workspace=None):
        host = AgentHost(self.home, workspace=workspace or self.workspace, environ=self.environ,
                         worker_factory=factory_for(policy or scripted_policy([]), self.workers))
        self.addCleanup(host.close)
        return host


@unittest.skipUnless(HAS_SANDBOX, "needs /usr/bin/sandbox-exec")
class ScriptTests(Case):
    @criteria("D6")
    def test_a_script_makes_many_receipted_tool_calls_in_one_grail_round(self):
        code = ("for n in range(1, 11):\n"
                "    write_text(f'many/{n}.txt', n * n)\n"
                "print('total', sum(int(read_text(f'many/{n}.txt')) for n in range(1, 11)))\n")
        host = self.host(scripted_policy([[("run_script", {"code": code})]], "Done."))
        result = host.chat("square ten numbers")
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.workspace / "many" / "7.txt").read_text(), "49")
        segments = result.evidence["long_turn"]["segments"]
        self.assertEqual([s["rounds"] for s in segments], [1], "one Grail round, 21 effects")
        receipts = host.receipts(result.turn_id)
        [outer] = [r for r in receipts if r["tool"] == "run_script"]
        inner = [r for r in receipts if r["call_id"].startswith(outer["call_id"] + ".")]
        self.assertEqual(len(inner), 20)
        self.assertEqual([r["tool"] for r in inner], ["write_file"] * 10 + ["read_file"] * 10)
        self.assertTrue(all(r["state"] == "succeeded" for r in inner + [outer]))
        self.assertEqual(outer["result"]["evidence"]["inner_calls"], 20)
        self.assertEqual(result.evidence["long_turn"]["inner_calls"], 20)
        self.assertEqual(result.evidence["long_turn"]["tool_calls"], 21)
        record_metric("d6_unit_inner_calls_per_round", len(inner))

    @criteria("D6", "D10")
    def test_inner_calls_pass_the_same_capability_and_allowlist_checks(self):
        code = ("for tool, args in [('write_file', {'path': 'x.txt', 'content': 'x'}),\n"
                "                   ('run_command', {'command': 'touch y.txt'}),\n"
                "                   ('read_file', {'path': 'seed.txt'})]:\n"
                "    try:\n"
                "        print(tool, call(tool, **args)[:40])\n"
                "    except ToolError as error:\n"
                "        print(tool, 'refused:', error)\n")
        (self.workspace / "seed.txt").write_text("seed")
        host = self.host(scripted_policy([[("run_script", {"code": code})]], "Done."))
        result = host.chat("try", capabilities=["scripts.run", "files.read", "shell.run"])
        self.assertTrue(result.ok, result.error)
        self.assertFalse((self.workspace / "x.txt").exists())
        self.assertFalse((self.workspace / "y.txt").exists())
        receipts = {r["tool"]: r for r in host.receipts(result.turn_id) if "." in r["call_id"]}
        self.assertEqual(receipts["write_file"]["state"], "denied")  # not granted to the turn
        self.assertEqual(receipts["write_file"]["result"]["reason"], "not granted")
        self.assertEqual(receipts["run_command"]["state"], "denied")  # granted, not allowed
        self.assertEqual(receipts["run_command"]["result"]["reason"], "not allowed in scripts")
        self.assertEqual(receipts["read_file"]["state"], "succeeded")

    @criteria("D6")
    def test_a_revoked_grant_refuses_the_next_inner_call(self):
        code = ("print(call('list_files', path='.')[:5])\n"
                "import time; time.sleep(1.5)\n"
                "try:\n    call('list_files', path='.')\nexcept ToolError as e:\n"
                "    print('refused:', e)\n")
        host = self.host(scripted_policy([[("run_script", {"code": code})]], "Done."))

        def revoke():
            wait_until(lambda: self.workers and self.workers[0].grants, 10)
            time.sleep(0.7)
            host.authority.revoke(self.workers[0].grants[0])
        threading.Thread(target=revoke, daemon=True).start()
        result = host.chat("go")
        receipts = [r for r in host.receipts(result.turn_id) if "." in r["call_id"]]
        self.assertEqual([(r["tool"], r["state"]) for r in receipts],
                         [("list_files", "succeeded"), ("list_files", "denied")])
        self.assertEqual(receipts[1]["result"]["reason"], "grant refused")

    @criteria("D6")
    def test_the_script_sandbox_has_no_network_and_a_read_only_workspace(self):
        host = self.host()
        code = ("import socket, os\n"
                "for target in [('1.1.1.1', 53), ('127.0.0.1', %d)]:\n"
                "    s = socket.socket()\n    s.settimeout(2)\n"
                "    try:\n        s.connect(target); print('connected', target)\n"
                "    except OSError as e:\n        print('network denied', target[1])\n"
                "try:\n    open('direct.txt', 'w').write('x'); print('wrote workspace')\n"
                "except OSError:\n    print('workspace read-only')\n"
                "for path in [%r, %r]:\n"
                "    try:\n        os.listdir(path); print('read', path)\n"
                "    except OSError:\n        print('denied', path)\n"
                "print(open('seed.txt').read())\n"
                ) % (host.broker.port, str(host.home), os.path.expanduser("~"))
        (self.workspace / "seed.txt").write_text("readable seed")
        result = host.invoke_tool("run_script", {"code": code})
        self.assertTrue(result["ok"], result["content"])
        text = result["content"]
        self.assertEqual(text.count("network denied"), 2, text)
        self.assertIn("workspace read-only", text)
        self.assertNotIn("wrote workspace", text)
        self.assertEqual(text.count("denied /"), 2, text)
        self.assertIn("readable seed", text)
        self.assertFalse((self.workspace / "direct.txt").exists())

    @criteria("D6")
    def test_calls_time_and_output_are_bounded(self):
        host = self.host()
        many = host.invoke_tool("run_script", {"code": (
            "n = 0\nfor i in range(60):\n    try:\n        call('list_files', path='.')\n"
            "        n += 1\n    except ToolError:\n        pass\nprint('made', n)\n")})
        self.assertIn("made 50", many["content"])
        self.assertIn("over the limit", many["content"])
        started = time.monotonic()
        slow = host.invoke_tool("run_script", {"code": "import time\ntime.sleep(30)\n",
                                               "timeout_seconds": 2})
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(slow["ok"])
        self.assertIn("timed out", slow["content"])
        loud = host.invoke_tool("run_script", {"code": "print('x' * 500000)\n"})
        self.assertLess(len(loud["content"]), 70_000)
        self.assertIn("characters omitted", loud["content"])

    @criteria("D6", "D8")
    def test_cancelling_the_turn_stops_the_script_within_five_seconds(self):
        cancel = threading.Event()
        code = "import time\nprint('started', flush=True)\ntime.sleep(60)\n"
        host = self.host(scripted_policy([[("run_script", {"code": code})]], "Done."))
        threading.Thread(target=lambda: (time.sleep(1.5), cancel.set()), daemon=True).start()
        started = time.monotonic()
        result = host.chat("go", cancel_event=cancel)
        self.assertEqual(result.state, "cancelled")
        self.assertLess(time.monotonic() - started, 1.5 + 5.0)
        [outer] = host.receipts(result.turn_id)
        self.assertTrue(outer["result"]["cancelled"])
        self.assertEqual(outer["result"]["evidence"]["group_state"], "gone")


class ScriptInterpreterTests(unittest.TestCase):
    """An interpreter installed inside a denied area (a pyenv or uv Python under the home)
    still starts: only its standard library is re-allowed, never an area that is or holds
    something denied."""

    def organ(self, *deny):
        home = private_dir(self)
        return ScriptOrgan(run_root=home / "run", deny_read=(home, *deny),
                           environ={"HOME": str(home)}), home

    @criteria("D6", "D10")
    def test_only_a_standard_library_inside_a_denied_area_is_re_allowed(self):
        organ, home = self.organ()
        inside = home / "pyenv" / "versions" / "3.11" / "lib" / "python3.11"
        inside.mkdir(parents=True)
        workspace, call_dir = private_dir(self), private_dir(self)
        organ.library = (inside, Path("/usr/lib"), home, Path("/"))
        self.assertEqual(organ.policy(workspace, call_dir).readable, (workspace, call_dir, inside))
        organ.library = ()
        self.assertEqual(organ.policy(workspace, call_dir).readable, (workspace, call_dir))

    @criteria("D6")
    def test_the_library_is_this_interpreters_standard_library(self):
        organ, _home = self.organ()
        self.assertTrue(any((path / "os.py").is_file() for path in organ.library), organ.library)
        self.assertEqual(standard_library("/bin/sh"), ())

    @unittest.skipUnless(HAS_SANDBOX, "needs /usr/bin/sandbox-exec")
    @criteria("D6", "D10")
    def test_an_interpreter_whose_library_lies_in_a_denied_area_starts_and_reads_no_more(self):
        [stdlib] = [path for path in standard_library(sys.executable) if (path / "os.py").is_file()]
        organ, _home = self.organ(stdlib.parent)
        workspace, call_dir = private_dir(self), private_dir(self)
        probe = ("import json, os\ntry:\n    os.listdir(%r)\n    print('listed')\n"
                 "except OSError:\n    print('parent denied')\n") % str(stdlib.parent)

        def run(policy):
            return subprocess.run(sandbox.wrap([sys.executable, "-I", "-S", "-c", probe], policy),
                                  capture_output=True, text=True, timeout=60, cwd=workspace,
                                  env={"HOME": str(call_dir), "PATH": "/usr/bin:/bin"})

        policy = organ.policy(workspace, call_dir)
        started = run(policy)
        self.assertEqual(started.returncode, 0, started.stderr[-400:])
        self.assertEqual(started.stdout.strip(), "parent denied")
        control = run(dataclasses.replace(policy, readable=(workspace, call_dir)))
        self.assertNotEqual(control.returncode, 0, "without the re-allow it cannot start")


@unittest.skipUnless(HAS_SANDBOX, "needs /usr/bin/sandbox-exec")
class ProcessTests(Case):
    def call(self, host, tool, **arguments):
        return host.invoke_tool(tool, arguments)

    @criteria("D7")
    def test_start_poll_read_write_and_stop_a_background_process(self):
        host = self.host()
        started = self.call(host, "process_start",
                            command="echo ready; while read line; do echo got:$line; done",
                            name="echo loop")
        self.assertTrue(started["ok"], started["content"])
        process_id = started["content"].split()[1]
        self.assertTrue(process_id.startswith("proc_"))
        self.assertTrue(wait_until(lambda: "ready" in self.call(
            host, "process_read", process_id=process_id)["content"], 10))
        self.assertTrue(self.call(host, "process_write", process_id=process_id,
                                  input="hello\n")["ok"])
        self.assertTrue(wait_until(lambda: "got:hello" in self.call(
            host, "process_read", process_id=process_id)["content"], 10))
        status = self.call(host, "process_status", process_id=process_id)["content"]
        self.assertIn("running", status)
        record = host.store.get_process(host.namespace, process_id)
        self.assertEqual(record["state"], "running")
        pid = record["pid"]
        self.assertTrue(pid_running(pid))
        stopped = self.call(host, "process_stop", process_id=process_id)
        self.assertTrue(stopped["ok"])
        self.assertTrue(wait_until(lambda: not group_exists(pid), 5))
        self.assertEqual(host.store.get_process(host.namespace, process_id)["state"], "stopped")
        self.assertIn("got:hello", self.call(host, "process_read", process_id=process_id,
                                             offset=0)["content"])

    @criteria("D7")
    def test_an_exited_process_records_its_exit_code(self):
        host = self.host()
        started = self.call(host, "process_start", command="echo done; exit 3")
        process_id = started["content"].split()[1]
        self.assertTrue(wait_until(lambda: host.store.get_process(
            host.namespace, process_id)["state"] == "exited", 10))
        record = host.store.get_process(host.namespace, process_id)
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("exit code 3", self.call(host, "process_status",
                                               process_id=process_id)["content"])

    @criteria("D7")
    def test_count_output_and_input_are_bounded(self):
        host = self.host()
        ids = []
        for number in range(4):
            result = self.call(host, "process_start", command="sleep 30")
            self.assertTrue(result["ok"], result["content"])
            ids.append(result["content"].split()[1])
        fifth = self.call(host, "process_start", command="sleep 30")
        self.assertFalse(fifth["ok"])
        self.assertIn("At most 4", fifth["content"])
        for process_id in ids:
            self.call(host, "process_stop", process_id=process_id)
        loud = self.call(host, "process_start",
                         command="head -c 1500000 /dev/zero | tr '\\0' 'x'; echo")
        process_id = loud["content"].split()[1]
        self.assertTrue(wait_until(lambda: host.store.get_process(
            host.namespace, process_id)["state"] == "exited", 20))
        output = host.run_root_output = (host.home / "run" / "processes" / process_id /
                                         "output.log")
        self.assertEqual(output.stat().st_size, process_module.OUTPUT_CAP)
        read = self.call(host, "process_read", process_id=process_id)["content"]
        self.assertIn("not kept", read)
        self.assertIn("of 1500001", read)
        self.assertFalse(self.call(host, "process_write", process_id=process_id,
                                   input="x" * 9000)["ok"])

    @criteria("D7", "D10")
    def test_a_process_belongs_to_its_workspace_and_its_sandbox(self):
        host = self.host()
        other = private_dir(self)
        started = self.call(host, "process_start", command=(
            "(echo x > %s/escape.txt) 2>/dev/null || echo write-denied; "
            "(/usr/bin/nc -z -w 2 1.1.1.1 53) 2>/dev/null || echo network-denied; "
            "echo inside > inside.txt; sleep 30") % other)
        process_id = started["content"].split()[1]
        self.assertTrue(wait_until(lambda: "network-denied" in self.call(
            host, "process_read", process_id=process_id)["content"], 10))
        self.assertIn("write-denied", self.call(host, "process_read",
                                                process_id=process_id)["content"])
        self.assertFalse((other / "escape.txt").exists())
        self.assertEqual((self.workspace / "inside.txt").read_text(), "inside\n")
        foreign = host.invoke_tool("process_stop", {"process_id": process_id}, workspace=other)
        self.assertFalse(foreign["ok"])
        self.assertIn("No background process", foreign["content"])
        self.assertIn("No background processes", host.invoke_tool(
            "process_status", {"process_id": "all"}, workspace=other)["content"])
        self.assertIn("running", self.call(host, "process_status",
                                           process_id=process_id)["content"])

    @criteria("D7")
    def test_a_process_outlives_its_turn_in_a_long_lived_host_and_stop_ends_it(self):
        host = self.host(scripted_policy([
            [("process_start", {"command": "echo tick; sleep 60", "name": "ticker"})]],
            "Started."))
        host.process_organ.long_lived = True
        first = host.chat("start a ticker")
        self.assertTrue(first.ok, first.error)
        [record] = host.store.list_processes(host.namespace)
        self.assertEqual(record["turn_id"], first.turn_id)
        self.assertTrue(pid_running(record["pid"]))
        self.assertTrue(wait_until(lambda: "tick" in self.call(
            host, "process_read", process_id=record["process_id"])["content"], 10))
        host.close()  # the daemon's stop closes its host
        self.assertTrue(wait_until(lambda: not group_exists(record["pid"]), 5))
        from brainstem_agent.state import Store
        with Store(self.home / "state" / "agent.sqlite3") as store:
            self.assertEqual(store.get_process(None, record["process_id"])["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
