"""A2/A6 unit specs for the bridge agent, loaded exactly the way Grail loads agents.

Grail (brainstem.py lines 1640-1700) executes every AGENTS_PATH/*_agent.py fresh per
request with spec_from_file_location + exec_module, then instantiates every public
class whose __module__ is that module and that has perform(). This test re-states
that loader (it does not import Grail) and fakes only ``flask.request``.
"""

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from acceptance_support import criteria, private_dir
from brainstem_agent.broker import Broker
from brainstem_agent.organs import BindContext, ToolResult, ToolSpec
from brainstem_agent.policy import GrantAuthority, RunBinding
from brainstem_agent.state import Store
from brainstem_agent.worker import BRIDGE_FILE

AGENT_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


class BasicAgent:
    """Copy of Grail's agents/basic_agent.py contract (pinned 0.6.16)."""

    def __init__(self, name=None, metadata=None):
        if name is not None:
            self.name = name
        elif not hasattr(self, "name"):
            self.name = "BasicAgent"
        if metadata is not None:
            self.metadata = metadata
        elif not hasattr(self, "metadata"):
            self.metadata = {"name": self.name, "description": "Base agent -- override this.",
                             "parameters": {"type": "object", "properties": {}, "required": []}}

    def perform(self, **kwargs):
        return "Not implemented."

    def system_context(self):
        return None

    def to_tool(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.metadata.get("description", ""),
            "parameters": self.metadata.get("parameters", {"type": "object", "properties": {}})}}


class FakeRequest:
    def __init__(self, headers, body=None):
        self.headers = headers
        self._body = body or {}

    def get_json(self, force=False, silent=False):
        return self._body


