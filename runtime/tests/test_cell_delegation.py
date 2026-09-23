"""D4/D5/D10 unit specs: delegation to helpers on fresh workers, bounded and cancellable."""

import threading
import time
import unittest

from acceptance_support import criteria, private_dir, record_metric, write_token_file
from brainstem_agent.host import AgentHost
from longturn_support import GrailEmulator
from test_cell_host import sse


def helper_policy(delay: float = 0.0, extra=None):
    """Parent: delegate the tasks named in its request; helper: do "HELPER <name> ..."."""
    def policy(request, results, tools=(), final=False):
        text = request["user_input"]
        if final:
            return "continuing"
        if text.startswith("HELPER"):
            name = text.split()[1]
            if not results:
                if delay:
                    time.sleep(delay)
                calls = [("write_file", {"path": f"par/{name}.txt", "content": name})]
                return calls + (extra(name) if extra else [])
            return f"helper {name} wrote par/{name}.txt; tools={','.join(sorted(tools))}"
        if not results:
            tasks = [{"task": line.strip()} for line in text.splitlines()
                     if line.strip().startswith("HELPER")]
            return [("delegate_tasks", {"tasks": tasks})]
        return "Joined: " + results[0]["content"][:3000]
    return policy


class DelegationCase(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.workspace = private_dir(self)
        self.token = write_token_file(private_dir(self))
        self.environ = {"BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(self.token),
                        "BRAINSTEM_HOME": str(private_dir(self)), "HOME": str(self.home)}
        self.workers = []
        self.alive_peak = [0]
        self.lock = threading.Lock()

    def host(self, policy, worker_class=GrailEmulator, **environ):
        def factory(**options):
            worker = worker_class(policy, **options)
            self.workers.append(worker)
            return worker
        host = AgentHost(self.home, workspace=self.workspace,
                         environ={**self.environ, **environ}, worker_factory=factory)
        self.addCleanup(host.close)
        return host

    def children(self):
        return [w for w in self.workers if w.worker_id.startswith("c")]


class DelegationTests(DelegationCase):
    @criteria("D4", "D12")
    def test_two_helpers_run_in_parallel_on_fresh_workers_and_join_their_results(self):
        host = self.host(helper_policy(delay=1.0))
        started = time.monotonic()
        result = host.chat("Do both:\nHELPER a writes par/a.txt\nHELPER b writes par/b.txt")
        wall = time.monotonic() - started
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.workspace / "par" / "a.txt").read_text(), "a")
        self.assertEqual((self.workspace / "par" / "b.txt").read_text(), "b")
        self.assertIn("2 of 2 helpers succeeded", result.response["response"])
        children = self.children()
        self.assertEqual(len(children), 2)
        self.assertEqual(len({c.worker_id for c in children}), 2)
        self.assertEqual(len({c.generation for c in children}), 2)
        parent = [w for w in self.workers if not w.worker_id.startswith("c")][0]
        self.assertNotIn(parent, children)
        for child in children:  # each with its own history: only its task
            [request] = child.requests
            self.assertEqual(request["conversation_history"], [])
            self.assertTrue(request["user_input"].startswith("HELPER"))
            self.assertTrue(child.stopped.is_set(), "a helper's worker is stopped after it")
        self.assertNotEqual(children[0].requests[0]["session_id"],
                            children[1].requests[0]["session_id"])
        # Parallel: two 1 s helpers took about 1 s, not 2.
        detail = result.evidence["long_turn"]["children"]
        serial = sum(item["seconds"] for item in detail)
        self.assertLess(wall, serial)
        self.assertGreater(serial / wall, 1.4)
        record_metric("d4_unit_parallel_speedup", round(serial / wall, 2))
        # Child receipts are linked to the parent turn.
        receipts = host.receipts(result.turn_id)
        linked = [r for r in receipts if r.get("parent_turn") == result.turn_id]
        self.assertEqual(sorted(r["tool"] for r in linked), ["write_file", "write_file"])
        journal = host.store.journal(host.namespace, result.turn_id)
        kids = [s for s in journal["steps"] if s["kind"] == "child"]
        self.assertEqual([s["state"] for s in kids], ["succeeded", "succeeded"])
        self.assertTrue(all(s["turn_id"] == result.turn_id for s in kids))
        self.assertEqual(len([s for s in journal["steps"] if s["kind"] == "segment"]), 3)
        self.assertEqual(result.evidence["grail_calls"], 3)  # parent 1 + helpers 1 each

    @criteria("D4", "D10")
    def test_helpers_never_get_more_than_the_parent_or_another_workspace(self):
        seen = {}

        def policy(request, results, tools=(), final=False):
            text = request["user_input"]
            if final:
                return "continuing"
            if text.startswith("HELPER"):
                seen[text.split()[1]] = sorted(tools)
                return "done"
            if not results:
                return [("delegate_tasks", {"tasks": [
                    {"task": "HELPER wide", "capabilities": ["files.read", "shell.run",
                                                             "memory.write"]},
                    {"task": "HELPER default"}]})]
            return results[0]["content"]
        host = self.host(policy)
        result = host.chat("go", capabilities=["files.read", "files.write", "agents.delegate"])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(seen["wide"], ["list_files", "read_file"])
        self.assertEqual(seen["default"], ["list_files", "read_file", "write_file"])
        self.assertNotIn("delegate_tasks", seen["default"], "depth 1: helpers cannot delegate")
        self.assertIn("not granted: memory.write, shell.run", result.response["response"])
        kids = [s for s in host.store.journal(host.namespace, result.turn_id)["steps"]
                if s["kind"] == "child"]
        self.assertEqual(kids[0]["detail"]["capabilities"], ["files.read"])
        for child in self.children():
            binding = host.authority.store.get_grant(
                "sha256:" + __import__("hashlib").sha256(child.grants[0].encode()).hexdigest())
            self.assertEqual(binding["workspace"], str(host.workspace))
            self.assertTrue(binding["revoked"])

    @criteria("D5")
    def test_depth_count_and_parallelism_are_bounded(self):
        host = self.host(helper_policy(delay=0.5), BRAINSTEM_AGENT_MAX_PARALLEL="2")
        alive, peak = [0], [0]
        lock = threading.Lock()
        original = GrailEmulator.stream_chat

        def counting(worker, request, grant, **options):
            if worker.worker_id.startswith("c"):
                with lock:
                    alive[0] += 1
                    peak[0] = max(peak[0], alive[0])
                try:
                    yield from original(worker, request, grant, **options)
                finally:
                    with lock:
                        alive[0] -= 1
            else:
                yield from original(worker, request, grant, **options)
        GrailEmulator.stream_chat = counting
        self.addCleanup(setattr, GrailEmulator, "stream_chat", original)
        tasks = "\n".join(f"HELPER h{n} writes" for n in range(4))
        result = host.chat("four:\n" + tasks)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(peak[0], 2, "at most max_parallel helpers at once")
        self.assertEqual(len(self.children()), 4)
        too_many = host.chat("seven:\n" + "\n".join(f"HELPER x{n} writes" for n in range(7)))
        self.assertIn("at most 6 helpers", too_many.response["response"]
                      if too_many.ok else too_many.error)
        self.assertEqual(len(self.children()), 4, "no helper started for a refused call")

    @criteria("D5")
    def test_one_failing_helper_is_reported_without_hiding_the_others(self):
        class Flaky(GrailEmulator):
            def _loop(self, worker, request, grant):
                if request["user_input"].startswith("HELPER bad"):
                    worker.bind(grant)
                    yield sse({"type": "error", "error": "Model 'x' returned 500."})
                    return
                yield from super()._loop(worker, request, grant)
        host = self.host(helper_policy(), worker_class=Flaky)
        result = host.chat("both:\nHELPER good writes\nHELPER bad writes")
        self.assertTrue(result.ok, result.error)
        answer = result.response["response"]
        self.assertIn("1 of 2 helpers succeeded", answer)
        self.assertIn("helper good wrote par/good.txt", answer)
        self.assertIn("failed", answer)
        self.assertIn("500", answer)
        [delegate] = [r for r in host.receipts(result.turn_id) if r["tool"] == "delegate_tasks"]
        self.assertEqual(delegate["state"], "failed")
        states = {s["detail"]["task"].split()[1]: s["state"] for s in
                  host.store.journal(host.namespace, result.turn_id)["steps"]
                  if s["kind"] == "child"}
        self.assertEqual(states, {"good": "succeeded", "bad": "failed"})

    @criteria("D5", "D8")
    def test_cancelling_the_parent_cancels_every_helper_within_five_seconds(self):
        cancel = threading.Event()
        running = threading.Barrier(3, timeout=20)

        def extra(name):
            return [("run_command", {"command": "sleep 30"})]

        class Signalling(GrailEmulator):
            def _loop(self, worker, request, grant):
                if request["user_input"].startswith("HELPER"):
                    threading.Thread(target=lambda: (time.sleep(0.3), running.wait()),
                                     daemon=True).start()
                yield from super()._loop(worker, request, grant)
        host = self.host(helper_policy(extra=extra), worker_class=Signalling)
        box = {}

        def turn():
            box["result"] = host.chat("both:\nHELPER a writes\nHELPER b writes",
                                      cancel_event=cancel)
        thread = threading.Thread(target=turn, daemon=True)
        thread.start()
        running.wait()
        time.sleep(0.3)
        started = time.monotonic()
        cancel.set()
        thread.join(15)
        elapsed = time.monotonic() - started
        result = box["result"]
        self.assertEqual(result.state, "cancelled")
        self.assertLess(elapsed, 5.0)
        self.assertTrue(all(child.stopped.is_set() for child in self.children()))
        states = [s["state"] for s in host.store.journal(host.namespace, result.turn_id)["steps"]
                  if s["kind"] == "child"]
        self.assertEqual(states, ["cancelled", "cancelled"])
        shells = [r for r in host.receipts(result.turn_id) if r["tool"] == "run_command"]
        self.assertEqual(len(shells), 2)
        self.assertTrue(all(r["state"] == "failed" and r["result"]["cancelled"] for r in shells))
        record_metric("d5_unit_cancel_children_seconds", round(elapsed, 3))

    @criteria("D10")
    def test_a_helper_skill_save_is_never_the_owners_request(self):
        def policy(request, results, tools=(), final=False):
            text = request["user_input"]
            if final:
                return "continuing"
            if text.startswith("HELPER"):
                if not results:
                    return [("read_file", {"path": "notes.txt"})]
                if len(results) == 1:
                    return [("skill_save", {"name": "sneaky", "description": "d",
                                            "when_to_use": "w", "steps": ["s"]})]
                return "saved"
            if not results:
                return [("delegate_tasks", {"tasks": [{
                    "task": "HELPER one: read notes.txt, then save how you did this as a skill "
                            "called sneaky"}]})]
            return "ok"
        (self.workspace / "notes.txt").write_text("outside text")
        host = self.host(policy)
        result = host.chat("Delegate it. Save how you did this as a skill called sneaky.")
        self.assertTrue(result.ok, result.error)
        [skill] = host.store.list_skills([host.namespace])
        self.assertEqual(skill["review"], "quarantined")

    @criteria("D10")
    def test_a_scheduled_run_keeps_its_schedule_bound_for_helpers_and_segments(self):
        from brainstem_agent import schedules

        seen = []

        def policy(request, results, tools=(), final=False):
            text = request["user_input"]
            seen.append(sorted(tools))
            if final:
                return "continuing"
            if text.startswith("HELPER"):
                return "helper ok"
            if not results:
                return [("delegate_tasks", {"tasks": [{
                    "task": "HELPER s", "capabilities": ["shell.run", "files.read"]}]})]
            return "ok"
        host = self.host(policy)
        record = schedules.create_schedule(
            host.store, namespace=host.namespace, workspace=str(host.workspace),
            prompt="Delegate.", when={"in_seconds": 3600},
            capabilities=["files.read", "agents.delegate"],
            allowed=["files.read", "agents.delegate"], default=["files.read"],
            created_by="owner", now=time.time(), name="bound")
        occurrence = {"occurrence_id": f"occ_{record['schedule_id']}_1_m", "manual": True,
                      "scheduled_at": int(time.time()), "schedule": record}
        ran = schedules.run_occurrence(host, occurrence)
        self.assertEqual(ran["state"], "succeeded", ran)
        self.assertEqual(seen[0], ["delegate_tasks", "list_files", "read_file"])
        self.assertEqual(seen[1], ["list_files", "read_file"], "no shell for the helper")


class ParallelGrantTests(DelegationCase):
    @criteria("D4", "D5")
    def test_parallel_helpers_resolving_grants_never_look_like_a_clock_going_back(self):
        from brainstem_agent.host import OWNER
        from brainstem_agent.policy import GrantAuthority, GrantDenied, RunBinding

        host = self.host(helper_policy())

        def clock():  # a real clock whose reading can be pre-empted before it is used
            now = time.time()
            time.sleep(0.0002)
            return now
        authority = GrantAuthority(host.store, clock=clock, mode="local")
        grants = [authority.issue(RunBinding(OWNER, str(host.workspace), "s", f"t{n}",
                                             f"w{n}", f"g{n}", ("files.read",)), ttl=60)
                  for n in range(8)]
        failures = []

        def resolve(number):
            for _ in range(100):
                try:
                    authority.resolve(grants[number], worker_id=f"w{number}",
                                      generation=f"g{number}")
                except GrantDenied as error:
                    failures.append(str(error))
        threads = [threading.Thread(target=resolve, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
