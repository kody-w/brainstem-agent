"""H10 unit specs: local observability.

``logs`` reads the cell's event log (and the daemon's, the workers' and the outbound request
logs) with filters; ``stats --json`` computes turns, outcomes, the uncertain count, latency
percentiles, tool usage and scheduled-run fire delays from the local store. Neither makes a
network connection, and the event log never holds the owner's words or a credential.
"""

import io
import json
import socket
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from acceptance_support import (CANARY_TOKEN, cli_json, criteria, isolated_env, leaks,
                                private_dir, run_cli)
from brainstem_agent import cli, observe
from brainstem_agent.host import AgentHost
from ops_support import MARKER_WORDS, FakeWorker, done, seed_home, sse, tool_turn


def error_turn(worker, request, grant):
    worker.bind(grant)
    yield sse({"type": "error", "error": "The model is overloaded."})


def crash_after_tool(worker, request, grant):
    bound = worker.bind(grant)
    worker.invoke(grant, bound, "write_file", {"path": "x.txt", "content": "x"})
    raise OSError("worker died")
    yield  # pragma: no cover


class ObservabilityCase(unittest.TestCase):
    def setUp(self):
        self.home, self.workspace, self.environ = seed_home(self)
        self.scratch = private_dir(self)
        for script in (error_turn, crash_after_tool):
            host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                             worker_factory=lambda s=script, **options: FakeWorker(s, **options))
            host.chat(f"a turn that does not succeed {MARKER_WORDS}")
            host.close()

    def cli(self, *arguments):
        env = {**isolated_env(self.home, self.scratch),
               "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": self.environ[
                   "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"]}
        result = run_cli([*arguments, "--json"], env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        return cli_json(result)


class StatsTests(ObservabilityCase):
    @criteria("H10")
    def test_stats_are_computed_from_the_store(self):
        stats = self.cli("stats")
        self.assertEqual(stats["source"], "the local store; nothing leaves this machine")
        turns = stats["turns"]
        self.assertEqual(turns["total"], 5)
        self.assertEqual(turns["outcomes"], {"succeeded": 3, "failed": 1, "uncertain": 1})
        self.assertEqual(stats["uncertain"]["turns"], 1)
        self.assertGreaterEqual(stats["uncertain"]["total"], 1)
        latency = stats["latency_seconds"]["all"]
        self.assertEqual(latency["count"], 5)
        for key in ("p50", "p90", "p95", "p99", "max", "mean"):
            self.assertGreaterEqual(latency[key], 0.0, key)
        self.assertLessEqual(latency["p50"], latency["p99"])
        tools = stats["tools"]
        # Two owner turns, the scheduled run (files.read only: its calls were denied) and the
        # turn whose worker died after its write.
        self.assertEqual(tools["remember"]["calls"], 3)
        self.assertEqual(tools["remember"]["states"], {"succeeded": 2, "denied": 1})
        self.assertEqual(tools["write_file"]["states"], {"succeeded": 3, "denied": 1})
        self.assertEqual(stats["tool_calls"], sum(entry["calls"] for entry in tools.values()))
        runs = stats["scheduled_runs"]
        self.assertEqual(runs["total"], 1)
        self.assertEqual(runs["fire_delay_seconds"]["count"], 1)
        self.assertEqual(stats["turns"]["grail_requests"], 5)

    @criteria("H10")
    def test_stats_windows_and_workspaces(self):
        later = self.cli("stats", "--since", "1s")
        self.assertLessEqual(later["turns"]["total"], 5)
        future = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 3600))
        none = self.cli("stats", "--since", future)
        self.assertEqual(none["turns"]["total"], 0)
        other = self.cli("stats", "--workspace", str(private_dir(self)))
        self.assertEqual(other["turns"]["total"], 0)
        mine = self.cli("stats", "--workspace", str(self.workspace))
        self.assertEqual(mine["turns"]["total"], 5)

    @criteria("H10")
    def test_percentiles_use_the_nearest_rank(self):
        values = list(range(1, 101))
        result = observe.percentiles(values)
        self.assertEqual((result["p50"], result["p90"], result["p99"], result["max"]),
                         (50, 90, 99, 100))
        self.assertEqual(observe.percentiles([]), {"count": 0})
        self.assertEqual(observe.percentiles([4.0])["p50"], 4.0)

    @criteria("H10")
    def test_stats_and_logs_never_open_a_network_connection(self):
        original = socket.socket.connect

        def refuse(*_args, **_kwargs):
            raise AssertionError("observability must stay on this machine")
        socket.socket.connect = refuse
        try:
            environ = {"BRAINSTEM_AGENT_HOME": str(self.home), "HOME": str(self.home)}
            for arguments in (["stats", "--json"], ["logs", "--json"],
                              ["logs", "--source", "egress", "--json"]):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(cli.main(arguments, environ), 0)
                json.loads(buffer.getvalue())
        finally:
            socket.socket.connect = original


class LogsTests(ObservabilityCase):
    @criteria("H10")
    def test_logs_filter_by_event_level_turn_text_and_time(self):
        finished = self.cli("logs", "--event", "turn.finished")["entries"]
        self.assertEqual(len(finished), 5)
        self.assertEqual({item["state"] for item in finished},
                         {"succeeded", "failed", "uncertain"})
        warnings = self.cli("logs", "--level", "warn")["entries"]
        self.assertTrue(warnings)
        self.assertTrue(all(item["level"] in ("warn", "error") for item in warnings))
        turn = finished[0]["turn_id"]
        mine = self.cli("logs", "--turn", turn)["entries"]
        self.assertEqual({item["event"] for item in mine}, {"turn.started", "turn.finished"})
        self.assertTrue(self.cli("logs", "--grep", "overloaded")["entries"])
        self.assertEqual(self.cli("logs", "--since", "2099-01-01")["entries"], [])
        started = self.cli("logs", "--event", "turn.*", "--limit", "2")["entries"]
        self.assertEqual(len(started), 2)

    @criteria("H10", "H9")
    def test_the_event_log_never_holds_the_owners_words_or_a_credential(self):
        text = (self.home / "logs" / "events.jsonl").read_text()
        self.assertNotIn(MARKER_WORDS, text)
        self.assertEqual(leaks({"canary": CANARY_TOKEN}, roots=[self.home / "logs"]), [])
        observe.EventLog(self.home).write("probe", detail=f"token {CANARY_TOKEN} here")
        self.assertNotIn(CANARY_TOKEN, (self.home / "logs" / "events.jsonl").read_text())

    @criteria("H10")
    def test_daemon_worker_and_egress_sources(self):
        logs = self.home / "logs"
        (logs / "daemon.log").write_text("daemon started\nsomething failed: boom\n")
        (logs / "workers").mkdir(exist_ok=True)
        (logs / "workers" / "w1-g1.log").write_text("[brainstem] STREAM call\n")
        (self.home / "state" / "egress.jsonl").write_text(json.dumps(
            {"at": observe._iso(time.time()), "tool": "web_fetch", "host": "example.org"}) + "\n")
        daemon = self.cli("logs", "--source", "daemon", "--grep", "failed")["entries"]
        self.assertEqual([item["line"] for item in daemon], ["something failed: boom"])
        worker = self.cli("logs", "--source", "worker")["entries"]
        self.assertEqual(worker[-1]["line"], "[brainstem] STREAM call")
        egress = self.cli("logs", "--source", "egress", "--since", "1h")["entries"]
        self.assertEqual(egress[0]["host"], "example.org")
