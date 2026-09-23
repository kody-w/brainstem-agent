from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from brainstem_agent.harness import HarnessError, OfflineHarness, TurnUnavailable
from brainstem_agent.policy import GrantAuthority, GrantDenied, RunBinding, fixture_namespace
from brainstem_agent.state import StateError, Store


class SimulatedCrash(BaseException):
    pass


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.database = self.root / "state.sqlite3"
        self.store = Store(self.database)
        self.addCleanup(self.store.close)
        self.now = 1000.0
        self.authority = GrantAuthority(self.store, clock=lambda: self.now)

    def binding(self):
        return RunBinding(
            "alice", "project", "session", "turn", "worker", "generation",
            ("fixture.write",),
        )

    def harness(self, transport, **kwargs):
        return OfflineHarness(
            self.store, self.authority, self.workspace, transport, **kwargs,
        )


class GrantTests(FixtureCase):
    def test_live_authority_is_refused(self):
        with self.assertRaises(GrantDenied):
            GrantAuthority(self.store, mode="live")

    def test_exact_binding_and_capability_are_required(self):
        binding = self.binding()
        token = self.authority.issue(binding)
        self.authority.authorize(token, binding, "fixture.write")
        for field in ("owner", "workspace", "session_id", "turn_id", "worker_id", "generation"):
            with self.subTest(field=field), self.assertRaises(GrantDenied):
                self.authority.authorize(
                    token, replace(binding, **{field: "other"}), "fixture.write",
                )
        with self.assertRaises(GrantDenied):
            self.authority.authorize(token, binding, "shell.execute")
        with self.assertRaises(GrantDenied):
            self.authority.authorize("unknown-token", binding, "fixture.write")

    def test_model_cannot_widen_capability_binding(self):
        binding = self.binding()
        token = self.authority.issue(binding)
        widened = replace(binding, capabilities=("fixture.write", "shell.execute"))
        with self.assertRaises(GrantDenied):
            self.authority.authorize(token, widened, "shell.execute")

    def test_revocation_survives_reopen(self):
        binding = self.binding()
        token = self.authority.issue(binding)
        self.authority.revoke(token)
        with Store(self.database) as reopened:
            other = GrantAuthority(reopened, clock=lambda: self.now)
            with self.assertRaises(GrantDenied):
                other.authorize(token, binding, "fixture.write")

    def test_observed_expiry_revokes_and_cannot_revive_on_reopen(self):
        binding = self.binding()
        token = self.authority.issue(binding, ttl=10)
        self.now = 1010
        with self.assertRaises(GrantDenied):
            self.authority.authorize(token, binding, "fixture.write")
        with Store(self.database) as reopened:
            rolled_back = GrantAuthority(reopened, clock=lambda: 1001)
            with self.assertRaises(GrantDenied):
                rolled_back.authorize(token, binding, "fixture.write")

    def test_in_process_clock_regression_refuses(self):
        binding = self.binding()
        token = self.authority.issue(binding)
        self.now = 1010
        self.authority.authorize(token, binding, "fixture.write")
        self.now = 1005
        with self.assertRaises(GrantDenied):
            self.authority.authorize(token, binding, "fixture.write")

    def test_invalid_ttl_clock_and_capabilities_refuse(self):
        for ttl in (0, -1, 301, float("nan"), float("inf"), True, "10", 10**1000):
            with self.subTest(ttl=str(ttl)[:20]), self.assertRaises(GrantDenied):
                self.authority.issue(self.binding(), ttl=ttl)
        for capabilities in ((), ("*",), ("fixture.write", "fixture.write"), ([],)):
            with self.subTest(capabilities=capabilities), self.assertRaises(GrantDenied):
                replace(self.binding(), capabilities=capabilities)
        self.now = float("nan")
        with self.assertRaises(GrantDenied):
            self.authority.issue(self.binding())


