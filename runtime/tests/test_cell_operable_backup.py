"""H3 unit specs: backup, restore and export.

The store is copied with SQLite's online backup API while a daemon runs; every file is listed
with its SHA-256 in the manifest; secrets are excluded (and reported) unless asked for;
restore verifies every digest first and refuses on any mismatch without writing; export
writes skills, memory, profile and sessions in documented portable formats.
"""

import json
import os
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from acceptance_support import (CANARY_TOKEN, cli_json, criteria, isolated_env, leaks,
                                private_dir, record_metric, run_cli)
from brainstem_agent import backup, state
from brainstem_agent.organs.skills import parse_skill
from daemon_support import daemon_env, spawn_fake_daemon
from ops_support import MARKER_WORDS, seed_home, tree_snapshot

SECRET = "sk-" + "Q" * 40


def reach_with_secrets(home: Path) -> None:
    (home / "reach.json").write_text(json.dumps({
        "web": {"deny_domains": ["example.com"], "search_provider": "wikipedia"},
        "mcpServers": {
            "notes": {"command": "/usr/bin/true", "args": ["--token", "plain-arg"],
                      "env": {"NOTES_TOKEN": "hunter2hunter2", "MODE": "fast"}},
            "remote": {"url": "https://mcp.example.test/mcp?key=abc123secret",
                       "headers_note": f"Bearer {SECRET}"}}}))
    os.chmod(home / "reach.json", 0o600)


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.home, self.workspace, self.environ = seed_home(self)
        self.scratch = private_dir(self)

    def env(self, home=None, **extra):
        return {**isolated_env(home or self.home, self.scratch),
                "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": self.environ[
                    "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"], **extra}

    def cli(self, *arguments, home=None, workspace=None):
        extra = ["--workspace", str(workspace)] if workspace else []
        result = run_cli([*arguments, *extra, "--json"], self.env(home), timeout=180)
        return result.returncode, cli_json(result)


