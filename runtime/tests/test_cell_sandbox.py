"""A5/A6 unit specs for the Seatbelt membrane (real /usr/bin/sandbox-exec when present)."""

import socket
import subprocess
import unittest
import uuid
from pathlib import Path

from acceptance_support import criteria, private_dir
from brainstem_agent import sandbox
from brainstem_agent.sandbox import SandboxPolicy, SandboxUnavailable


class PolicyTests(unittest.TestCase):
    @criteria("A5")
    def test_missing_sandbox_is_explicit_never_a_silent_fallback(self):
        environ = {"BRAINSTEM_AGENT_SANDBOX_EXEC": "/nonexistent/sandbox-exec"}
        self.assertFalse(sandbox.available(environ=environ))
        with self.assertRaises(SandboxUnavailable):
            sandbox.wrap(["/bin/echo", "hi"], SandboxPolicy(), environ=environ)

    @criteria("A5", "A6")
    def test_policy_paths_must_be_absolute_and_plain(self):
        for bad in (Path("relative/dir"), Path('/tmp/quote"d'), Path("/tmp/back\\slash")):
            with self.subTest(str(bad)), self.assertRaises(ValueError):
                sandbox.render(SandboxPolicy(writable=(bad,)))
        with self.assertRaises(ValueError):
            sandbox.render(SandboxPolicy(network="everything"))

    @criteria("A5")
    def test_environment_label(self):
        self.assertEqual(sandbox.ENVIRONMENT, "macos-seatbelt")


@unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
class SeatbeltBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.inside = private_dir(self)
        self.outside = private_dir(self)
        (self.outside / "secret.txt").write_text("OUTSIDE-SECRET")

    def run_sandboxed(self, command, policy):
        argv = sandbox.wrap(["/bin/sh", "-c", command], policy)
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=self.inside)

    def policy(self, **changes):
        values = dict(
            read_denied=(self.outside,), writable=(self.inside,), network="none",
        )
        values.update(changes)
        return SandboxPolicy(**values)

    @criteria("A5")
    def test_writes_are_confined_to_writable_roots(self):
        result = self.run_sandboxed(
            f"echo in > {self.inside}/ok.txt; echo out > {self.outside}/escape.txt; true",
            self.policy())
        self.assertEqual((self.inside / "ok.txt").read_text(), "in\n")
        self.assertFalse((self.outside / "escape.txt").exists(), result.stderr)

    @criteria("A5", "A6")
    def test_read_denied_subtree_is_unreadable_and_readable_reallows(self):
        result = self.run_sandboxed(f"cat {self.outside}/secret.txt", self.policy())
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("OUTSIDE-SECRET", result.stdout)
        allowed = self.run_sandboxed(
            f"cat {self.outside}/secret.txt",
            self.policy(readable=(self.outside,)))
        self.assertIn("OUTSIDE-SECRET", allowed.stdout)

    @criteria("A5", "A6")
    def test_write_denied_wins_inside_writable(self):
        protected = self.inside / "protected.txt"
        protected.write_text("original")
        self.run_sandboxed(
            f"echo changed > {protected}; rm -f {protected}; true",
            self.policy(write_denied=(protected,)))
        self.assertEqual(protected.read_text(), "original")

    @criteria("A5")
    def test_network_none_blocks_remote_and_loopback(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.2)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        result = self.run_sandboxed(
            f"/usr/bin/nc -z -w 3 127.0.0.1 {port} && echo LOOPBACK_OK || echo LOOPBACK_BLOCKED;"
            " /usr/bin/curl -sS -m 5 -o /dev/null https://example.com"
            " && echo REMOTE_OK || echo REMOTE_BLOCKED",
            self.policy())
        self.assertIn("LOOPBACK_BLOCKED", result.stdout)
        self.assertIn("REMOTE_BLOCKED", result.stdout)
        with self.assertRaises(OSError):
            listener.accept()[0].close()

    @criteria("A6")
    def test_loopback_port_allowlist(self):
        allowed = socket.socket()
        allowed.bind(("127.0.0.1", 0))
        allowed.listen(1)
        self.addCleanup(allowed.close)
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        self.addCleanup(other.close)
        port, other_port = allowed.getsockname()[1], other.getsockname()[1]
        result = self.run_sandboxed(
            f"/usr/bin/nc -z -w 3 127.0.0.1 {port} && echo ALLOWED_OK || echo ALLOWED_BLOCKED;"
            f" /usr/bin/nc -z -w 3 127.0.0.1 {other_port} && echo OTHER_OK || echo OTHER_BLOCKED",
            self.policy(network="outbound", loopback_ports=(port,)))
        self.assertIn("ALLOWED_OK", result.stdout)
        self.assertIn("OTHER_BLOCKED", result.stdout)

    @criteria("A5")
    def test_profile_file_is_written_when_requested(self):
        profile = self.inside / f"profile-{uuid.uuid4().hex}.sb"
        argv = sandbox.wrap(["/bin/echo", "ok"], self.policy(), profile_path=profile)
        self.assertIn(str(profile), argv)
        self.assertTrue(profile.read_text().startswith("(version 1)"))
        self.assertEqual(subprocess.run(argv, capture_output=True, text=True).stdout, "ok\n")

    @criteria("A5", "A6")
    def test_nested_reallow_inside_a_denied_tree_is_readable_and_siblings_are_not(self):
        nested = self.outside / "workers" / "g1"
        nested.mkdir(parents=True)
        (nested / "own.txt").write_text("OWN")
        result = self.run_sandboxed(
            f"cat {nested}/own.txt; ls {nested} >/dev/null && echo LIST_OK;"
            f" cat {self.outside}/secret.txt 2>&1",
            self.policy(readable=(nested,)))
        self.assertIn("OWN", result.stdout)
        self.assertIn("LIST_OK", result.stdout)
        self.assertNotIn("OUTSIDE-SECRET", result.stdout)
        self.assertIn("Operation not permitted", result.stdout)