class HarnessTests(FixtureCase):
    def test_live_mode_refuses_without_dispatch(self):
        def forbidden(request, context):
            self.fail("Transport must not run.")
        with self.assertRaises(HarnessError):
            self.harness(forbidden, mode="live")

    def test_replay_survives_restart_with_changed_content(self):
        calls = []

        def transport(request, context):
            calls.append(request)
            result = context.tools.write(
                context, name="result.txt", text="fixture", request_key="write",
            )
            self.assertEqual(result["bytes"], 7)
            return {
                "response": "Fixture completed.",
                "session_id": request["session_id"],
                "agent_logs": "write completed",
                "model": "synthetic",
            }

        harness = self.harness(transport)
        first = harness.run("alice", "write", workspace="project", idempotency_key="one")
        replay = harness.run("alice", "different input", workspace="project", idempotency_key="one")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.response, replay.response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(first.response), {"response", "agent_logs", "session_id"})
        self.assertEqual((self.workspace / "result.txt").read_text(), "fixture")
        with Store(self.database) as reopened:
            after_restart = OfflineHarness(
                reopened, GrantAuthority(reopened, clock=lambda: self.now),
                self.workspace, transport,
            )
            result = after_restart.run(
                "alice", "third input", workspace="project", idempotency_key="one",
            )
            self.assertTrue(result.replayed)
            self.assertEqual(len(calls), 1)

    def test_successful_tool_result_deduplicates_inside_turn(self):
        def transport(request, context):
            arguments = {"name": "once.txt", "text": "once", "request_key": "one"}
            first = context.tools.write(context, **arguments)
            second = context.tools.write(context, **arguments)
            self.assertEqual(first, second)
            return {"response": "done", "agent_logs": [], "session_id": request["session_id"]}
        self.harness(transport).run("alice", "write once", workspace="project")
        self.assertEqual((self.workspace / "once.txt").read_text(), "once")

    def test_history_is_owned_and_supplied_on_next_turn(self):
        observed = []

        def transport(request, context):
            observed.append(request)
            return {"response": "answer", "agent_logs": [], "session_id": request["session_id"]}

        harness = self.harness(transport)
        first = harness.run("alice", "first", workspace="project")
        harness.run(
            "alice", "second", workspace="project", session_id=first.response["session_id"],
        )
        harness.run("bob", "private", workspace="other")
        self.assertEqual(observed[1]["conversation_history"], [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
        ])
        self.assertEqual(observed[2]["conversation_history"], [])

    def test_workspace_replay_namespaces_are_separate(self):
        calls = []

        def transport(request, context):
            calls.append(context.binding.workspace)
            return {
                "response": request["user_input"],
                "agent_logs": [],
                "session_id": request["session_id"],
            }

        harness = self.harness(transport)
        first = harness.run("alice", "private A", workspace="A", idempotency_key="shared")
        second = harness.run("alice", "private B", workspace="B", idempotency_key="shared")
        self.assertFalse(second.replayed)
        self.assertNotEqual(first.response["session_id"], second.response["session_id"])
        self.assertEqual(second.response["response"], "private B")
        self.assertEqual(calls, ["A", "B"])

    def test_existing_session_cannot_move_to_another_workspace(self):
        calls = []

        def transport(request, context):
            calls.append(request)
            return {"response": "private", "agent_logs": [], "session_id": request["session_id"]}

        harness = self.harness(transport)
        first = harness.run("alice", "private A", workspace="A")
        with self.assertRaises(StateError):
            harness.run(
                "alice", "read A from B", workspace="B",
                session_id=first.response["session_id"],
            )
        self.assertEqual(len(calls), 1)

    def test_error_shaped_success_is_uncertain_not_retried(self):
        calls = []

        def transport(request, context):
            calls.append(request)
            return {"response": "Sign in", "no_copilot_access": True}

        harness = self.harness(transport)
        with self.assertRaises(HarnessError):
            harness.run("alice", "request", workspace="project", idempotency_key="one")
        with self.assertRaises(TurnUnavailable):
            harness.run("alice", "retry", workspace="project", idempotency_key="one")
        self.assertEqual(len(calls), 1)
        retained = self.store.reserve_chat(
            fixture_namespace("alice", "project"), "inspect", idempotency_key="one",
        )
        self.assertEqual(retained.state, "uncertain")
        self.assertIsNone(retained.response)

    def test_completed_turn_revokes_its_grant(self):
        captured = []

        def transport(request, context):
            captured.append(context)
            return {"response": "done", "agent_logs": [], "session_id": request["session_id"]}

        self.harness(transport).run("alice", "request", workspace="project")
        with self.assertRaises(GrantDenied):
            captured[0].tools.write(
                captured[0], name="late.txt", text="denied", request_key="late",
            )
        self.assertFalse((self.workspace / "late.txt").exists())

    def test_worker_reentrance_refuses_without_deadlock(self):
        def transport(request, context):
            with self.assertRaises(TurnUnavailable):
                harness.run("alice", "nested", workspace="project")
            return {"response": "done", "agent_logs": [], "session_id": request["session_id"]}
        harness = self.harness(transport)
        harness.run("alice", "request", workspace="project")

    def test_crash_after_effect_preserves_uncertainty_and_no_replay(self):
        jobs = []

        def crash(job_id):
            jobs.append(job_id)
            raise SimulatedCrash()

        def transport(request, context):
            context.tools.write(context, name="written.txt", text="exists", request_key="write")
            self.fail("Crash must interrupt completion.")

        harness = self.harness(transport, after_effect=crash)
        with self.assertRaises(SimulatedCrash):
            harness.run("alice", "write", workspace="project", idempotency_key="crash")
        self.assertEqual((self.workspace / "written.txt").read_text(), "exists")
        with Store(self.database) as reopened:
            reopened.recover_interrupted()
            namespace = fixture_namespace("alice", "project")
            self.assertEqual(reopened.get_job(namespace, jobs[0]).state, "uncertain")
            retained = reopened.reserve_chat(namespace, "retry", idempotency_key="crash")
            self.assertEqual(retained.state, "uncertain")
            after_restart = OfflineHarness(
                reopened, GrantAuthority(reopened, clock=lambda: self.now),
                self.workspace, transport,
            )
            with self.assertRaises(TurnUnavailable):
                after_restart.run("alice", "retry", workspace="project", idempotency_key="crash")
        self.assertEqual(len(jobs), 1)

    def test_fixture_paths_do_not_escape_or_follow_leaf_links(self):
        outside = self.root / "outside.txt"
        outside.write_text("do not change")
        (self.workspace / "linked.txt").symlink_to(outside)

        def transport(request, context):
            for name in ("../outside.txt", "/tmp/absolute.txt", ".hidden", "a/b.txt"):
                with self.subTest(name=name), self.assertRaises(HarnessError):
                    context.tools.write(context, name=name, text="bad", request_key="escape")
            with self.assertRaises(OSError):
                context.tools.write(
                    context, name="linked.txt", text="bad", request_key="symlink",
                )
            return {"response": "denied", "agent_logs": [], "session_id": request["session_id"]}

        self.harness(transport).run("alice", "denial fixtures", workspace="project")
        self.assertEqual(outside.read_text(), "do not change")

    def test_revoked_grant_cannot_write(self):
        def transport(request, context):
            self.authority.revoke(context.grant)
            with self.assertRaises(GrantDenied):
                context.tools.write(context, name="denied.txt", text="no", request_key="write")
            return {"response": "denied", "agent_logs": [], "session_id": request["session_id"]}
        self.harness(transport).run("alice", "request", workspace="project")
        self.assertFalse((self.workspace / "denied.txt").exists())


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        runtime = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, "-m", "brainstem_agent", *arguments],
            capture_output=True, text=True, timeout=15,
            env={"PATH": os.defpath, "PYTHONPATH": str(runtime)},
        )

    def test_cli_evidence_is_explicitly_synthetic(self):
        result = self.run_cli("fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["stage"], "offline-contract-verified")
        self.assertFalse(report["real_grail_executed"])
        self.assertFalse(report["sandbox_qualified"])
        self.assertFalse(report["native_rapp_activated"])
        self.assertTrue(all(report["checks"].values()))

    def test_live_cli_is_refused_before_writing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence.json"
            result = self.run_cli("fixture", "--mode", "live", "--output", str(output))
            self.assertEqual(result.returncode, 2)
            self.assertIn("not implemented", result.stderr)
            self.assertFalse(output.exists())

    def test_cli_never_overwrites_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence.json"
            output.write_text("original")
            result = self.run_cli("fixture", "--output", str(output))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