class EchoOrgan:
    name = "echo"

    def tools(self):
        return [ToolSpec("echo", "Echo text.", {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "test.echo", "read")]

    def invoke(self, context, tool, arguments):
        return ToolResult("echo:" + arguments["text"])

    def context(self, bind):
        return "Echo organ ready."


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.store = Store(self.dir / "agent.sqlite3")
        self.addCleanup(self.store.close)
        self.authority = GrantAuthority(self.store, mode="local")
        workspace = private_dir(self)
        self.broker = Broker(
            organs=[EchoOrgan()],
            resolve_grant=lambda g, w, gen: self.authority.resolve(g, worker_id=w, generation=gen),
            bind_context=lambda b: BindContext(
                owner=b.owner, workspace=b.workspace, namespace="ns", session_id=b.session_id,
                turn_id=b.turn_id, workspace_root=workspace, user_input="hi",
                capabilities=b.capabilities),
            receipts=self.store, namespace=lambda b: "ns",
        )
        self.broker.start()
        self.addCleanup(self.broker.stop)
        self.key = self.broker.register_worker("w1", "g1")
        self.env = {
            "BRAINSTEM_AGENT_BROKER_URL": self.broker.url,
            "BRAINSTEM_AGENT_WORKER_ID": "w1",
            "BRAINSTEM_AGENT_WORKER_GENERATION": "g1",
            "BRAINSTEM_AGENT_WORKER_KEY": self.key,
        }

    def grant(self):
        return self.authority.issue(RunBinding(
            owner="local", workspace="ws", session_id="s", turn_id="turn_b", worker_id="w1",
            generation="g1", capabilities=("test.echo",)), ttl=60)

    def load(self, request=None):
        """Grail-equivalent per-request load; returns (agents, captured stdout)."""
        flask = types.ModuleType("flask")
        flask.has_request_context = lambda: request is not None
        flask.request = request
        agents_pkg = types.ModuleType("agents")
        agents_pkg.__path__ = []
        basic = types.ModuleType("agents.basic_agent")
        basic.BasicAgent = BasicAgent
        modules = {"flask": flask, "agents": agents_pkg, "agents.basic_agent": basic}
        output = io.StringIO()
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            name = f"agent_rapp_bridge_agent_py_{id(self)}_0"
            spec = importlib.util.spec_from_file_location(name, BRIDGE_FILE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded = {}
            for attribute in dir(module):
                candidate = getattr(module, attribute)
                if (isinstance(candidate, type) and candidate.__module__ == module.__name__
                        and hasattr(candidate, "perform")
                        and attribute not in ("BasicAgent", "object")
                        and not attribute.startswith("_")):
                    instance = candidate()
                    loaded[instance.name] = instance
        return loaded, output.getvalue()

    def perform(self, agent, /, **arguments):
        # Positional-only so a model argument named "self" reaches the bridge (test fix:
        # the original helper signature collided with it; the assertion is unchanged).
        with mock.patch.dict(os.environ, self.env):
            return agent.perform(**arguments)

    @criteria("A2")
    def test_bridge_is_a_data_file_named_like_an_agent(self):
        self.assertTrue(BRIDGE_FILE.is_file())
        self.assertTrue(BRIDGE_FILE.name.endswith("_agent.py"))
        package = Path(BRIDGE_FILE).resolve().parents[1]
        self.assertFalse((BRIDGE_FILE.parent / "__init__.py").exists())
        self.assertTrue(str(BRIDGE_FILE).startswith(str(package)))

    @criteria("A6")
    def test_bridge_imports_only_stdlib_flask_and_basic_agent(self):
        tree = ast.parse(BRIDGE_FILE.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = set(sys.stdlib_module_names) | {"flask", "agents", "basic_agent"}
        self.assertLessEqual(imported, allowed)

    @criteria("A6")
    def test_no_request_context_or_no_grant_defines_no_tools(self):
        self.assertEqual(self.load(None)[0], {})
        self.assertEqual(self.load(FakeRequest({}))[0], {})
        self.assertEqual(self.broker.bind_count("anything"), 0)

    @criteria("A2")
    def test_grant_header_binds_and_defines_granted_tools(self):
        grant = self.grant()
        agents, output = self.load(FakeRequest({"X-Brainstem-Agent-Grant": grant}))
        self.assertEqual(set(agents), {"echo"})
        self.assertEqual(self.broker.bind_count(grant), 1)
        contexts = [a.system_context() for a in agents.values() if a.system_context()]
        self.assertEqual(len(contexts), 1)
        self.assertIn("<brainstem_agent>", contexts[0])
        for name, agent in agents.items():
            self.assertRegex(name, AGENT_NAME)
            self.assertIsInstance(agent.metadata["description"], str)
            self.assertEqual(agent.metadata["parameters"]["type"], "object")
            self.assertEqual(agent.to_tool()["function"]["name"], name)
            json.dumps(agent.to_tool())
        self.assertNotIn(grant, output)
        self.assertNotIn(self.key, output)

    @criteria("A2")
    def test_perform_round_trips_through_the_broker_with_only_model_arguments(self):
        grant = self.grant()
        agents, _ = self.load(FakeRequest({"X-Brainstem-Agent-Grant": grant}))
        self.assertEqual(self.perform(agents["echo"], text="hi"), "echo:hi")
        [receipt] = self.store.list_receipts("ns")
        self.assertEqual((receipt["tool"], receipt["state"]), ("echo", "succeeded"))
        self.assertIsInstance(self.perform(agents["echo"], self="x", text="y"), str)

    @criteria("A6")
    def test_forged_grant_yields_only_an_honest_status_tool(self):
        agents, output = self.load(FakeRequest({"X-Brainstem-Agent-Grant": "forged-grant-value"}))
        self.assertEqual(set(agents), {"brainstem_agent_status"})
        status = agents["brainstem_agent_status"]
        self.assertIn("unavailable", status.system_context().lower())
        self.assertIn("unavailable", self.perform(status).lower())
        self.assertNotIn("forged-grant-value", output)

    @criteria("A8")
    def test_perform_never_raises_when_the_broker_is_gone(self):
        grant = self.grant()
        agents, _ = self.load(FakeRequest({"X-Brainstem-Agent-Grant": grant}))
        self.broker.stop()
        result = self.perform(agents["echo"], text="late")
        self.assertIsInstance(result, str)
        self.assertIn("failed", result.lower())


if __name__ == "__main__":
    unittest.main()
