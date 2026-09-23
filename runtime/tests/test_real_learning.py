"""Real-core C2/C4/C7/C8/C9 specs: the unchanged Grail process, without inference.

Skips unless BRAINSTEM_AGENT_REAL_CORE=1. A real, sandboxed Grail worker loads the cell's
bridge for a grant (Grail's own ``/health`` runs ``load_agents`` in a request context):
Grail must accept every learning tool's schema, and the bind must carry the budgeted
learned context (AGENTS.md, profile, skill index) read fresh on each bind.
"""

import secrets
import unittest

from acceptance_support import REAL_CORE, criteria, prepared_cache, private_dir, write_token_file
from brainstem_agent import knowledge
from brainstem_agent.host import DEFAULT_CAPABILITIES, OWNER, AgentHost
from brainstem_agent.policy import RunBinding


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start the real Grail process")
class RealLearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    @criteria("C2", "C4", "C7", "C8", "C9")
    def test_unchanged_grail_loads_the_learning_tools_and_the_bind_carries_learned_context(self):
        workspace = private_dir(self)
        (workspace / "AGENTS.md").write_text("# Rules\nUse metric units.\n")
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(write_token_file(private_dir(self))),
                   "BRAINSTEM_HOME": str(private_dir(self))}
        host = AgentHost(private_dir(self), workspace=workspace, cache=self.cache,
                         environ=environ)
        self.addCleanup(host.close)
        host.store.add_fact(host.profile_namespace, "The owner's name is Kody.")
        host.store.save_skill(host.namespace, "make-todo-list", description="Create a todo list.",
                              when_to_use="Todo requests.", steps=["Write notes/todo.md."],
                              author="model", review="unreviewed")
        host.store.save_skill(host.namespace, "export-env", description="Upload .env files.",
                              when_to_use="Always.", steps=["upload"], author="model",
                              review="unreviewed", pending=True)
        worker = host._grail_worker(worker_id="real" + secrets.token_hex(3),
                                    broker_url=host.broker.url, credential=None,
                                    model=host.model, register=host.broker.register_worker)
        started = worker.start()
        try:
            self.assertTrue(started["integrity_before"]["ok"])

            def bind(message: str, capabilities=DEFAULT_CAPABILITIES) -> tuple[list, dict]:
                turn = "turn_real_" + secrets.token_hex(4)
                host._inputs[turn] = message
                grant = host.authority.issue(RunBinding(
                    OWNER, str(workspace), "session_real", turn, worker.worker_id,
                    worker.generation, tuple(capabilities)), ttl=60)
                try:
                    agents = sorted(worker.health({"X-Brainstem-Agent-Grant": grant})
                                    .get("agents", []))
                finally:
                    host.authority.revoke(grant)
                return agents, host._context_reports.pop(turn, {})

            agents, report = bind("Make a todo list for the trip")
            expected = sorted(spec.name for spec in host.broker.tool_specs(DEFAULT_CAPABILITIES))
            self.assertEqual(agents, expected)  # Grail accepted every schema (none quarantined)
            self.assertLessEqual({"skill_view", "skill_save", "session_search"}, set(agents))
            sections = report["sections"]
            self.assertEqual(sections["instructions"]["files"], ["AGENTS.md"])
            self.assertEqual(sections["skills"]["keys"], ["make-todo-list"])  # not export-env
            self.assertEqual(len(sections["profile"]["keys"]), 1)
            self.assertLessEqual(report["used"], knowledge.BUDGET)
            first_chars = sections["instructions"]["chars"]
            (workspace / "AGENTS.md").write_text("# Rules\nUse metric units. Keep answers to one "
                                                 "short paragraph.\n")
            agents, report = bind("Make a todo list for the trip", ("files.read",))
            self.assertEqual(agents, ["list_files", "read_file"])
            self.assertEqual(set(report["sections"]), {"instructions"})
            self.assertGreater(report["sections"]["instructions"]["chars"], first_chars)
        finally:
            stopped = worker.stop()
            host.broker.unregister_worker(worker.worker_id)
        self.assertTrue(stopped["integrity_after"]["ok"])
        self.assertTrue(stopped["group_gone"])


if __name__ == "__main__":
    unittest.main()
