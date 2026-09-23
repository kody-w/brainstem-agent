"""Live D1, D2, D4 and D8 specs: long turns with real Copilot inference through the cell.

Skips unless BRAINSTEM_AGENT_LIVE=1. About 12 Grail requests (each segment and each
helper's segment is one): D1 about 4 (the chain, continued across segments), D2 about 4
(the combined request, then reuse in a new process), D4 about 3 (the parent and two helpers).
Transcripts are summarised as metrics (no secrets) for the evidence.
"""

import time
import unittest

from acceptance_support import LIVE, criteria, record_metric
from test_live import LiveCase

CHAIN = ("Create chain/1.txt containing 1. Then repeatedly read the latest file and write the "
         "next file containing the value plus one, up to chain/6.txt. Then report the final "
         "value.")
GREET = ("Create notes/greet.txt containing the word hello, then use the shell to append a "
         "second line containing world, then show me the file. After that, save how you did "
         "this as a skill called greet-file.")
REUSE = "Using your saved greet-file skill, create notes/greet2.txt the same way."
DELEGATE = ("Delegate two independent sub-tasks to helpers in parallel: helper A writes "
            "par/a.txt containing alpha, helper B writes par/b.txt containing beta. Then tell "
            "me both file contents.")


def receipts_of(report):
    return [f"{r['tool']}:{r['state']}" for r in report["evidence"]["receipts"]]


@unittest.skipUnless(LIVE, "set BRAINSTEM_AGENT_LIVE=1 to spend real Copilot turns")
class LiveLongTurnTests(LiveCase):
    def summary(self, label, report, seconds=None):
        long_turn = (report.get("evidence") or {}).get("long_turn") or {}
        record_metric(f"{label}_transcript", {
            "state": report.get("state"), "turn_id": report.get("turn_id"),
            "seconds": seconds, "grail_requests": long_turn.get("grail_requests"),
            "segments": [{key: s.get(key) for key in ("segment", "state", "rounds",
                                                      "tool_calls", "exhausted", "seconds")}
                         for s in long_turn.get("segments", [])],
            "children": [{key: c.get(key) for key in ("index", "state", "seconds", "segments",
                                                      "tool_calls", "worker_id")}
                         for c in long_turn.get("children", [])],
            "receipts": receipts_of(report) if report.get("evidence") else [],
            "response": ((report.get("response") or {}).get("response")
                         or report.get("error") or "")[:500]})

    @criteria("D1", "D8", "D12")
    def test_d1_a_six_step_dependent_chain_finishes_in_one_command(self):
        started = time.monotonic()
        result, report = self.chat(CHAIN, "--capabilities", "files.read,files.write",
                                   timeout=900)
        seconds = round(time.monotonic() - started, 2)
        self.summary("d1", report, seconds)
        self.assert_success(result, report)
        for number in range(1, 7):
            self.assertEqual((self.workspace / "chain" / f"{number}.txt").read_text().strip(),
                             str(number))
        self.assertIn("6", report["response"]["response"])
        segments = report["evidence"]["long_turn"]["segments"]
        record_metric("d1_segments", len(segments))
        record_metric("d1_seconds", seconds)
        journal = self.cli("turns", "show", report["turn_id"])
        journaled = [s for s in journal["steps"] if s["kind"] == "segment"]
        self.assertEqual(len(journaled), len(segments), "every Grail request is a segment")
        self.assertTrue(all(s["turn_id"] == report["turn_id"] for s in journaled))
        self.assertEqual(journal["steps"][0]["state"], "succeeded")
        self.assertIn('"event": "segment.started"', result.stderr)  # --json progress events
        writes = [r for r in journal["receipts"] if r["tool"] == "write_file"]
        self.assertGreaterEqual(len(writes), 6)

    @criteria("D2", "D12")
    def test_d2_the_lead_request_saves_the_skill_and_a_new_process_reuses_it(self):
        started = time.monotonic()
        result, saved = self.chat(GREET, timeout=900)
        self.summary("d2_save", saved, round(time.monotonic() - started, 2))
        self.assert_success(result, saved)
        text = (self.workspace / "notes" / "greet.txt").read_text()
        record_metric("d2_greet_file", text[:200])
        self.assertIn("hello", text)
        self.assertIn("world", text)
        self.assertIn("skill_save:succeeded", receipts_of(saved))
        [skill] = [s for s in self.cli("skills", "list")["skills"] if s["name"] == "greet-file"]
        self.assertTrue(skill["offered"], skill)
        record_metric("d2_skill", {key: skill[key] for key in ("name", "review", "version",
                                                                "created_by")})
        started = time.monotonic()
        result, reuse = self.chat(REUSE, timeout=900)
        self.summary("d2_reuse", reuse, round(time.monotonic() - started, 2))
        self.assert_success(result, reuse)
        self.assertNotEqual(reuse["session_id"], saved["session_id"])
        loads = [r for r in self.cli("receipts", "--turn", reuse["turn_id"])["receipts"]
                 if r["tool"] == "skill_view" and r["state"] == "succeeded"]
        self.assertTrue(loads, receipts_of(reuse))
        self.assertEqual(loads[0]["result"]["evidence"]["loaded"], "greet-file")
        second = (self.workspace / "notes" / "greet2.txt").read_text()
        record_metric("d2_greet2_file", second[:200])
        self.assertIn("hello", second)
        self.assertIn("world", second)

    @criteria("D4", "D12")
    def test_d4_two_helpers_work_in_parallel_on_fresh_workers(self):
        started = time.monotonic()
        result, report = self.chat(DELEGATE, timeout=900)
        wall = round(time.monotonic() - started, 2)
        self.summary("d4", report, wall)
        self.assert_success(result, report)
        self.assertEqual((self.workspace / "par" / "a.txt").read_text().strip(), "alpha")
        self.assertEqual((self.workspace / "par" / "b.txt").read_text().strip(), "beta")
        answer = report["response"]["response"].lower()
        self.assertIn("alpha", answer)
        self.assertIn("beta", answer)
        children = report["evidence"]["long_turn"]["children"]
        self.assertGreaterEqual(len(children), 2)
        self.assertEqual(len({c["worker_id"] for c in children}), len(children))
        self.assertTrue(all(c["state"] == "succeeded" for c in children), children)
        journal = self.cli("turns", "show", report["turn_id"])
        kids = [s["step_id"] for s in journal["steps"] if s["kind"] == "child"]
        self.assertEqual(len(kids), len(children))
        # Parallel: the helpers' Grail requests (their journaled segments) overlap in time.
        first = {}
        for step in journal["steps"]:
            if step["kind"] == "segment" and step["turn_id"] in kids:
                first.setdefault(step["turn_id"], (step["started_at"], step["finished_at"]))
        (a_start, a_end), (b_start, b_end) = list(first.values())[:2]
        self.assertLess(max(a_start, b_start), min(a_end, b_end), "helpers ran in parallel")
        [delegate] = [r for r in journal["receipts"] if r["tool"] == "delegate_tasks"]
        timing = delegate["result"]["evidence"]
        speedup = round(timing["serial_seconds"] / timing["wall_seconds"], 2)
        record_metric("d4_parallel_speedup", speedup)
        record_metric("d4_child_seconds", [c["seconds"] for c in children])
        self.assertGreater(speedup, 1.2)
        linked = [r for r in self.cli("receipts", "--turn", report["turn_id"])["receipts"]
                  if r.get("parent_turn") == report["turn_id"]]
        self.assertTrue(any(r["tool"] == "write_file" for r in linked), linked)


if __name__ == "__main__":
    unittest.main()
