"""A5/A6/A8 unit specs for the sandboxed shell organ (real Seatbelt, no Grail)."""

import os
import pwd
import socket
import subprocess
import threading
import time
import unittest
import uuid

from acceptance_support import (
    INSTALLED_CREDENTIAL,
    OWNER_HOME,
    REPO,
    criteria,
    kill_quietly,
    pid_running,
    private_dir,
    real_credential_needles,
    wait_until,
)
from brainstem_agent import sandbox
from brainstem_agent.organs import InvocationContext, OrganError, validate_arguments
from brainstem_agent.organs.shell import ShellOrgan


def context(root, **changes):
    values = dict(
        owner="local", workspace="ws", namespace="ns", session_id="s", turn_id="t",
        call_id="call" + uuid.uuid4().hex[:8], workspace_root=root,
        capabilities=("shell.run",), deadline=time.monotonic() + 60,
    )
    values.update(changes)
    return InvocationContext(**values)


@unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
class ShellOrganTests(unittest.TestCase):
    def setUp(self):
        self.workspace = private_dir(self)
        self.agent_home = private_dir(self)
        (self.agent_home / "state").mkdir(mode=0o700)
        (self.agent_home / "state" / "agent.sqlite3").write_text("STATE-CANARY")
        self.outside = private_dir(self)
        self.canary = REPO / f".ba-owner-home-canary-{uuid.uuid4().hex}"
        self.canary.write_text("OWNER-HOME-CANARY")
        self.addCleanup(self.canary.unlink)
        self.organ = ShellOrgan(
            run_root=self.agent_home / "run",
            deny_read=(self.agent_home,),
            environ={**os.environ, "GITHUB_TOKEN": "ghu_EnvCanary0000000000",
                     "BRAINSTEM_AGENT_WORKER_KEY": "worker-key-canary"},
        )
        self.spec = self.organ.tools()[0]

    def run_command(self, command, ctx=None, **arguments):
        arguments = validate_arguments(self.spec.parameters, {"command": command, **arguments})
        return self.organ.invoke(ctx or context(self.workspace), "run_command", arguments)

    @criteria("A5")
    def test_tool_contract(self):
        self.assertEqual([spec.name for spec in self.organ.tools()], ["run_command"])
        self.assertEqual(self.spec.capability, "shell.run")
        self.assertEqual(self.spec.effect, "external")

    @criteria("A5")
    def test_command_runs_inside_the_workspace(self):
        result = self.run_command("pwd; echo hi > made.txt; cat made.txt")
        self.assertTrue(result.ok, result.content)
        self.assertEqual((self.workspace / "made.txt").read_text(), "hi\n")
        self.assertIn(str(self.workspace), result.content)
        self.assertEqual(result.evidence["exit_code"], 0)
        self.assertEqual(result.evidence["sandbox"], "macos-seatbelt")

    @criteria("A5")
    def test_network_access_fails(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        result = self.run_command(
            "/usr/bin/curl -sS -m 5 -o /dev/null https://example.com && echo NET_OK || echo NET_BLOCKED;"
            " /usr/bin/nc -z -w 3 1.1.1.1 443 && echo IP_OK || echo IP_BLOCKED;"
            f" /usr/bin/nc -z -w 3 127.0.0.1 {port} && echo LOOP_OK || echo LOOP_BLOCKED")
        for blocked in ("NET_BLOCKED", "IP_BLOCKED", "LOOP_BLOCKED"):
            self.assertIn(blocked, result.content)
        for allowed in ("NET_OK", "IP_OK", "LOOP_OK"):
            self.assertNotIn(allowed, result.content)

    @criteria("A5")
    def test_writing_outside_the_workspace_fails(self):
        tmp_target = f"/private/tmp/ba-escape-{uuid.uuid4().hex}"
        result = self.run_command(
            f"echo x > {self.outside}/escape.txt && echo W1_OK || echo W1_BLOCKED;"
            f" echo x > {tmp_target} && echo W2_OK || echo W2_BLOCKED;"
            " echo x > ../parent-escape.txt && echo W3_OK || echo W3_BLOCKED;"
            f" echo x > {OWNER_HOME}/ba-escape.txt && echo W4_OK || echo W4_BLOCKED")
        for index in range(1, 5):
            self.assertIn(f"W{index}_BLOCKED", result.content)
        self.assertFalse((self.outside / "escape.txt").exists())
        self.assertFalse(os.path.exists(tmp_target))
        self.assertFalse((self.workspace.parent / "parent-escape.txt").exists())
        self.assertFalse((OWNER_HOME / "ba-escape.txt").exists())

    @criteria("A5", "A6")
    def test_owner_home_state_and_credential_are_unreadable(self):
        commands = [
            f"ls {OWNER_HOME} >/dev/null 2>&1 && echo HOME_LIST_OK || echo HOME_LIST_BLOCKED",
            f"cat {self.canary} >/dev/null 2>&1 && echo CANARY_OK || echo CANARY_BLOCKED",
            f"cat {self.agent_home}/state/agent.sqlite3 >/dev/null 2>&1"
            " && echo STATE_OK || echo STATE_BLOCKED",
        ]
        if INSTALLED_CREDENTIAL.exists():
            commands.append(
                f"cat {INSTALLED_CREDENTIAL} >/dev/null 2>&1 && echo CRED_OK || echo CRED_BLOCKED")
        result = self.run_command("; ".join(commands))
        for marker in ("HOME_LIST", "CANARY", "STATE"):
            self.assertIn(marker + "_BLOCKED", result.content)
        if INSTALLED_CREDENTIAL.exists():
            self.assertIn("CRED_BLOCKED", result.content)
        self.assertNotIn("OWNER-HOME-CANARY", result.content)
        self.assertNotIn("STATE-CANARY", result.content)
        for value in real_credential_needles().values():
            if value:
                self.assertNotIn(value, result.content)

    @criteria("A6", "A11")
    def test_environment_is_scrubbed(self):
        result = self.run_command("env")
        self.assertNotIn("ghu_EnvCanary", result.content)
        self.assertNotIn("worker-key-canary", result.content)
        self.assertNotIn("BRAINSTEM_AGENT_WORKER_KEY", result.content)
        self.assertIn("HOME=", result.content)
        self.assertNotIn(f"HOME={OWNER_HOME}\n", result.content)

    @criteria("A8")
    def test_timeout_kills_the_whole_process_group(self):
        started = time.monotonic()
        result = self.run_command("sleep 30 & echo $! > bg.pid; sleep 30; echo never",
                                  timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(result.ok)
        self.assertTrue(result.evidence["timed_out"])
        self.assertNotIn("never", result.content)
        pid = int((self.workspace / "bg.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("background process survived the timeout")

    @criteria("A8")
    def test_cancellation_stops_the_command(self):
        ctx = context(self.workspace)
        outcome = {}

        def run():
            outcome["result"] = self.run_command("sleep 30; echo never", ctx=ctx)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.5)
        cancelled_at = time.monotonic()
        ctx.cancelled.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - cancelled_at, 5)
        self.assertFalse(outcome["result"].ok)
        self.assertNotIn("never", outcome["result"].content)

    @criteria("A5")
    def test_refuses_to_run_without_the_sandbox(self):
        organ = ShellOrgan(
            run_root=self.agent_home / "run", deny_read=(self.agent_home,),
            environ={"BRAINSTEM_AGENT_SANDBOX_EXEC": "/nonexistent/sandbox-exec"},
        )
        arguments = validate_arguments(self.spec.parameters, {"command": "echo ran > ran.txt"})
        with self.assertRaises(OrganError) as caught:
            organ.invoke(context(self.workspace), "run_command", arguments)
        self.assertIn("sandbox", str(caught.exception).lower())
        self.assertFalse((self.workspace / "ran.txt").exists())


class _RaisingEvent:
    """A cancellation flag whose third poll raises, like Ctrl-C landing in the wait loop."""

    def __init__(self):
        self.calls = 0

    def is_set(self):
        self.calls += 1
        if self.calls == 3:
            raise KeyboardInterrupt
        return False

    def set(self):
        pass


@unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
class ShellMembraneProbeTests(unittest.TestCase):
    """Real denial probes against the shell profile; denials must be the kernel's EPERM."""

    setUp = ShellOrganTests.setUp
    run_command = ShellOrganTests.run_command

    @staticmethod
    def probe(label, command):
        return (f"{command} >/dev/null 2>{label}.err && echo {label}_OK || "
                f"(grep -q 'Operation not permitted' {label}.err && echo {label}_EPERM || "
                f"echo {label}_OTHER)")

    def sleeper(self):
        process = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        self.addCleanup(process.wait, 10)
        self.addCleanup(kill_quietly, process.pid)
        return process

    @criteria("A5", "A6")
    def test_reads_and_writes_outside_the_workspace_fail_with_eperm(self):
        probes = {
            "READ_HOME_FILE": f"cat {self.canary}",
            "LIST_HOME": f"ls {OWNER_HOME}",
            "READ_STATE": f"cat {self.agent_home}/state/agent.sqlite3",
            "WRITE_OUTSIDE": f"sh -c 'echo x > {self.outside}/escape.txt'",
            "WRITE_HOME": f"sh -c 'echo x > {OWNER_HOME}/ba-escape-{uuid.uuid4().hex}'",
            "WRITE_STATE": f"sh -c 'echo x >> {self.agent_home}/state/agent.sqlite3'",
        }
        if INSTALLED_CREDENTIAL.exists():
            probes["READ_CREDENTIAL"] = f"cat {INSTALLED_CREDENTIAL}"
        result = self.run_command("; ".join(self.probe(k, v) for k, v in probes.items()))
        for label in probes:
            self.assertIn(f"{label}_EPERM", result.content, label)
        self.assertEqual((self.agent_home / "state" / "agent.sqlite3").read_text(), "STATE-CANARY")
        self.assertEqual(list(self.outside.iterdir()), [])

    @criteria("A5", "A6")
    def test_owner_services_are_unreachable_over_mach(self):
        result = self.run_command(
            "/usr/bin/lsappinfo front 2>&1; echo LS_DONE;"
            " /usr/bin/defaults read -g AppleLocale >/dev/null 2>&1 && echo PREFS_OK || echo PREFS_BLOCKED;"
            f" /usr/bin/security find-generic-password -s ba-probe-{uuid.uuid4().hex} 2>&1; echo KC_DONE")
        self.assertIn("LS_DONE", result.content)
        self.assertNotIn("ASN:", result.content, "LaunchServices (open, app launch) is reachable")
        self.assertIn("PREFS_BLOCKED", result.content, "the owner's preferences are readable")
        self.assertIn("SecKeychainSearchCreateFromAttributes", result.content,
                      "the owner's keychain can be searched")

    @criteria("A5", "A6")
    def test_commands_cannot_signal_processes_outside_their_sandbox(self):
        outside = self.sleeper()
        result = self.run_command(
            self.probe("PROBE_SIGNAL", f"kill -0 {outside.pid}") + "; " +
            self.probe("TERM_SIGNAL", f"kill -TERM {outside.pid}") +
            "; sleep 5 & kill $! && echo JOB_CONTROL_OK")
        self.assertIn("PROBE_SIGNAL_EPERM", result.content)
        self.assertIn("TERM_SIGNAL_EPERM", result.content)
        self.assertIn("JOB_CONTROL_OK", result.content)
        self.assertIsNone(outside.poll(), "a sandboxed command signalled an outside process")

    @criteria("A5")
    def test_common_tools_keep_working_inside_the_tightened_sandbox(self):
        result = self.run_command(
            "ls -l / >/dev/null && echo LS_OK; id -un; /usr/bin/git --version >/dev/null && echo GIT_OK;"
            " awk 'BEGIN { print \"AWK_OK\" }'; echo data > t.txt && tar -cf t.tar t.txt && echo TAR_OK;"
            " /usr/bin/perl -e 'print \"PERL_OK\\n\"'")
        for marker in ("LS_OK", "GIT_OK", "AWK_OK", "TAR_OK", "PERL_OK"):
            self.assertIn(marker, result.content)
        self.assertIn(pwd.getpwuid(os.getuid()).pw_name, result.content)

    @criteria("A8")
    def test_a_finished_command_leaves_no_process_in_its_group(self):
        result = self.run_command("sleep 30 & echo $! > bg.pid; echo started")
        self.assertTrue(result.ok, result.content)
        self.assertTrue(result.evidence["group_gone"], result.evidence)
        pid = int((self.workspace / "bg.pid").read_text())
        self.assertTrue(wait_until(lambda: not pid_running(pid), 2.0), "background job survived")

    @criteria("A8")
    def test_an_exception_in_the_wait_loop_still_kills_the_group(self):
        ctx = context(self.workspace, cancelled=_RaisingEvent())
        with self.assertRaises(KeyboardInterrupt):
            self.run_command("echo $$ > leader.pid; exec sleep 30", ctx=ctx)
        pid = int((self.workspace / "leader.pid").read_text())
        self.addCleanup(kill_quietly, pid)
        self.assertTrue(wait_until(lambda: not pid_running(pid), 2.0),
                        "the sandboxed command outlived the interrupted tool call")

    @criteria("A5", "A8")
    def test_setsid_descendants_escape_the_group_but_stay_inside_the_sandbox(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.2)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        outside = self.sleeper()
        daemon = (
            "exec >/dev/null 2>&1; sleep 0.5; echo $$ > daemon.pid;"
            f" /usr/bin/nc -z -w 2 127.0.0.1 {port} && echo NET_OK > net.txt || echo NET_BLOCKED > net.txt;"
            f" (echo x > {self.outside}/daemon-escape.txt) && echo W_OK > write.txt || echo W_BLOCKED > write.txt;"
            f" cat {self.canary} > home.txt 2>/dev/null && echo R_OK > read.txt || echo R_BLOCKED > read.txt;"
            f" kill -0 {outside.pid} && echo S_OK > signal.txt || echo S_BLOCKED > signal.txt;"
            " sleep 30")
        command = ("/usr/bin/perl -MPOSIX -e 'exit 0 if fork; POSIX::setsid(); exec \"/bin/sh\", \"-c\", "
                   "$ARGV[0]' '" + daemon + "'; echo launched")
        result = self.run_command(command)
        self.assertIn("launched", result.content)
        self.assertTrue(wait_until(lambda: (self.workspace / "signal.txt").exists(), 10.0),
                        "the setsid daemon never reported")
        pid = int((self.workspace / "daemon.pid").read_text())
        self.addCleanup(kill_quietly, pid)
        self.assertTrue(pid_running(pid), "documented limit: setsid() escapes the group kill")
        self.assertEqual((self.workspace / "net.txt").read_text().strip(), "NET_BLOCKED")
        self.assertEqual((self.workspace / "write.txt").read_text().strip(), "W_BLOCKED")
        self.assertEqual((self.workspace / "read.txt").read_text().strip(), "R_BLOCKED")
        self.assertEqual((self.workspace / "signal.txt").read_text().strip(), "S_BLOCKED")
        self.assertFalse((self.outside / "daemon-escape.txt").exists())
        with self.assertRaises(OSError):
            listener.accept()[0].close()


if __name__ == "__main__":
    unittest.main()
