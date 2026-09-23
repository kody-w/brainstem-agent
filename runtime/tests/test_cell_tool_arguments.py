"""Tool arguments that unchanged Grail accepts.

Grail streams a tool call's argument string together from the provider's
fragments, starting from ``""``, then ``run_tool_calls`` does ``json.loads`` and
refuses anything that is not a JSON object ("Tool arguments must be a valid JSON
object.") without calling ``perform()``. Models stream no argument fragments at
all for a tool whose schema requires nothing (seen live for ``schedule_list``), so
such a call never reaches the cell. Every tool the cell advertises therefore
requires an argument, with a documented neutral value; owner calls that omit it
get the same default, and a JSON ``null`` for an optional argument means "not
given" (models send those too).
"""

import json
import unittest

from acceptance_support import criteria, private_dir, write_token_file
from brainstem_agent.host import DEFAULT_CAPABILITIES, AgentHost
from brainstem_agent.organs import OrganError, ToolSpec, validate_arguments
import test_cell_bridge
from test_cell_bridge import FakeRequest
from test_cell_host import FakeWorker, done

SCHEMA = {"type": "object", "properties": {
    "path": {"type": "string", "minLength": 1, "default": "."},
    "count": {"type": "integer", "minimum": 1},
    "name": {"type": "string"}}, "required": ["path"]}


def grail_arguments(fragments):
    """Grail 0.6.16's handling of one streamed call: accumulate, then json.loads."""
    arguments = ""
    for fragment in fragments:
        if fragment:
            arguments += fragment
    try:
        parsed = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    return parsed if isinstance(parsed, dict) else "Error: Tool arguments must be a valid JSON object."


def minimal_call(spec):
    """The smallest well-formed call a model makes: only the required arguments."""
    properties = spec.parameters.get("properties", {})
    sample = {"string": "all", "integer": 60, "boolean": False, "array": [], "number": 1}
    return {name: properties[name].get("default", sample[properties[name]["type"]])
            for name in spec.parameters.get("required", [])}


class HostCase(unittest.TestCase):
    def setUp(self):
        self.workspace = private_dir(self)
        environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(write_token_file(private_dir(self))),
                   "BRAINSTEM_HOME": str(private_dir(self))}
        self.calls = []

        def script(worker, request, grant):
            bound = worker.bind(grant)
            for tool, arguments in self.calls:
                status, body = worker.invoke(grant, bound, tool, arguments)
                self.results.append((tool, status, body))
            yield done(request, "ok")

        self.results = []
        self.host = AgentHost(private_dir(self), workspace=self.workspace, environ=environ,
                              worker_factory=lambda **options: FakeWorker(script, **options))
        self.addCleanup(self.host.close)


