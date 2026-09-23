"""A2/A6/A8 unit specs for the broker: the cytoplasm between Grail workers and organs."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from acceptance_support import criteria, private_dir
from brainstem_agent.broker import Broker
from brainstem_agent.organs import BindContext, OrganError, ToolResult, ToolSpec
from brainstem_agent.policy import GrantAuthority, RunBinding
from brainstem_agent.state import Store

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Clock:
    def __init__(self):
        self.now = 2_000_000.0

    def __call__(self):
        return self.now


class FakeOrgan:
    name = "fake"

    def __init__(self):
        self.calls = []
        self.started = threading.Event()

    def tools(self):
        schema = {"type": "object", "properties": {"text": {"type": "string", "maxLength": 50}},
                  "required": ["text"]}
        empty = {"type": "object", "properties": {}}
        return [
            ToolSpec("echo", "Echo text back.", schema, "test.echo", "read"),
            ToolSpec("slow", "Wait until cancelled.", empty, "test.slow", "external"),
            ToolSpec("boom", "Crash internally.", empty, "test.echo", "read"),
            ToolSpec("refuse", "Refuse politely.", empty, "test.echo", "read"),
        ]

    def invoke(self, context, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == "echo":
            return ToolResult(arguments["text"], evidence={"echoed": True})
        if tool == "slow":
            self.started.set()
            if context.cancelled.wait(10):
                raise OrganError("The run was cancelled.")
            return ToolResult("finished")
        if tool == "boom":
            raise RuntimeError("internal secret detail /Users/owner/.ssh")
        raise OrganError("nope")

    def context(self, bind):
        return "Fake organ context for " + bind.workspace


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.store = Store(self.dir / "agent.sqlite3")
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.authority = GrantAuthority(self.store, clock=self.clock, mode="local")
        self.organ = FakeOrgan()
        self.workspace = private_dir(self)
        self.broker = Broker(
            organs=[self.organ],
            resolve_grant=lambda grant, worker, generation: self.authority.resolve(
                grant, worker_id=worker, generation=generation),
            bind_context=self.bind_context,
            receipts=self.store,
            namespace=lambda binding: "ns-" + binding.workspace,
        )
        self.broker.start()
        self.addCleanup(self.broker.stop)
        self.key = self.broker.register_worker("w1", "g1")

    def bind_context(self, binding):
        return BindContext(
            owner=binding.owner, workspace=binding.workspace, namespace="ns-" + binding.workspace,
            session_id=binding.session_id, turn_id=binding.turn_id,
            workspace_root=self.workspace, user_input="hello", capabilities=binding.capabilities,
        )

    def grant(self, capabilities=("test.echo",), worker="w1", generation="g1", ttl=60):
        return self.authority.issue(RunBinding(
            owner="local", workspace="ws", session_id="s", turn_id="turn_1", worker_id=worker,
            generation=generation, capabilities=tuple(capabilities)), ttl=ttl)

    def post(self, path, body, *, worker="w1", generation="g1", key=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        request = urllib.request.Request(self.broker.url + path, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "X-Brainstem-Agent-Worker": worker,
            "X-Brainstem-Agent-Generation": generation,
            "Authorization": "Bearer " + (self.key if key is None else key),
        })
        try:
            with OPEN.open(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def bind(self, grant, **options):
        return self.post("/v1/bind", {"grant": grant}, **options)

    def invoke(self, grant, bind_id, tool, arguments, call_id="call-1", **options):
        return self.post("/v1/invoke", {"grant": grant, "bind_id": bind_id, "call_id": call_id,
                                        "tool": tool, "arguments": arguments}, **options)

    @criteria("A2", "A6")
    def test_bind_advertises_only_granted_tools_with_data_framing(self):
        self.assertTrue(self.broker.url.startswith("http://127.0.0.1:"))
        grant = self.grant(("test.echo",))
        status, body = self.bind(grant)
        self.assertEqual(status, 200, body)
        self.assertEqual({tool["name"] for tool in body["tools"]}, {"echo", "boom", "refuse"})
        self.assertNotIn("capability", json.dumps(body["tools"]))
        self.assertIn("<brainstem_agent>", body["context"])
        self.assertIn("not instructions", body["context"])
        self.assertIn("Fake organ context", body["context"])
        self.assertEqual(self.broker.bind_count(grant), 1)

    @criteria("A6")
    def test_worker_authentication_is_required(self):
        grant = self.grant()
        for options in ({"key": ""}, {"key": "wrong-key"}, {"generation": "g2"}, {"worker": "w2"}):
            with self.subTest(options=options):
                status, body = self.bind(grant, **options)
                self.assertEqual(status, 401, body)
        self.assertEqual(self.broker.bind_count(grant), 0)
        self.assertEqual(self.store.list_receipts("ns-ws"), [])

    @criteria("A6")
    def test_forged_expired_revoked_and_cross_worker_grants_are_refused(self):
        other_key = self.broker.register_worker("w2", "g2")
        cross = self.grant(worker="w1", generation="g1")
        self.assertEqual(self.bind(cross, worker="w2", generation="g2", key=other_key)[0], 403)
        self.assertEqual(self.bind("forged-" + "x" * 30)[0], 403)
        revoked = self.grant()
        self.authority.revoke(revoked)
        self.assertEqual(self.bind(revoked)[0], 403)
        expired = self.grant(ttl=5)
        self.clock.now += 6
        self.assertEqual(self.bind(expired)[0], 403)
        self.assertEqual(self.store.list_receipts("ns-ws"), [])

    @criteria("A6")
    def test_unregistered_generation_is_refused(self):
        grant = self.grant()
        self.broker.unregister_worker("w1")
        self.assertEqual(self.bind(grant)[0], 401)

    @criteria("A2")
    def test_invoke_runs_the_organ_and_writes_a_durable_receipt(self):
        grant = self.grant()
        _, bound = self.bind(grant)
        status, body = self.invoke(grant, bound["bind_id"], "echo", {"text": "hi", "extra": 1})
        self.assertEqual((status, body), (200, {"ok": True, "content": "hi"}))
        self.assertEqual(self.organ.calls, [("echo", {"text": "hi"})])
        [receipt] = self.store.list_receipts("ns-ws", turn_id="turn_1")
        self.assertEqual((receipt["tool"], receipt["state"]), ("echo", "succeeded"))
        self.assertEqual(receipt["request"], {"text": "hi"})
        self.assertEqual(receipt["result"]["content_chars"], 2)
        self.assertIn("content_sha256", receipt["result"])
        self.assertNotIn(grant, json.dumps(receipt))

    @criteria("A6")
    def test_invoke_requires_a_matching_bind_and_an_advertised_tool(self):
        grant = self.grant(("test.echo",))
        _, bound = self.bind(grant)
        self.assertEqual(self.invoke(grant, "unknown-bind", "echo", {"text": "x"})[0], 404)
        other = self.grant(("test.echo",))
        self.assertEqual(self.invoke(other, bound["bind_id"], "echo", {"text": "x"})[0], 404)
        status, _ = self.invoke(grant, bound["bind_id"], "slow", {}, call_id="call-2")
        self.assertIn(status, (403, 404))
        self.assertEqual([call[0] for call in self.organ.calls], [])
        states = [r["state"] for r in self.store.list_receipts("ns-ws")]
        self.assertEqual(states, ["denied"])

    @criteria("A8")
    def test_organ_errors_are_honest_and_internal_errors_are_generic(self):
        grant = self.grant()
        _, bound = self.bind(grant)
        self.assertEqual(self.invoke(grant, bound["bind_id"], "refuse", {}, call_id="c1")[1],
                         {"ok": False, "content": "nope"})
        _, body = self.invoke(grant, bound["bind_id"], "boom", {}, call_id="c2")
        self.assertFalse(body["ok"])
        self.assertNotIn("secret detail", body["content"])
        self.assertNotIn(".ssh", body["content"])
        _, body = self.invoke(grant, bound["bind_id"], "echo", {"text": 5}, call_id="c3")
        self.assertFalse(body["ok"])
        self.assertEqual(
            sorted(r["state"] for r in self.store.list_receipts("ns-ws")), ["failed"] * 3)

    @criteria("A8")
    def test_cancel_grant_stops_inflight_calls_and_refuses_later_ones(self):
        grant = self.grant(("test.echo", "test.slow"))
        _, bound = self.bind(grant)
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(
            result=self.invoke(grant, bound["bind_id"], "slow", {}, call_id="slow-1")))
        thread.start()
        self.assertTrue(self.organ.started.wait(5))
        started = time.monotonic()
        self.broker.cancel_grant(grant)
        thread.join(5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(outcome["result"][1]["ok"], False)
        self.assertEqual(self.bind(grant)[0], 403)
        self.assertEqual(self.invoke(grant, bound["bind_id"], "echo", {"text": "x"},
                                     call_id="late")[0], 403)

    @criteria("A6")
    def test_malformed_and_oversized_requests(self):
        self.assertEqual(self.post("/v1/bind", None, raw=b"not json")[0], 400)
        self.assertEqual(self.post("/v1/bind", {"grant": 5})[0], 400)
        self.assertEqual(self.post("/v1/unknown", {})[0], 404)
        self.assertEqual(self.post("/v1/bind", None, raw=b"{" + b" " * (1 << 21) + b"}")[0], 413)


if __name__ == "__main__":
    unittest.main()
