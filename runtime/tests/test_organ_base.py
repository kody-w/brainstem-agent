import threading
import time
import unittest
from pathlib import Path

from brainstem_agent.organs import (
    InvocationContext,
    OrganError,
    ToolResult,
    ToolSpec,
    clip,
    validate_arguments,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 20},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "flag": {"type": "boolean"},
    },
    "required": ["path"],
}


def spec(**changes):
    values = {
        "name": "read_file",
        "description": "Read a file.",
        "parameters": SCHEMA,
        "capability": "files.read",
        "effect": "read",
    }
    values.update(changes)
    return ToolSpec(**values)


class ToolSpecTests(unittest.TestCase):
    def test_valid_spec_wire_shape(self):
        wire = spec().to_wire()
        self.assertEqual(
            set(wire), {"name", "description", "parameters", "timeout_seconds"}
        )
        wire["parameters"]["properties"].clear()
        self.assertIn("path", spec().parameters["properties"])

    def test_spec_copies_parameters(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        tool = spec(parameters=schema)
        schema["properties"]["b"] = {"type": "string"}
        self.assertNotIn("b", tool.parameters["properties"])

    def test_invalid_specs_refuse(self):
        cases = [
            {"name": "Read File"},
            {"name": "x" * 49},
            {"description": " "},
            {"capability": "Files"},
            {"effect": "delete"},
            {"timeout_seconds": 0},
            {"timeout_seconds": float("inf")},
            {"parameters": {"type": "string"}},
            {"parameters": {"type": "object", "properties": {"a": {"type": "array"}}}},
            {"parameters": {"type": "object", "properties": {"a": {"type": "string", "format": "uri"}}}},
            {"parameters": {"type": "object", "properties": {}, "required": ["missing"]}},
            {"parameters": {"type": "object", "additionalProperties": {"type": "string"}}},
            {"parameters": {"type": "object", "properties": {"a": {"type": "string", "items": {"type": "string"}}}}},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                spec(**change)


class ArgumentTests(unittest.TestCase):
    def test_valid_arguments_drop_unknown_keys(self):
        self.assertEqual(
            validate_arguments(SCHEMA, {"path": "a.txt", "count": 2.0, "extra": 1}),
            {"path": "a.txt", "count": 2},
        )

    def test_invalid_arguments_refuse(self):
        cases = [
            None,
            [],
            {},
            {"path": ""},
            {"path": 3},
            {"path": "x" * 21},
            {"path": "a", "count": True},
            {"path": "a", "count": 9},
            {"path": "a", "count": 1.5},
            {"path": "a", "mode": "c"},
            {"path": "a", "tags": ["x", "y", "z"]},
            {"path": "a", "tags": [1]},
            {"path": "a", "flag": "yes"},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(OrganError):
                validate_arguments(SCHEMA, arguments)

    def test_additional_properties_true_keeps_values(self):
        schema = {"type": "object", "additionalProperties": True}
        self.assertEqual(validate_arguments(schema, {"a": [1]}), {"a": [1]})


class ContextTests(unittest.TestCase):
    def context(self, deadline):
        return InvocationContext(
            owner="local", workspace="w", namespace="ns:x", session_id="s",
            turn_id="t", call_id="c", workspace_root=Path("/tmp"),
            capabilities=("files.read",), deadline=deadline,
        )

    def test_cancellation_and_deadline(self):
        live = self.context(time.monotonic() + 30)
        live.check()
        self.assertGreater(live.remaining(), 0)
        live.cancelled.set()
        with self.assertRaises(OrganError):
            live.check()
        with self.assertRaises(OrganError):
            self.context(time.monotonic() - 1).check()

    def test_contexts_have_distinct_events(self):
        first = self.context(time.monotonic() + 1)
        second = self.context(time.monotonic() + 1)
        self.assertIsInstance(first.cancelled, threading.Event)
        self.assertIsNot(first.cancelled, second.cancelled)


class ResultAndClipTests(unittest.TestCase):
    def test_result_types(self):
        self.assertTrue(ToolResult("ok").ok)
        with self.assertRaises(TypeError):
            ToolResult(1)
        with self.assertRaises(TypeError):
            ToolResult("x", ok="yes")

    def test_clip_keeps_head_and_tail(self):
        text = "a" * 500 + "b" * 500
        clipped = clip(text, 200)
        self.assertLessEqual(len(clipped), 200)
        self.assertTrue(clipped.startswith("a"))
        self.assertTrue(clipped.endswith("b"))
        self.assertIn("characters omitted", clipped)
        self.assertEqual(clip("short", 200), "short")


if __name__ == "__main__":
    unittest.main()