def _families(profile: str) -> dict:
    """Map each rule family to the set of SBPL operation names the profile uses for it."""
    families = {}
    for line in profile.splitlines():
        parts = line.strip("()").split()
        if len(parts) < 2 or parts[0] not in ("allow", "deny") or parts[1] == "default":
            continue
        operation = parts[1]
        family = "-".join(operation.rstrip("*").split("-")[:2]) if operation.startswith("file") \
            else operation.rstrip("*").split("-")[0]
        families.setdefault(family, set()).add(operation)
    return families


class PrecedenceTests(unittest.TestCase):
    """SBPL: an operation-specific rule beats a wildcard regardless of order, so every
    deny and re-allow pair in a profile must name exactly the same operation."""

    def profiles(self):
        import sys

        from brainstem_agent.grail import GrailSource
        from brainstem_agent.organs.shell import ShellOrgan
        from brainstem_agent.worker import GrailWorker, WorkerConfig

        root = private_dir(self)
        shell = ShellOrgan(run_root=root / "run", deny_read=(root,))
        yield "shell", shell.policy(root / "workspace", root / "run" / "call")
        worker = GrailWorker(WorkerConfig(
            worker_id="w", home=root, source=GrailSource("0" * 40, root, {"brainstem.py": ""}),
            python=Path(sys.executable), broker_url="http://127.0.0.1:4242"),
            register=lambda *_: "key")
        worker.tree = root / "workers" / "w" / "g"
        yield "worker", worker._policy(4242)

    @criteria("A5", "A6")
    def test_every_rule_family_uses_one_operation_name(self):
        for name, policy in self.profiles():
            with self.subTest(profile=name):
                families = _families(sandbox.render(policy))
                self.assertEqual(families["file-read"], {"file-read-data"})
                self.assertEqual(families["file-write"], {"file-write*"})
                self.assertEqual(len(families["network"]), 1, families["network"])
                for family, operations in families.items():
                    self.assertEqual(len(operations), 1, (family, operations))

    @criteria("A5")
    def test_mixed_operation_names_would_break_reallow(self):
        # Documents the rule the renderer relies on: a wildcard re-allow cannot undo a
        # specific deny, even when it comes later.
        if not sandbox.available():
            self.skipTest("Seatbelt sandbox-exec is unavailable")
        root = private_dir(self)
        (root / "denied" / "sub").mkdir(parents=True)
        (root / "denied" / "sub" / "s.txt").write_text("SUB")
        profile = (f'(version 1)\n(allow default)\n(deny file-read-data (subpath "{root}/denied"))\n'
                   f'(allow file-read* (subpath "{root}/denied/sub"))\n')
        result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat",
                                 f"{root}/denied/sub/s.txt"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SUB", result.stdout)


