"""Real-core E4/E6/E11 specs: the reaching cell's tools inside unchanged Grail.

Skips unless BRAINSTEM_AGENT_REAL_CORE=1. No inference: a real Grail worker (pinned,
byte-verified, in its Seatbelt profile, no credential) loads the bridge per request; its
``/health`` lists the tools Grail validated and loaded for a grant.
"""

import unittest

from acceptance_support import REAL_CORE, criteria, prepared_cache, private_dir, write_token_file
from reach_support import notes_server


@unittest.skipUnless(REAL_CORE, "set BRAINSTEM_AGENT_REAL_CORE=1 to start unchanged Grail")
class RealReachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = prepared_cache()

    @criteria("E4", "E6", "E11")
    def test_unchanged_grail_loads_the_web_tools_and_only_the_granted_servers_tools(self):
        import json

        from brainstem_agent.host import (CORE_CAPABILITIES, DEFAULT_CAPABILITIES, OWNER,
                                          TURN_CAPABILITIES, AgentHost)
        from brainstem_agent.policy import RunBinding

        home, workspace = private_dir(self), private_dir(self)
        (home / "reach.json").write_text(json.dumps({"mcpServers": {
            "notes": notes_server(private_dir(self)),
            "other": notes_server(private_dir(self), allow=["note_list"])}}))
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(write_token_file(private_dir(self))),
                   "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(private_dir(self))}
        host = AgentHost(home, workspace=workspace, cache=self.cache, environ=environ)
        self.addCleanup(host.close)
        host.mcp_organ.prepare(["mcp.notes", "mcp.other"])
        worker = host._grail_worker(worker_id="real", broker_url=host.broker.url,
                                    credential=None, model="auto",
                                    register=host.broker.register_worker)
        worker.start()
        self.addCleanup(worker.stop)

        def advertised(capabilities):
            grant = host.authority.issue(RunBinding(
                OWNER, str(host.workspace), "s", "t" + str(len(capabilities)), worker.worker_id,
                worker.generation, tuple(capabilities)), ttl=60)
            return sorted(worker.health({"X-Brainstem-Agent-Grant": grant})["agents"])

        chosen = [*TURN_CAPABILITIES, "mcp.notes"]
        loaded = advertised(chosen)
        self.assertEqual(loaded, sorted(s.name for s in host.broker.tool_specs(chosen)),
                         "Grail validated and loaded every web and MCP tool")
        for tool in ("web_fetch", "web_search", "mcp__notes__note_get", "mcp__notes__note_put"):
            self.assertIn(tool, loaded)
        self.assertFalse([name for name in loaded if name.startswith("mcp__other__")])
        self.assertEqual([name for name in advertised(["mcp.other"])], ["mcp__other__note_list"],
                         "the allow list holds inside Grail too")
        for unattended in (CORE_CAPABILITIES, DEFAULT_CAPABILITIES):
            names = advertised(unattended)
            self.assertFalse([name for name in names if name.startswith(("web_", "mcp__"))])
        stopped = worker.stop()
        self.assertTrue(stopped["integrity_after"]["ok"], stopped["integrity_after"])


if __name__ == "__main__":
    unittest.main()
