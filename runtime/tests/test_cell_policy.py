"""A6 unit specs for local grant authority: forged, expired, revoked, cross-worker."""

import unittest

from acceptance_support import criteria, private_dir
from brainstem_agent.policy import GrantAuthority, GrantDenied, RunBinding
from brainstem_agent.state import Store


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class LocalAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.path = self.dir / "agent.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.authority = GrantAuthority(self.store, clock=self.clock, mode="local")
        self.binding = RunBinding(
            owner="local", workspace="/w", session_id="s", turn_id="t",
            worker_id="w1", generation="g1", capabilities=("files.read", "files.write"),
        )

    @criteria("A6")
    def test_issue_and_resolve_for_the_bound_worker_generation(self):
        token = self.authority.issue(self.binding, ttl=60)
        self.assertEqual(self.authority.resolve(token, worker_id="w1", generation="g1"), self.binding)

    @criteria("A6", "A11")
    def test_handles_are_stored_only_as_digests(self):
        token = self.authority.issue(self.binding, ttl=60)
        self.store.close()
        self.assertNotIn(token.encode(), self.path.read_bytes())

    @criteria("A6")
    def test_forged_handles_are_refused(self):
        self.authority.issue(self.binding, ttl=60)
        for forged in ("forged", "x" * 43, "", "../../grant"):
            with self.subTest(forged=forged), self.assertRaises(GrantDenied):
                self.authority.resolve(forged, worker_id="w1", generation="g1")

    @criteria("A6")
    def test_expired_grants_are_refused(self):
        token = self.authority.issue(self.binding, ttl=60)
        self.clock.now += 61
        with self.assertRaises(GrantDenied):
            self.authority.resolve(token, worker_id="w1", generation="g1")

    @criteria("A6")
    def test_revoked_grants_are_refused_durably(self):
        token = self.authority.issue(self.binding, ttl=60)
        self.authority.revoke(token)
        with self.assertRaises(GrantDenied):
            self.authority.resolve(token, worker_id="w1", generation="g1")
        self.store.close()
        with Store(self.path) as reopened:
            with self.assertRaises(GrantDenied):
                GrantAuthority(reopened, clock=self.clock, mode="local").resolve(
                    token, worker_id="w1", generation="g1")

    @criteria("A6")
    def test_other_worker_or_generation_is_refused(self):
        token = self.authority.issue(self.binding, ttl=60)
        for worker_id, generation in (("w2", "g1"), ("w1", "g2"), ("w2", "g2")):
            with self.subTest(worker=worker_id, generation=generation):
                with self.assertRaises(GrantDenied):
                    self.authority.resolve(token, worker_id=worker_id, generation=generation)

    @criteria("A6")
    def test_ttl_is_bounded(self):
        for ttl in (0, -1, 3601, float("inf")):
            with self.subTest(ttl=ttl), self.assertRaises(GrantDenied):
                self.authority.issue(self.binding, ttl=ttl)

    @criteria("A6")
    def test_synthetic_grants_cannot_be_resolved_as_local(self):
        synthetic = GrantAuthority(self.store, clock=self.clock)
        token = synthetic.issue(self.binding, ttl=60)
        with self.assertRaises(GrantDenied):
            self.authority.resolve(token, worker_id="w1", generation="g1")


if __name__ == "__main__":
    unittest.main()