class BackupTests(BackupCase):
    @criteria("H3", "H12")
    def test_backup_while_the_daemon_runs_and_restore_into_a_fresh_home(self):
        env = daemon_env(isolated_env(self.home, self.scratch),
                         Path(self.environ["BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"]), self.scratch)
        process = spawn_fake_daemon(self.home, self.workspace, env)
        self.addCleanup(lambda: (run_cli(["stop", "--json"], env, timeout=60),
                                 process.communicate(timeout=30)))
        busy = threading.Thread(target=lambda: run_cli(
            ["chat", "[[pause 1.5]] still working", "--workspace", str(self.workspace), "--json"],
            env, timeout=60), daemon=True)
        busy.start()
        time.sleep(0.3)
        target = private_dir(self) / "snapshot"
        started = time.monotonic()
        result = run_cli(["backup", "--output", str(target), "--json"], env, timeout=120)
        backup_seconds = round(time.monotonic() - started, 3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        made = cli_json(result)
        busy.join(30)
        self.assertEqual(made["store"]["method"], "sqlite-online-backup")
        self.assertEqual(made["store"]["integrity_check"], "ok")
        self.assertGreaterEqual(made["store"]["counts"]["chats"], 2)
        self.assertEqual(made["store"]["counts"]["skills"], 1)
        self.assertEqual(made["store"]["counts"]["schedules"], 1)
        self.assertEqual(made["store"]["counts"]["occurrences"], 1)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        manifest = json.loads((target / "manifest.json").read_text())
        for item in manifest["files"]:
            self.assertEqual((target / item["path"]).stat().st_mode & 0o777, 0o600)
        self.assertTrue(backup.verify_backup(target)["ok"])
        excluded = " ".join(item["item"] for item in manifest["excluded"])
        self.assertIn("Copilot credential", excluded)
        self.assertIn("run/", excluded)
        fresh = private_dir(self) / "restored-home"
        started = time.monotonic()
        code, restored = self.cli("restore", str(target), home=fresh)
        restore_seconds = round(time.monotonic() - started, 3)
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored["counts"]["skills"], 1)
        self.assertIn("health", restored)
        code, memory = self.cli("memory", home=fresh, workspace=self.workspace)
        self.assertIn("The owner's favorite color is teal.", [f["text"] for f in memory["facts"]])
        self.assertIn("The owner lives in Lisbon.", [f["text"] for f in memory["facts"]])
        code, inbox = self.cli("inbox", home=fresh, workspace=self.workspace)
        self.assertEqual(len(inbox["inbox"]), 1)
        code, sessions = self.cli("sessions", home=fresh, workspace=self.workspace)
        # The owner's two-turn session, the scheduled run's and (caught mid-turn by the
        # snapshot, as it was at that instant) the chat that ran during the backup.
        self.assertIn(2, [item["turns"] for item in sessions["sessions"]])
        self.assertGreaterEqual(len(sessions["sessions"]), 2)
        self.assertEqual(leaks({"canary": CANARY_TOKEN}, roots=[target, fresh]), [])
        record_metric("h3_backup_restore", {
            "backup_seconds": backup_seconds, "restore_seconds": restore_seconds,
            "daemon_running_during_backup": True, "files": len(manifest["files"]),
            "store_counts": made["store"]["counts"], "verified_digests": True,
            "restored_counts": restored["counts"]})

    @criteria("H3")
    def test_secrets_are_excluded_by_default_and_reported(self):
        reach_with_secrets(self.home)
        code, made = self.cli("backup", "--output", str(private_dir(self) / "b"))
        self.assertEqual(code, 0, made)
        copy = json.loads((Path(made["path"]) / "reach.json").read_text())
        text = json.dumps(copy)
        for secret in ("hunter2hunter2", "abc123secret", SECRET):
            self.assertNotIn(secret, text)
        self.assertEqual(copy["mcpServers"]["notes"]["env"], {"NOTES_TOKEN": "", "MODE": ""})
        self.assertEqual(copy["mcpServers"]["remote"]["url"], "https://mcp.example.test/mcp")
        self.assertEqual(copy["web"]["deny_domains"], ["example.com"])
        reported = [item["item"] for item in made["excluded"]]
        self.assertIn("reach.json: mcpServers.notes.env.NOTES_TOKEN", reported)
        self.assertIn("reach.json: mcpServers.remote.url", reported)
        self.assertTrue(any("headers_note" in item for item in reported))
        self.assertFalse(made["secrets_included"])
        code, kept = self.cli("backup", "--output", str(private_dir(self) / "c"),
                              "--include-secrets")
        self.assertIn("hunter2hunter2", (Path(kept["path"]) / "reach.json").read_text())
        self.assertTrue(kept["secrets_included"])

    @criteria("H3")
    def test_restore_refuses_on_any_digest_mismatch_and_writes_nothing(self):
        code, made = self.cli("backup", "--output", str(private_dir(self) / "b"))
        target = Path(made["path"])
        store = target / "state" / "agent.sqlite3"
        for label, damage, undo in (
                ("changed byte", lambda: store.write_bytes(store.read_bytes()[:-1] + b"\x01"),
                 None),
                ("extra file", lambda: (target / "extra.txt").write_text("x"),
                 lambda: (target / "extra.txt").unlink()),
                ("missing file", lambda: (target / "state" / "agent.sqlite3").rename(
                    target / "moved"), None)):
            with self.subTest(label):
                original = store.read_bytes() if store.exists() else None
                damage()
                fresh = private_dir(self)
                before = tree_snapshot(fresh)
                code, refused = self.cli("restore", str(target), home=fresh)
                self.assertEqual(code, 1)
                self.assertIn("failed verification", refused["error"])
                self.assertEqual(tree_snapshot(fresh), before)
                if undo:
                    undo()
                elif label == "changed byte":
                    store.write_bytes(original)
        self.assertFalse(backup.verify_backup(target)["ok"])

    @criteria("H3")
    def test_restore_protects_an_existing_store_and_refuses_while_a_daemon_runs(self):
        code, made = self.cli("backup", "--output", str(private_dir(self) / "b"))
        code, refused = self.cli("restore", made["path"])
        self.assertEqual(code, 1)
        self.assertIn("already has a store", refused["error"])
        code, replaced = self.cli("restore", made["path"], "--replace")
        self.assertEqual(code, 0, replaced)
        self.assertTrue(Path(replaced["safety_backup"]).is_dir())
        self.assertTrue(backup.verify_backup(replaced["safety_backup"])["ok"])
        env = daemon_env(isolated_env(self.home, self.scratch),
                         Path(self.environ["BRAINSTEM_AGENT_GITHUB_TOKEN_FILE"]), self.scratch)
        process = spawn_fake_daemon(self.home, self.workspace, env)
        self.addCleanup(lambda: (run_cli(["stop", "--json"], env, timeout=60),
                                 process.communicate(timeout=30)))
        result = run_cli(["restore", made["path"], "--replace", "--json"], env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("daemon is running", cli_json(result)["error"])

    @criteria("H3", "H4")
    def test_a_backup_of_an_older_store_restores_and_migrates_with_its_own_safety_copy(self):
        code, made = self.cli("backup", "--output", str(private_dir(self) / "b"))
        target = Path(made["path"])
        store = target / "state" / "agent.sqlite3"
        connection = sqlite3.connect(store)
        for name in ("turn_steps_running", "processes_state", "turn_steps", "processes"):
            connection.execute(f"DROP {'INDEX' if name.endswith(('running', 'state')) else 'TABLE'} "
                               f"{name}")
        connection.commit()
        connection.close()
        manifest = json.loads((target / "manifest.json").read_text())
        for item in manifest["files"]:
            if item["path"] == "state/agent.sqlite3":
                item["sha256"] = backup._sha256(store)
                item["bytes"] = store.stat().st_size
        (target / "manifest.json").write_text(json.dumps(manifest))
        fresh = private_dir(self)
        code, restored = self.cli("restore", str(target), home=fresh)
        self.assertEqual(code, 0, restored)
        self.assertTrue(restored["migrated"])
        self.assertEqual(len(list((fresh / "state" / "pre-migration").glob("*.sqlite3"))), 1)
        self.assertEqual(state.inspect_database(fresh / "state" / "agent.sqlite3")[
            "compatibility"], "current")


class ExportTests(BackupCase):
    @criteria("H3")
    def test_export_writes_documented_portable_formats(self):
        target = private_dir(self) / "export"
        code, exported = self.cli("export", "--output", str(target), workspace=self.workspace)
        self.assertEqual(code, 0, exported)
        # Two owner turns in one session (each remembered the color) and the scheduled run's.
        self.assertEqual(exported["counts"], {"skills": 1, "sessions": 2, "memory": 2,
                                              "profile": 1})
        skill = parse_skill((target / "skills" / "workspace-make-todo.md").read_text())
        self.assertEqual(skill["name"], "make-todo")
        self.assertEqual(skill["steps"], ["List the tasks", "Write todo.md"])
        memory = [json.loads(line) for line in (target / "memory.jsonl").read_text().splitlines()]
        self.assertEqual(memory[0]["text"], "The owner's favorite color is teal.")
        self.assertEqual(memory[0]["scope"], "workspace")
        profile = [json.loads(line) for line in (target / "profile.jsonl").read_text().splitlines()]
        self.assertEqual(profile[0]["scope"], "profile")
        conversations = [[json.loads(line) for line in path.read_text().splitlines()]
                         for path in (target / "sessions").glob("*.jsonl")]
        messages = max(conversations, key=len)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"] * 2)
        self.assertIn(MARKER_WORDS, messages[0]["content"])
        manifest = json.loads((target / "export-manifest.json").read_text())
        self.assertEqual(set(manifest["formats"]), set(backup.EXPORT_FORMATS))
        for item in manifest["files"]:
            self.assertEqual(backup._sha256(target / item["path"]), item["sha256"])
        code, again = self.cli("export", "--output", str(target), workspace=self.workspace)
        self.assertEqual(code, 1)
        self.assertIn("already exists", again["error"])
