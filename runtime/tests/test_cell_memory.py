"""A3/A6 unit specs for cell memory: durable, scoped facts that Grail sees as data."""

import time
import unittest

from acceptance_support import criteria, private_dir
from brainstem_agent.organs import BindContext, InvocationContext, OrganError, validate_arguments
from brainstem_agent.organs.memory import MemoryOrgan
from brainstem_agent.state import StateError, Store


def invocation(namespace, root, **changes):
    values = dict(
        owner="local", workspace="ws", namespace=namespace, session_id="s", turn_id="t1",
        call_id="c", workspace_root=root, capabilities=("memory.read", "memory.write"),
        deadline=time.monotonic() + 30,
    )
    values.update(changes)
    return InvocationContext(**values)


def binding(namespace, user_input, root):
    return BindContext(
        owner="local", workspace="ws", namespace=namespace, session_id="s", turn_id="t2",
        workspace_root=root, user_input=user_input,
        capabilities=("memory.read", "memory.write"),
    )


class MemoryOrganTests(unittest.TestCase):
    def setUp(self):
        directory = private_dir(self)
        self.root = private_dir(self)
        self.store = Store(directory / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.organ = MemoryOrgan(self.store)
        self.specs = {spec.name: spec for spec in self.organ.tools()}

    def call(self, namespace, tool, arguments):
        arguments = validate_arguments(self.specs[tool].parameters, arguments)
        return self.organ.invoke(invocation(namespace, self.root), tool, arguments)

    @criteria("A3")
    def test_tools_and_capabilities(self):
        self.assertEqual(set(self.specs), {"remember", "recall", "forget"})
        self.assertEqual(self.specs["remember"].capability, "memory.write")
        self.assertEqual(self.specs["forget"].capability, "memory.write")
        self.assertEqual(self.specs["recall"].capability, "memory.read")
        self.assertLessEqual(self.specs["remember"].parameters["properties"]["text"]["maxLength"], 2000)

    @criteria("A3")
    def test_remember_is_durable_and_recall_finds_it(self):
        result = self.call("ns-a", "remember", {"text": "The user's favorite color is teal."})
        self.assertTrue(result.ok)
        facts = self.store.list_facts("ns-a")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["source_turn"], "t1")
        recalled = self.call("ns-a", "recall", {"query": "What is my favorite color?"})
        self.assertIn("teal", recalled.content)

    @criteria("A3")
    def test_bind_context_offers_relevant_facts_as_bounded_data(self):
        self.call("ns-a", "remember", {"text": "The user's favorite color is teal."})
        text = self.organ.context(binding("ns-a", "What is my favorite color? Answer with one word.", self.root))
        self.assertIn("<memory", text)
        self.assertIn("teal", text)
        self.assertLessEqual(len(text), 8000)

    @criteria("A6")
    def test_namespaces_do_not_share_memory(self):
        self.call("ns-a", "remember", {"text": "The user's favorite color is teal."})
        self.assertEqual(self.store.list_facts("ns-b"), [])
        self.assertNotIn("teal", self.call("ns-b", "recall", {"query": "favorite color"}).content)
        context = self.organ.context(binding("ns-b", "What is my favorite color?", self.root)) or ""
        self.assertNotIn("teal", context)

    @criteria("A3")
    def test_forget_removes_the_fact(self):
        self.call("ns-a", "remember", {"text": "Temporary fact about kiwi."})
        fact_id = self.store.list_facts("ns-a")[0]["fact_id"]
        self.assertTrue(self.call("ns-a", "forget", {"fact_id": fact_id}).ok)
        self.assertEqual(self.store.list_facts("ns-a"), [])
        with self.assertRaises(OrganError):
            self.call("ns-b", "forget", {"fact_id": fact_id})

    @criteria("A3")
    def test_fact_text_is_bounded(self):
        with self.assertRaises(OrganError):
            self.call("ns-a", "remember", {"text": "x" * 2001})
        with self.assertRaises(StateError):
            self.store.add_fact("ns-a", "x" * 2001)


if __name__ == "__main__":
    unittest.main()
