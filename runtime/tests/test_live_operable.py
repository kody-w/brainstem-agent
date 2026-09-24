"""Live H3/H8/H12 specs: real Copilot inference through the installed brainstem's sign-in.

Skips unless BRAINSTEM_AGENT_LIVE=1. Two live turns: one after a backup is restored into a
fresh home (the answer comes from the restored profile), and one after a rejected sign-in is
replaced in a stand-in brainstem while the daemon keeps running.
"""

import json
import time
import unittest

from acceptance_support import (INSTALLED_CREDENTIAL, LIVE, cli_json, criteria, leaks,
                                private_dir, real_credential_needles, record_metric, run_cli,
                                wait_until)
from brainstem_agent import daemon
from ops_support import fake_brainstem, replace_token
from test_live import LiveCase


@unittest.skipUnless(LIVE, "set BRAINSTEM_AGENT_LIVE=1 to spend real Copilot turns")
class LiveOperableTests(LiveCase):
    def tearDown(self):
        record = daemon.read_record(self.home)
        if record is not None:
            run_cli(["stop", "--json"], self.env(), timeout=90)

    @criteria("H3", "H12")
    def test_a_live_turn_after_restore_answers_from_the_restored_state(self):
        code = run_cli(["profile", "add", "--text", "The owner's favorite color is teal.",
                        "--json"], self.env())
        self.assertEqual(code.returncode, 0, code.stderr)
        target = private_dir(self) / "backup"
        started = time.monotonic()
        made = cli_json(run_cli(["backup", "--output", str(target), "--json"], self.env()))
        backup_seconds = round(time.monotonic() - started, 3)
        self.assertTrue(made["ok"], made)
        restored_home = private_dir(self)
        self.home = restored_home  # every later command (and the leak scan) uses the new home
        started = time.monotonic()
        restored = cli_json(run_cli(["restore", str(target), "--json"], self.env()))
        restore_seconds = round(time.monotonic() - started, 3)
        self.assertTrue(restored["ok"], restored)
        self.assertTrue(restored["health"]["ready"], restored["health"]["failing"])
        result, report = self.chat("What is my favorite color? Answer with one word.")
        self.assert_success(result, report)
        answer = report["response"]["response"]
        self.assertIn("teal", answer.lower())
        record_metric("h12_live_turn_after_restore", {
            "backup_seconds": backup_seconds, "restore_seconds": restore_seconds,
            "restored_counts": restored["counts"], "answer": answer[:80],
            "grail_requests": report["evidence"]["long_turn"]["grail_requests"],
            "fresh_home": True})

    @unittest.skipUnless(INSTALLED_CREDENTIAL.exists(), "needs the installed brainstem's sign-in")
    @criteria("H8")
    def test_a_rejected_sign_in_replaced_while_the_daemon_runs_serves_a_live_turn(self):
        fake_home = private_dir(self)
        bogus = "ghu_" + "0" * 36
        brainstem = fake_brainstem(fake_home, bogus)
        env = self.env(HOME=str(fake_home))
        started = cli_json(run_cli(["serve", "--detach", "--workspace", str(self.workspace),
                                    "--json"], env))
        client = daemon.connect(self.home)
        self.assertTrue(wait_until(lambda: "credential" in client.call(
            "GET", "/v1/health")["failing"], 120, 0.5))
        refused = cli_json(run_cli(["chat", "hello", "--workspace", str(self.workspace),
                                    "--json"], env))
        self.assertEqual(refused["evidence"]["refused"], "credential-invalid")
        self.outputs += [json.dumps(refused)]
        raw = INSTALLED_CREDENTIAL.read_text().strip()
        value = json.loads(raw)["access_token"] if raw.startswith("{") else raw
        try:
            replace_token(brainstem, value)
            client.call("POST", "/v1/wake", {})
            self.assertTrue(wait_until(lambda: client.call("GET", "/v1/health")["ready"], 180,
                                       0.5))
            result, report = self.chat("Reply with exactly the word: ready", env=env)
            self.assert_success(result, report)
            self.assertEqual(report["evidence"]["daemon"]["pid"], started["pid"])
        finally:
            run_cli(["stop", "--json"], env, timeout=90)
            (brainstem / "src" / "rapp_brainstem" / ".copilot_token").unlink(missing_ok=True)
        self.assertEqual(leaks({"bogus": bogus, **real_credential_needles()},
                               roots=[self.home]), [])
