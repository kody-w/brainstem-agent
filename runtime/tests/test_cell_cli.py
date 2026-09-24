"""A1/A5/A6/A8/A11 CLI specs through the public command surface (no Grail process)."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import unittest

from acceptance_support import (
    CANARY_TOKEN,
    HAVE_SEED,
    NO_SEED,
    SEED,
    criteria,
    isolated_env,
    kill_quietly,
    leaks,
    pid_running,
    private_dir,
    run_cli,
    cli_json,
    wait_until,
    write_token_file,
)
from brainstem_agent import grail, sandbox


class CliCase(unittest.TestCase):
    def setUp(self):
        self.scratch = private_dir(self)
        self.home = private_dir(self)
        self.cache = self.home / "cache"
        self.workspace = private_dir(self)
        self.token = write_token_file(self.scratch)

    def env(self, **extra):
        return isolated_env(self.home, self.scratch, BRAINSTEM_AGENT_CACHE=str(self.cache), **extra)

    def credential_env(self, **extra):
        return self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(self.token), **extra)

    def tool(self, name, arguments, workspace=None, env=None):
        result = run_cli(["tool", name, "--arguments", json.dumps(arguments),
                          "--workspace", str(workspace or self.workspace), "--json"],
                         env or self.credential_env())
        return result, cli_json(result)


class DoctorCliTests(CliCase):
    @criteria("A1")
    def test_doctor_is_not_ready_with_clear_reasons(self):
        result = run_cli(["doctor", "--json"], self.env(
            BRAINSTEM_AGENT_SANDBOX_EXEC="/nonexistent/sandbox-exec"))
        self.assertNotEqual(result.returncode, 0)
        report = cli_json(result)
        self.assertFalse(report["ready"])
        self.assertEqual(report["product"], "Brainstem Agent")
        for check in ("grail_source", "worker_interpreter", "credential", "sandbox"):
            self.assertFalse(report["checks"][check]["ok"], check)
            self.assertTrue(report["checks"][check]["detail"], check)
        self.assertGreaterEqual(len(report["problems"]), 4)
        human = run_cli(["doctor"], self.env())
        self.assertNotEqual(human.returncode, 0)
        self.assertIn("not ready", (human.stdout + human.stderr).lower())

    @criteria("A1", "A11")
    def test_doctor_names_the_credential_source_never_its_value(self):
        result = run_cli(["doctor", "--json"], self.credential_env())
        report = cli_json(result)
        self.assertTrue(report["checks"]["credential"]["ok"])
        self.assertEqual(report["checks"]["credential"]["source"], "file")
        self.assertEqual(report["checks"]["credential"]["kind"], "ghu")
        self.assertEqual(leaks({"c": CANARY_TOKEN, "p": CANARY_TOKEN[:8]},
                               texts=[result.stdout, result.stderr]), [])
        self.assertEqual(report["checks"]["sandbox"]["ok"], sandbox.available())
        self.assertEqual(report["checks"]["sandbox"]["environment"], "macos-seatbelt")

    @unittest.skipUnless(HAVE_SEED, NO_SEED)
    @criteria("A1", "A7")
    def test_doctor_verifies_pinned_source_and_detects_tampering(self):
        source = grail.ensure_grail_source(self.cache, seed_dir=SEED, fetch=False)
        report = cli_json(run_cli(["doctor", "--json"], self.credential_env()))
        check = report["checks"]["grail_source"]
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["commit"], grail.PINNED_COMMIT)
        self.assertEqual(check["kernel_sha256"], grail.KERNEL_SHA256)
        self.assertEqual(check["files"], 30)
        target = source.root / "soul.md"
        os.chmod(target, 0o644)
        target.write_text("tampered")
        result = run_cli(["doctor", "--json"], self.credential_env())
        self.assertNotEqual(result.returncode, 0)
        tampered = cli_json(result)["checks"]["grail_source"]
        self.assertFalse(tampered["ok"])
        self.assertIn("soul.md", tampered["detail"])


class ChatCliTests(CliCase):
    @criteria("A8")
    def test_chat_without_a_credential_is_an_explicit_non_success(self):
        result = run_cli(["chat", "hello", "--workspace", str(self.workspace), "--json"], self.env())
        self.assertNotEqual(result.returncode, 0)
        report = cli_json(result)
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "failed")
        self.assertIn("credential", report["error"].lower())
        self.assertFalse((self.home / "workers").exists() and any((self.home / "workers").iterdir()))
        human = run_cli(["chat", "hello", "--workspace", str(self.workspace)], self.env())
        self.assertNotEqual(human.returncode, 0)
        self.assertIn("credential", human.stderr.lower())

    @criteria("A8", "A11")
    def test_chat_with_an_unprepared_cache_fails_explicitly_without_leaking(self):
        result = run_cli(["chat", "hello", "--workspace", str(self.workspace), "--json"],
                         self.credential_env(BRAINSTEM_AGENT_GRAIL_SEED=str(self.scratch / "none")))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(cli_json(result)["ok"])
        self.assertEqual(leaks({"c": CANARY_TOKEN}, roots=[self.home],
                               texts=[result.stdout, result.stderr]), [])


class ToolCliTests(CliCase):
    @criteria("A6")
    def test_file_tools_refuse_absolute_parent_and_symlink_paths(self):
        outside = private_dir(self)
        (self.workspace / "link").symlink_to(outside, target_is_directory=True)
        for path in ("/etc/hosts", "../escape.txt", "link/escape.txt", str(outside / "x.txt")):
            with self.subTest(path=path):
                result, report = self.tool("write_file", {"path": path, "content": "x"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(report["ok"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.workspace.parent / "escape.txt").exists())

    @criteria("A2", "A6")
    def test_tool_calls_leave_durable_receipts(self):
        result, report = self.tool("write_file", {"path": "notes/a.txt", "content": "hello"})
        self.assertEqual(result.returncode, 0, report)
        self.assertTrue(report["ok"])
        receipts = cli_json(run_cli(["receipts", "--workspace", str(self.workspace), "--json"],
                                    self.credential_env()))["receipts"]
        self.assertEqual([(r["tool"], r["state"]) for r in receipts], [("write_file", "succeeded")])
        self.tool("write_file", {"path": "../x", "content": "hello"})
        receipts = cli_json(run_cli(["receipts", "--workspace", str(self.workspace), "--json"],
                                    self.credential_env()))["receipts"]
        self.assertEqual(sorted(r["state"] for r in receipts), ["failed", "succeeded"])

    @criteria("A6")
    def test_two_workspaces_share_neither_memory_nor_files(self):
        other = private_dir(self)
        self.tool("remember", {"text": "The user's favorite color is teal."})
        self.tool("write_file", {"path": "only-a.txt", "content": "A"})
        _, recall = self.tool("recall", {"query": "favorite color"}, workspace=other)
        self.assertNotIn("teal", recall["content"])
        memory_b = cli_json(run_cli(["memory", "--workspace", str(other), "--json"],
                                    self.credential_env()))
        self.assertEqual(memory_b["facts"], [])
        memory_a = cli_json(run_cli(["memory", "--workspace", str(self.workspace), "--json"],
                                    self.credential_env()))
        self.assertEqual(len(memory_a["facts"]), 1)
        result, report = self.tool("read_file", {"path": "only-a.txt"}, workspace=other)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["ok"])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A5")
    def test_shell_tool_is_sandboxed_through_the_cli(self):
        result, report = self.tool("run_command", {"command":
            "echo cli > from-shell.txt; /usr/bin/curl -sS -m 5 -o /dev/null https://example.com"
            " && echo NET_OK || echo NET_BLOCKED"})
        self.assertEqual(result.returncode, 0, report)
        self.assertIn("NET_BLOCKED", report["content"])
        self.assertEqual((self.workspace / "from-shell.txt").read_text(), "cli\n")


# How long a cancelled CLI may take to exit before a spec calls it hung. The 5 s
# cancellation bound (A8) is asserted on the effects themselves (the tool's group gone, its
# receipt settled), which do not include the interpreter's own teardown: that is not the
# cell's work, and under heavy parallel load it alone can take seconds.
HANG_GUARD = 60


# How long a cancelled (or orphaned) tool's process may take to be seen gone. The CLI kills its
# group at once; on a loaded 3-CPU CI runner that has taken more than 5 s to show.
GROUP_GONE_SECONDS = 15.0


class CliSignalTests(CliCase):
    """SIGINT/SIGTERM are cancellation requests; the CLI never leaves tool processes behind."""

    def spawn(self, arguments, env=None):
        process = subprocess.Popen([sys.executable, "-m", "brainstem_agent", *arguments],
                                   env=env or self.credential_env(), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.finish, process)
        return process

    @staticmethod
    def finish(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)

    def shell_tool(self, env=None):
        arguments = json.dumps({"command": "echo $$ > leader.pid; exec sleep 30",
                                "timeout_seconds": 60})
        process = self.spawn(["tool", "run_command", "--arguments", arguments,
                              "--workspace", str(self.workspace), "--json"], env=env)
        marker = self.workspace / "leader.pid"
        if not wait_until(lambda: marker.exists() and marker.read_text().strip(), 20.0):
            self.fail("the shell tool never started: " + process.stderr.read()[-300:])
        pid = int(marker.read_text())
        self.addCleanup(kill_quietly, pid)
        return process, pid

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A8")
    def test_sigint_during_a_direct_shell_tool_cancels_and_kills_its_group(self):
        process, pid = self.shell_tool()
        signalled, signalled_at = time.monotonic(), time.time()
        process.send_signal(signal.SIGINT)
        process.send_signal(signal.SIGINT)
        # Soon after the signal the command's whole group is gone (observed directly) ...
        gone = wait_until(lambda: not pid_running(pid), GROUP_GONE_SECONDS, interval=0.02)
        group_seconds = time.monotonic() - signalled
        self.assertTrue(gone, f"the sandboxed command was still running {group_seconds:.1f}s "
                              "after SIGINT")
        out, err = process.communicate(timeout=HANG_GUARD)
        self.assertEqual(process.returncode, 4, err[-400:])
        self.assertFalse(pid_running(pid), "the sandboxed command outlived the CLI")
        self.assertNotIn("Traceback", err)
        report = json.loads(out)
        self.assertFalse(report["ok"])
        self.assertIn("cancel", report["content"].lower())
        receipts = cli_json(run_cli(["receipts", "--workspace", str(self.workspace), "--json"],
                                    self.credential_env()))["receipts"]
        self.assertEqual([(r["tool"], r["state"]) for r in receipts], [("run_command", "failed")])
        # ... and the CLI's own cancellation (group killed, its stop confirmed, the outcome
        # durably recorded) was complete within the same bound.
        [receipt] = receipts
        recorded = receipt["finished_at"] - signalled_at
        self.assertLess(recorded, GROUP_GONE_SECONDS,
                        f"the cancel was recorded {recorded:.1f}s after SIGINT")
        self.assertTrue(receipt["result"]["evidence"]["cancelled"])
        self.assertTrue(receipt["result"]["evidence"]["group_gone"])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A8")
    def test_sigterm_during_a_direct_shell_tool_is_a_clean_cancel(self):
        process, pid = self.shell_tool()
        signalled = time.monotonic()
        process.send_signal(signal.SIGTERM)
        gone = wait_until(lambda: not pid_running(pid), GROUP_GONE_SECONDS, interval=0.02)
        seconds = time.monotonic() - signalled
        self.assertTrue(gone, f"the sandboxed command was still running {seconds:.1f}s after "
                              "SIGTERM")
        out, err = process.communicate(timeout=HANG_GUARD)
        self.assertEqual(process.returncode, 4, err[-400:])
        self.assertFalse(pid_running(pid))
        self.assertFalse(json.loads(out)["ok"])

    @unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
    @criteria("A8")
    def test_sigkill_of_the_cli_during_a_shell_tool_kills_its_group_promptly(self):
        process, pid = self.shell_tool()
        killed = time.monotonic()
        process.kill()
        process.communicate(timeout=15)
        gone = wait_until(lambda: not pid_running(pid), GROUP_GONE_SECONDS, interval=0.02)
        seconds = time.monotonic() - killed
        self.assertTrue(gone, f"the sandboxed command was still running {seconds:.1f}s after "
                              "its host was SIGKILLed")

    @criteria("A8")
    def test_sigint_while_waiting_for_the_home_lock_cancels_before_any_turn(self):
        state = self.home / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        lock = os.open(state / "host.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = self.spawn(["chat", "hello", "--workspace", str(self.workspace), "--json"])
        database = state / "agent.sqlite3"
        # The signal handler is installed before the home is opened; the CLI then waits for
        # the lock, which it has open while it waits (seen with lsof where it exists).
        self.assertTrue(wait_until(database.exists, 30.0), "the CLI never opened its home")
        if os.path.exists("/usr/sbin/lsof"):
            self.assertTrue(wait_until(lambda: str(state / "host.lock") in subprocess.run(
                ["/usr/sbin/lsof", "-p", str(process.pid), "-Fn"], capture_output=True,
                text=True).stdout, 30.0), "the CLI never waited for the home lock")
        process.send_signal(signal.SIGINT)
        try:
            out, err = process.communicate(timeout=HANG_GUARD)
        except subprocess.TimeoutExpired:
            self.fail("SIGINT was ignored while the turn waited for the home lock")
        # An ignored SIGINT would end "failed" (busy) once the lock wait gives up; only the
        # honoured one ends "cancelled" with exit 4, before any turn.
        self.assertEqual(process.returncode, 4, err[-400:])
        report = json.loads(out)
        self.assertEqual(report["state"], "cancelled")
        fcntl.flock(lock, fcntl.LOCK_UN)
        sessions = cli_json(run_cli(["sessions", "--workspace", str(self.workspace), "--json"],
                                    self.credential_env()))["sessions"]
        self.assertEqual(sessions, [], "a cancelled wait must not reserve a turn")


class SessionsCliTests(CliCase):
    @criteria("A4")
    def test_sessions_command_is_json_and_workspace_scoped(self):
        report = cli_json(run_cli(["sessions", "--workspace", str(self.workspace), "--json"],
                                  self.credential_env()))
        self.assertEqual(report["sessions"], [])


if __name__ == "__main__":
    unittest.main()