class AdvertisedToolTests(HostCase):
    @criteria("A2", "B4")
    def test_every_tool_the_cell_advertises_requires_an_argument(self):
        specs = self.host.broker.tool_specs(DEFAULT_CAPABILITIES)
        names = {spec.name for spec in specs}
        self.assertLessEqual({"list_files", "schedule_list", "schedule_create", "run_command"},
                             names)
        for spec in specs:
            with self.subTest(tool=spec.name):
                wire = spec.to_wire()["parameters"]
                self.assertTrue(wire.get("required"), f"{spec.name} requires nothing, so a model "
                                "streams it empty arguments that Grail refuses")
                self.assertLessEqual(set(wire["required"]), set(wire["properties"]))
                # What the model sends for it is a JSON object Grail passes to perform().
                self.assertIsInstance(grail_arguments([json.dumps(minimal_call(spec))]), dict)
        # The failure mode itself: a call streamed with no argument fragments.
        self.assertIn("valid JSON object", grail_arguments([]))
        self.assertIn("valid JSON object", grail_arguments(["", None]))

    @criteria("A2", "B4")
    def test_the_minimal_calls_do_what_the_argument_free_calls_did(self):
        (self.workspace / "notes").mkdir()
        (self.workspace / "notes" / "a.txt").write_text("a")
        self.host.invoke_tool("schedule_create", {"prompt": "later", "in_seconds": 600,
                                                  "name": "later"})
        self.calls = [("list_files", {"path": "."}), ("schedule_list", {"schedule_id": "all"}),
                      ("schedule_list", {"schedule_id": "later"})]
        result = self.host.chat("go")
        self.assertTrue(result.ok, result.error)
        (_, s1, listed), (_, s2, schedules), (_, s3, shown) = self.results
        self.assertEqual((s1, listed["ok"]), (200, True), listed)
        self.assertIn("notes/", listed["content"])
        self.assertEqual((s2, schedules["ok"]), (200, True), schedules)
        self.assertIn("'later'", schedules["content"])
        self.assertTrue(shown["ok"], shown)
        self.assertIn("Prompt: later", shown["content"])
        self.assertEqual([r["state"] for r in self.host.receipts(turn_id=result.turn_id)],
                         ["succeeded"] * 3)

    @criteria("A2", "B4")
    def test_owner_calls_that_omit_the_argument_get_its_default(self):
        (self.workspace / "hello.txt").write_text("hi")
        listed = self.host.invoke_tool("list_files", {})
        self.assertTrue(listed["ok"], listed)
        self.assertIn("hello.txt", listed["content"])
        self.assertTrue(self.host.invoke_tool("schedule_list", {})["ok"])


class NullArgumentTests(HostCase):
    @criteria("A2")
    def test_null_for_an_optional_argument_means_not_given_and_defaults_fill_in(self):
        self.assertEqual(validate_arguments(SCHEMA, {"path": "a", "count": None, "name": None}),
                         {"path": "a"})
        self.assertEqual(validate_arguments(SCHEMA, {}), {"path": "."})
        self.assertEqual(validate_arguments(SCHEMA, {"count": None}), {"path": "."})
        strict = {**SCHEMA, "properties": {**SCHEMA["properties"], "path": {"type": "string"}}}
        for refused in ({"path": None}, {}, {"path": "a", "count": "x"}):
            with self.subTest(arguments=refused), self.assertRaises(OrganError):
                validate_arguments(strict, refused)

    @criteria("B4")
    def test_a_schedule_created_with_nulls_for_unused_timings_is_accepted(self):
        self.calls = [("schedule_create", {"prompt": "later", "in_seconds": 120, "at": None,
                                           "cron": None, "every_seconds": None, "timezone": None,
                                           "capabilities": None, "missed_policy": None})]
        self.assertTrue(self.host.chat("go").ok)
        [(_, status, body)] = self.results
        self.assertEqual((status, body["ok"]), (200, True), body)
        [schedule] = self.host.store.list_schedules(None)
        self.assertEqual(schedule["spec"]["kind"], "once")

    def test_a_default_that_breaks_its_own_schema_is_refused_when_the_tool_is_declared(self):
        with self.assertRaises(ValueError):
            ToolSpec("bad", "Bad default.", {"type": "object", "properties": {
                "count": {"type": "integer", "minimum": 1, "default": 0}},
                "required": ["count"]}, "test.bad", "read")


class BridgeStatusToolTests(unittest.TestCase):
    @criteria("A6")
    def test_the_honest_status_tool_requires_an_argument_too(self):
        bridge = test_cell_bridge.BridgeTests("test_bridge_is_a_data_file_named_like_an_agent")
        bridge.setUp()
        self.addCleanup(bridge.doCleanups)
        agents, _ = bridge.load(FakeRequest({"X-Brainstem-Agent-Grant": "forged-grant"}))
        status = agents["brainstem_agent_status"]
        parameters = status.to_tool()["function"]["parameters"]
        self.assertTrue(parameters.get("required"), parameters)
        self.assertLessEqual(set(parameters["required"]), set(parameters["properties"]))
        self.assertIn("unavailable", bridge.perform(status, intent="list files").lower())


if __name__ == "__main__":
    unittest.main()
