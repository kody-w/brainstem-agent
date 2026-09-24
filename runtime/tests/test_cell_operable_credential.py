"""H8 unit specs: the credential lifecycle.

The installed brainstem is simulated by a temporary HOME holding a fake
``.brainstem/src/rapp_brainstem/.copilot_token``; the Grail process is replaced by fakes that
answer the way unchanged Grail does when GitHub rejects a token. A rejected credential becomes
an explicit state; turns are then refused at once with guidance (no worker start, no Grail
request, nothing written); replacing the token file is picked up without a restart; the cell
never persists the credential.
"""

import json
import secrets
import time
import unittest
import urllib.request
from pathlib import Path

from acceptance_support import cli_json, criteria, leaks, private_dir, run_cli, wait_until
from brainstem_agent import daemon, observe
from brainstem_agent.credential_state import CredentialState, classify
from brainstem_agent.host import AgentHost
from brainstem_agent.worker import WorkerError
from daemon_support import spawn_fake_daemon
from ops_support import FakeWorker, done, fake_brainstem, replace_token, sse, tool_turn

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def token(kind: str) -> str:
    return f"ghu_{kind}" + secrets.token_hex(16)


class GrailLike(FakeWorker):
    """Starts like unchanged Grail: a revoked token is rejected at startup (health reports
    invalid_credentials); ``NoAccess`` tokens authenticate but have no Copilot access."""
    starts: list = []

    def start(self):
        GrailLike.starts.append(self.credential.value)
        started = super().start()
        if self.credential.value.startswith("ghu_Revoked"):
            raise WorkerError("The Copilot credential was rejected: Grail reports "
                              "invalid_credentials.")
        if self.credential.value.startswith("ghu_NoAccess"):
            raise WorkerError("The Copilot credential has no Copilot access.")
        return started


class CredentialCase(unittest.TestCase):
    def setUp(self):
        GrailLike.starts = []
        self.fake_home = private_dir(self)
        self.bad = token("Revoked")
        self.brainstem = fake_brainstem(self.fake_home, self.bad)
        self.home, self.workspace = private_dir(self), private_dir(self)
        # Only HOME points at the stand-in: the product finds ~/.brainstem as it would.
        self.environ = {"HOME": str(self.fake_home)}
        self.script = tool_turn(("write_file", {"path": "a.txt", "content": "a"}))

    def host(self):
        host = AgentHost(self.home, workspace=self.workspace, environ=self.environ,
                         worker_factory=lambda **options: GrailLike(
                             lambda *args: self.script(*args), **options))
        self.addCleanup(host.close)
        return host

    def chats(self) -> int:
        import sqlite3
        connection = sqlite3.connect(self.home / "state" / "agent.sqlite3")
        try:
            return connection.execute("SELECT count(*) FROM chats").fetchone()[0]
        finally:
            connection.close()