@unittest.skipUnless(sandbox.available(), "Seatbelt sandbox-exec is unavailable")
class WorkerProfileProbeTests(unittest.TestCase):
    """Real denial probes under the exact profile a Grail worker generation runs in."""

    def setUp(self):
        import hashlib
        import os
        import sys

        from acceptance_support import INSTALLED_CREDENTIAL, REPO
        from brainstem_agent.grail import GrailSource
        from brainstem_agent.worker import GrailWorker, WorkerConfig

        self.home = private_dir(self)
        self.workspace = private_dir(self)
        (self.workspace / "project.txt").write_text("WORKSPACE-SECRET")
        credential_dir = private_dir(self)
        self.credential = credential_dir / ".copilot_token"
        self.credential.write_text('{"access_token": "not-a-real-token"}')
        (self.home / "state").mkdir(mode=0o700)
        (self.home / "state" / "agent.sqlite3").write_text("STATE-CANARY")
        (self.home / "logs" / "workers").mkdir(parents=True, mode=0o700)
        (self.home / "logs" / "workers" / "w.log").write_text("LOG-CANARY")
        seed = private_dir(self)
        files = {"brainstem.py": "print('grail')\n", "agents/basic_agent.py": "class BasicAgent: pass\n",
                 "tests/test_x.py": "\n", ".vscode/settings.json": "{}\n", "soul.md": "soul\n"}
        inventory = {}
        for relative, text in files.items():
            (seed / relative).parent.mkdir(parents=True, exist_ok=True)
            (seed / relative).write_text(text)
            inventory[relative] = hashlib.sha256(text.encode()).hexdigest()
        self.canary = REPO / f".ba-owner-home-canary-{uuid.uuid4().hex}"
        self.canary.write_text("OWNER-HOME-CANARY")
        self.addCleanup(self.canary.unlink)
        self.installed = INSTALLED_CREDENTIAL
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        self.broker_port = self.listener.getsockname()[1]
        self.other = socket.socket()
        self.other.bind(("127.0.0.1", 0))
        self.other.listen(4)
        self.addCleanup(self.other.close)
        config = WorkerConfig(worker_id="probe", home=self.home,
                              source=GrailSource("0" * 40, seed, inventory),
                              python=Path(sys.executable),
                              broker_url=f"http://127.0.0.1:{self.broker_port}",
                              deny_read=(self.workspace, credential_dir))
        self.worker = GrailWorker(config, register=lambda *_: "key")
        self.worker.generation = "g" + uuid.uuid4().hex[:12]
        self.worker._prepare_tree()
        self.addCleanup(self.cleanup_tree)
        self.tree = self.worker.tree
        self.grail = self.tree / "rapp_brainstem"
        self.os = os

    def cleanup_tree(self):
        from acceptance_support import remove_tree

        remove_tree(self.tree)

    def run_probe(self, script):
        argv = sandbox.wrap(["/bin/sh", "-c", script], self.worker._policy(self.broker_port))
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=self.grail,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(self.tree / "home"),
                                   "TMPDIR": str(self.tree / "tmp")})

    @staticmethod
    def probe(label, command):
        return (f"({command}) >/dev/null 2>{label}.err && echo {label}_OK || "
                f"(grep -q 'Operation not permitted' {label}.err && echo {label}_EPERM || "
                f"echo {label}_OTHER); rm -f {label}.err")

    @criteria("A6")
    def test_worker_reads_of_owner_data_state_and_credentials_fail_with_eperm(self):
        probes = {
            "OWNER_HOME": f"cat {self.canary}",
            "OWNER_HOME_LIST": f"ls {Path.home().resolve()}",
            "STATE_DB": f"cat {self.home}/state/agent.sqlite3",
            "WORKER_LOG": f"cat {self.home}/logs/workers/w.log",
            "CREDENTIAL": f"cat {self.credential}",
            "WORKSPACE": f"cat {self.workspace}/project.txt",
        }
        if self.installed.exists():
            probes["INSTALLED_CREDENTIAL"] = f"cat {self.installed}"
        result = self.run_probe("; ".join(self.probe(k, v) for k, v in probes.items()) +
                                f"; cat {self.tree}/soul.md; cat {self.grail}/brainstem.py")
        for label in probes:
            self.assertIn(f"{label}_EPERM", result.stdout, label)
        self.assertIn("Brainstem Agent", result.stdout, "the worker must read its own soul")
        self.assertIn("print('grail')", result.stdout, "the worker must read its own Grail copy")
        for canary in ("OWNER-HOME-CANARY", "STATE-CANARY", "LOG-CANARY", "WORKSPACE-SECRET",
                       "not-a-real-token"):
            self.assertNotIn(canary, result.stdout)

    @criteria("A6", "A7")
    def test_worker_writes_outside_its_runtime_paths_fail_with_eperm(self):
        tracked = self.grail / "brainstem.py"
        before = tracked.read_bytes()
        probes = {
            "OUTSIDE_HOME": f"echo x > {self.home}/escape.txt",
            "OUTSIDE_WORKSPACE": f"echo x > {self.workspace}/escape.txt",
            "OWNER_HOME": f"echo x > {Path.home().resolve()}/ba-escape-{uuid.uuid4().hex}",
            "TRACKED_APPEND": f"echo x >> {tracked}",
            "TRACKED_TRUNCATE": f": > {tracked}",
            "TRACKED_CHMOD": f"chmod 644 {tracked}",
            "TRACKED_UNLINK": f"rm -f {tracked}",
            "TRACKED_RENAME_OVER": f"echo x > {self.grail}/new.py && mv -f {self.grail}/new.py {tracked}",
            "TRACKED_HARDLINK": f"ln {tracked} {self.grail}/alias.py",
            "GRAIL_AGENTS": f"echo x > {self.grail}/agents/evil_agent.py",
            "GRAIL_DIR_RENAME": f"mv {self.grail} {self.tree}/moved",
            "BRIDGE_AGENTS": f"echo x > {self.tree}/agents/evil_agent.py",
            "BRIDGE_UNLINK": f"rm -f {self.tree}/agents/rapp_bridge_agent.py",
            "DOTENV": f"echo X=1 >> {self.grail}/.env",
            "MODEL": f"echo m > {self.grail}/.brainstem_model",
            "SOUL": f"echo x > {self.tree}/soul.md",
        }
        allowed = {
            "RUNTIME_STATE": f"echo s > {self.grail}/.copilot_session",
            "PRIVATE_HOME": f"echo h > {self.tree}/home/h.txt",
            "PRIVATE_TMP": f"echo t > {self.tree}/tmp/t.txt",
        }
        script = "; ".join(self.probe(k, v) for k, v in {**probes, **allowed}.items())
        result = self.run_probe(script)
        for label in probes:
            self.assertIn(f"{label}_EPERM", result.stdout, label)
        for label in allowed:
            self.assertIn(f"{label}_OK", result.stdout, label)
        self.assertEqual(tracked.read_bytes(), before)
        self.assertEqual(self.os.stat(tracked).st_nlink, 1)
        self.assertFalse((self.grail / "alias.py").exists())
        self.assertEqual(sorted(p.name for p in (self.tree / "agents").iterdir()),
                         ["rapp_bridge_agent.py"])
        self.assertFalse((self.home / "escape.txt").exists())
        self.assertFalse((self.workspace / "escape.txt").exists())

    @criteria("A6")
    def test_worker_loopback_is_limited_to_its_broker(self):
        other = self.other.getsockname()[1]
        result = self.run_probe(
            f"/usr/bin/nc -z -w 3 127.0.0.1 {self.broker_port} && echo BROKER_OK || echo BROKER_BLOCKED;"
            f" /usr/bin/nc -z -w 3 127.0.0.1 {other} && echo OTHER_OK || echo OTHER_BLOCKED")
        self.assertIn("BROKER_OK", result.stdout)
        self.assertIn("OTHER_BLOCKED", result.stdout)


if __name__ == "__main__":
    unittest.main()
