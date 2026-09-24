"""H6 unit specs: uninstall.

``uninstall --dry-run`` lists exactly what ``uninstall`` removes (the service plist in the
owner's LaunchAgents directory, the daemon, caches, worker trees, runtime records, logs and
installed versions); the home goes only with ``--remove-home`` and an exact ``--confirm``; the
installed RAPP Brainstem is never touched. Tests use a temporary LaunchAgents directory, a fake
launchctl and a temporary HOME holding a stand-in brainstem.
"""

import hashlib
import os
import unittest
from pathlib import Path

from acceptance_support import (cli_json, criteria, pid_running, private_dir, run_cli,
                                wait_until, write_token_file)
from brainstem_agent import daemon, release
from daemon_support import spawn_fake_daemon
from ops_support import fake_brainstem, fake_launchctl, seed_home, tree_snapshot


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in sorted(os.walk(root)):
        for name in sorted(files):
            path = Path(directory) / name
            digest.update(str(path.relative_to(root)).encode() + path.read_bytes())
    return digest.hexdigest()


class UninstallCase(unittest.TestCase):
    def setUp(self):
        self.home, self.workspace, _environ = seed_home(self)
        self.fake_home = private_dir(self)
        self.brainstem = fake_brainstem(self.fake_home, "ghu_" + "Installed" * 4)
        self.brainstem_digest = digest_tree(self.brainstem)
        self.agents = private_dir(self)
        (self.agents / "com.example.unrelated.plist").write_text("<plist/>")
        self.launchctl, self.calls = fake_launchctl(private_dir(self))
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.fake_home), "LANG": "en_US.UTF-8",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "BRAINSTEM_AGENT_HOME": str(self.home),
                    "BRAINSTEM_AGENT_LAUNCH_AGENTS": str(self.agents),
                    "BRAINSTEM_AGENT_LAUNCHCTL": str(self.launchctl)}
        code, self.service = self.cli("service", "install")
        self.assertEqual(code, 0, self.service)
        for name in ("workers/w1/g1", "run/shell/c1", "logs/workers", "cache/grail",
                     "workspaces/default/notes"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
            (self.home / name / "file").write_text("x")
        release.install_tree(self.home, release.PACKAGE.parent, origin="test")
        self.addCleanup(self.stop_daemon)

    def cli(self, *arguments, env=None):
        result = run_cli([*arguments, "--json"], env or self.env, timeout=120)
        return result.returncode, cli_json(result)

    def stop_daemon(self):
        record = daemon.read_record(self.home)
        if record is not None:
            run_cli(["stop", "--json"], self.env, timeout=60)
            if pid_running(record["pid"]):
                os.kill(record["pid"], 9)

    def assert_brainstem_untouched(self):
        self.assertEqual(digest_tree(self.brainstem), self.brainstem_digest)
        self.assertTrue((self.agents / "com.example.unrelated.plist").is_file())


class UninstallTests(UninstallCase):
    @criteria("H6")
    def test_the_dry_run_lists_exactly_what_uninstall_then_removes(self):
        before = (tree_snapshot(self.home), tree_snapshot(self.agents))
        code, plan = self.cli("uninstall", "--dry-run")
        self.assertEqual(code, 0, plan)
        self.assertEqual((tree_snapshot(self.home), tree_snapshot(self.agents)), before,
                         "a dry run changes nothing")
        listed = {item["kind"]: item.get("path") for item in plan["remove"]}
        self.assertEqual(set(listed), {"service", "workers", "run", "logs", "versions", "cache"})
        self.assertEqual(listed["service"], self.service["path"])
        kept = [item["path"] for item in plan["keep"]]
        self.assertIn(str(self.brainstem), kept)
        self.assertIn(str(self.home / "state"), kept)
        code, done = self.cli("uninstall")
        self.assertEqual(code, 0, done)
        self.assertEqual({item["kind"] for item in done["removed"]}, set(listed))
        for kind, path in listed.items():
            self.assertFalse(Path(path).exists(), kind)
        self.assertEqual(sorted(os.listdir(self.home)), ["state", "workspaces"])
        self.assertIn(f"bootout gui/{os.getuid()} {self.service['path']}",
                      self.calls.read_text())
        self.assert_brainstem_untouched()

    @criteria("H6")
    def test_removing_the_home_needs_an_exact_confirmation(self):
        code, plan = self.cli("uninstall", "--remove-home", "--dry-run")
        self.assertEqual(code, 0)
        home_item = next(item for item in plan["remove"] if item["kind"] == "home")
        self.assertGreaterEqual(home_item["store_counts"]["chats"], 2)
        self.assertIn("workspaces", home_item["includes"])
        before = tree_snapshot(self.home)
        code, refused = self.cli("uninstall", "--remove-home")
        self.assertEqual(code, 2)
        self.assertTrue(refused["needs_confirmation"])
        self.assertIn("--confirm", refused["refused"][0])
        code, refused = self.cli("uninstall", "--remove-home", "--confirm", str(self.fake_home))
        self.assertEqual(code, 2)
        self.assertEqual(tree_snapshot(self.home), before)
        code, done = self.cli("uninstall", "--remove-home", "--confirm", str(self.home))
        self.assertEqual(code, 0, done)
        self.assertFalse(self.home.exists())
        self.assert_brainstem_untouched()

    @criteria("H6")
    def test_uninstall_stops_a_running_daemon(self):
        token = write_token_file(private_dir(self))
        env = {**self.env, "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token)}
        process = spawn_fake_daemon(self.home, self.workspace, env)
        code, plan = self.cli("uninstall", "--dry-run", env=env)
        self.assertIn("daemon", [item["kind"] for item in plan["remove"]])
        code, done = self.cli("uninstall", env=env)
        self.assertEqual(code, 0, done)
        process.communicate(timeout=30)
        self.assertIsNone(daemon.read_record(self.home))
        self.assertFalse(pid_running(process.pid))
        self.assert_brainstem_untouched()

    @criteria("H6")
    def test_a_home_overlapping_the_brainstem_or_holding_foreign_files_is_refused(self):
        inside = self.brainstem / "agent-home"
        inside.mkdir()
        env = {**self.env, "BRAINSTEM_AGENT_HOME": str(inside)}
        code, refused = self.cli("uninstall", "--remove-home", "--confirm", str(inside), env=env)
        self.assertEqual(code, 1)
        self.assertIn("installed RAPP Brainstem", refused["refused"][0])
        (self.home / "Documents").mkdir()
        code, refused = self.cli("uninstall", "--remove-home", "--confirm", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("did not create", " ".join(refused["refused"]))
        self.assertTrue((self.home / "state").is_dir())
        self.assert_brainstem_untouched()