class RejectedCredentialTests(CredentialCase):
    @criteria("H8")
    def test_a_rejected_credential_is_an_explicit_state_and_turns_are_refused_at_once(self):
        host = self.host()
        first = host.chat("hello")
        self.assertEqual(first.state, "failed")
        self.assertIn("rejected", first.error)
        self.assertIn("Sign in again in RAPP Brainstem", first.error)
        record = json.loads((self.home / "state" / "credential.json").read_text())
        self.assertEqual(record["kind"], "invalid")
        self.assertEqual(record["source"], "brainstem")
        self.assertEqual(set(record), {"kind", "reason", "since", "last_failure", "attempts",
                                       "retry_after", "source", "identity"})
        self.assertEqual(len(GrailLike.starts), 1)
        chats = self.chats()
        for _ in range(10):  # no retry storm: nothing starts, nothing is sent or written
            refused = host.chat("hello again")
            self.assertEqual(refused.state, "failed")
            self.assertEqual(refused.evidence["refused"], "credential-invalid")
            self.assertEqual(refused.evidence["grail_calls"], 0)
            self.assertIn("Nothing was run", refused.error)
            self.assertIn("http://localhost:7071", refused.error)
        self.assertEqual(len(GrailLike.starts), 1)
        self.assertEqual(self.chats(), chats)
        self.assertEqual(len(observe.read_events(self.home, event="credential.invalid")), 1)
        self.assertEqual(len(observe.read_events(self.home, event="turn.refused")), 10)
        self.assertIsNone(host.warm(), "the idle warm-up starts nothing either")
        self.assertEqual(len(GrailLike.starts), 1)

    @criteria("H8")
    def test_replacing_the_token_is_picked_up_without_a_restart(self):
        host = self.host()
        self.assertEqual(host.chat("hello").state, "failed")
        self.assertEqual(host.chat("hello").evidence["refused"], "credential-invalid")
        good = token("Fresh")
        replace_token(self.brainstem, good)
        result = host.chat("hello after signing in again")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(GrailLike.starts[-1], good, "the new worker got the new token")
        self.assertFalse((self.home / "state" / "credential.json").exists())
        self.assertTrue(observe.read_events(self.home, event="credential.changed"))
        self.assertTrue(observe.read_events(self.home, event="credential.recovered"))
        self.assertEqual(leaks({"bad": self.bad, "good": good}, roots=[self.home]), [])

    @criteria("H8")
    def test_no_access_has_its_own_state_and_guidance(self):
        replace_token(self.brainstem, token("NoAccess"))
        host = self.host()
        self.assertEqual(host.chat("hello").state, "failed")
        refused = host.chat("hello")
        self.assertEqual(refused.evidence["refused"], "credential-no-access")
        self.assertIn("no Copilot access", refused.error)

    @criteria("H8")
    def test_a_401_from_a_warm_worker_mid_life_is_recognized(self):
        good = token("Fresh")
        replace_token(self.brainstem, good)

        def rejected(worker, request, grant):
            worker.bind(grant)
            yield sse({"type": "error", "error": "Copilot auth failed (401): Bad credentials. "
                                                 "Sign in with GitHub to retry."})
        self.script = rejected
        host = self.host()
        first = host.chat("hello")
        self.assertEqual(first.state, "failed")
        self.assertIn("Sign in again", first.error)
        self.assertEqual(host.chat("hello").evidence["refused"], "credential-invalid")

    @criteria("H8")
    def test_after_the_backoff_one_probe_goes_ahead_and_the_backoff_doubles(self):
        host = self.host()
        host.chat("hello")
        state = CredentialState(self.home)
        record = state.read()
        record["retry_after"] = time.time() - 1
        state._write(record)
        probe = host.chat("hello")
        self.assertNotIn("refused", probe.evidence)
        self.assertEqual(len(GrailLike.starts), 2, "exactly one probe")
        again = state.read()
        self.assertEqual(again["attempts"], 2)
        self.assertGreater(again["retry_after"] - again["last_failure"], 1100)
        self.assertEqual(host.chat("hello").evidence["refused"], "credential-invalid")
        self.assertEqual(len(GrailLike.starts), 2)

    @criteria("H8")
    def test_only_credential_errors_count(self):
        for text, kind in (("The Copilot credential was rejected: Grail reports "
                            "invalid_credentials.", "invalid"),
                           ("Grail reported an error: Copilot auth failed (403): forbidden",
                            "invalid"),
                           ("Grail reported an error: NO_COPILOT_ACCESS:someone", "no_access"),
                           ("The Grail worker did not become ready in time.", None),
                           ("Grail reported an error: The model is overloaded.", None),
                           ("The Grail worker stopped or the stream broke mid-turn (OSError).",
                            None)):
            self.assertEqual(classify(text), kind, text)

    @criteria("H8", "H7")
    def test_doctor_and_status_explain_the_rejected_credential(self):
        self.host().chat("hello")
        scratch = private_dir(self)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.fake_home), "LANG": "en_US.UTF-8",
               "BRAINSTEM_AGENT_HOME": str(self.home),
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        doctor = cli_json(run_cli(["doctor", "--json"], env))
        check = doctor["checks"]["credential"]
        self.assertFalse(check["ok"])
        self.assertEqual(check["state"], "invalid")
        self.assertIn("Sign in again", check["fix"])
        status = cli_json(run_cli(["status", "--json"], env))
        failing = status["readiness"]["failing"]
        self.assertIn("credential", failing)
        self.assertFalse(status["readiness"]["live"])
        del scratch


class DaemonHotReloadTests(CredentialCase):
    @criteria("H8", "H7")
    def test_the_daemon_stops_warming_and_recovers_when_the_token_file_changes(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.fake_home), "LANG": "en_US.UTF-8",
               "BRAINSTEM_AGENT_HOME": str(self.home),
               "BRAINSTEM_AGENT_CACHE": str(self.home / "cache"),
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        process = spawn_fake_daemon(self.home, self.workspace, env)
        self.addCleanup(lambda: (run_cli(["stop", "--json"], env, timeout=60),
                                 process.communicate(timeout=30)))
        record = daemon.read_record(self.home)
        client = daemon.Client(record)
        self.assertTrue(wait_until(lambda: observe.read_events(
            self.home, event="credential.invalid"), 20))
        for _ in range(5):  # every wake runs the idle warm-up: it must start nothing
            client.call("POST", "/v1/wake", {})
            time.sleep(0.2)
        self.assertEqual(len(observe.read_events(self.home, event="worker.warm_failed")), 1)
        health = client.call("GET", "/v1/health")
        self.assertTrue(health["live"])
        self.assertFalse(health["ready"])
        self.assertIn("credential", health["failing"])
        credential = next(item for item in health["checks"] if item["id"] == "credential")
        self.assertIn("Sign in again", credential["fix"])
        refused = cli_json(run_cli(["chat", "hi", "--workspace", str(self.workspace),
                                    "--json"], env))
        self.assertEqual(refused["evidence"]["refused"], "credential-invalid")
        replace_token(self.brainstem, token("Fresh"))
        client.call("POST", "/v1/wake", {})

        def warm():
            answer = client.call("GET", "/v1/health")
            worker = next(item for item in answer["checks"] if item["id"] == "worker")
            return worker["ok"]
        self.assertTrue(wait_until(warm, 20, 0.2))
        answer = cli_json(run_cli(["chat", "[[tools]]", "--workspace", str(self.workspace),
                                   "--json"], env))
        self.assertTrue(answer["ok"], answer.get("error"))
        self.assertEqual(daemon.read_record(self.home)["pid"], record["pid"], "no restart")
        self.assertEqual(leaks({"bad": self.bad}, roots=[self.home]), [])
