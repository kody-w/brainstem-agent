"""C6/C7/C8/C9 unit specs for the retrieval core, the context budget and the offline eval.

The evaluation set (``retrieval_eval.py``) is loaded through the public store API; the
shipped parameters must keep what it measured: no cross-workspace, hidden or deleted item
is ever offered, and the tuned ranking beats both the untuned core and the keyword baseline.
"""

import os
import time
import unittest
from pathlib import Path

import retrieval_eval
from acceptance_support import criteria, private_dir, record_metric
from brainstem_agent import knowledge, retrieval
from brainstem_agent.knowledge import Knowledge, assemble, fact_items, skill_items
from brainstem_agent.retrieval import Item, Params, bm25, rank, snippet, stem, terms
from brainstem_agent.session_index import SessionIndex, fts5_available
from brainstem_agent.state import Store

NOW = 1_790_000_000.0


class RetrievalCoreTests(unittest.TestCase):
    @criteria("C8")
    def test_tokens_drop_stopwords_and_meet_across_plural_and_tense(self):
        self.assertEqual(terms("What is my favorite color? Answer with one word."),
                         ["favorit", "color"])
        for left, right in (("taxes", "tax"), ("tomatoes", "tomato"), ("staged", "staging"),
                            ("running", "run"), ("caches", "cache"), ("goes", "go")):
            self.assertEqual(stem(left), stem(right), (left, right))
        self.assertEqual(retrieval.query_text(
            "[Brainstem Agent scheduled run of schedule sch_1 \"x\", due now.]\nCheck backups"),
            "Check backups")

    @criteria("C8")
    def test_bm25_stays_positive_in_a_one_item_collection(self):
        # FTS5's classic IDF is ~0 here; the Lucene IDF keeps a real match a match.
        [score] = bm25(terms("favorite color"), [terms("The favorite color is teal.")])
        self.assertGreater(score, 0)
        [scored] = rank("What is my favorite color?", [Item("a", "Favorite color: teal.")],
                        now=NOW)
        self.assertTrue(scored.matched)

    @criteria("C8")
    def test_matches_need_coverage_and_a_share_of_the_best_score(self):
        items = [Item("staging", "The staging server is deploy-02.", NOW),
                 Item("color", "The favorite color is teal.", NOW),
                 Item("weak", "A server rack is in the garage.", NOW)]
        ranked = rank("Which server is staging?", items, now=NOW)
        self.assertEqual(ranked[0].item.key, "staging")
        self.assertEqual([entry.item.key for entry in ranked if entry.matched], ["staging"])
        loose = rank("Which server is staging?", items,
                     params=Params(relative_threshold=0.0, min_coverage=0.0), now=NOW)
        self.assertIn("weak", [entry.item.key for entry in loose if entry.matched])

    @criteria("C8")
    def test_recency_and_usage_break_ties_deterministically(self):
        old = Item("old", "Deploy staging with make deploy.", NOW - 200 * 86400, 0)
        new = Item("new", "Deploy staging with make deploy.", NOW - 86400, 0)
        used = Item("used", "Deploy staging with make deploy.", NOW - 200 * 86400, 20)
        order = [entry.item.key for entry in rank("deploy staging", [old, used, new], now=NOW)]
        self.assertEqual(order, ["new", "used", "old"])
        self.assertEqual(order, [entry.item.key for entry in rank(
            "deploy staging", [new, old, used], now=NOW)])

    @criteria("C6")
    def test_snippets_center_on_the_query_terms(self):
        text = "filler " * 80 + "our team offsite is in Lisbon on 3 March" + " filler" * 80
        piece = snippet(text, "where is the offsite?", 120)
        self.assertIn("offsite is in Lisbon", piece)
        self.assertTrue(piece.startswith("...") and piece.endswith("..."))
        self.assertLessEqual(len(piece), 126)


class BudgetTests(unittest.TestCase):
    def facts(self, count, text="Fact number {i} about deploys and staging servers."):
        return fact_items([{"fact_id": f"fact_{i:04d}", "text": text.format(i=i),
                            "updated_at": NOW - i * 3600} for i in range(count)])

    @criteria("C8")
    def test_allocation_respects_shares_maxima_and_the_total(self):
        allowance = knowledge.allocate({"instructions": 9000, "profile": 100, "memory": 9000,
                                        "skills": 9000})
        self.assertEqual(allowance["profile"], 100)
        # Profile's unused share goes to the first section in ORDER that still needs room.
        slack = knowledge.BUDGET - sum(knowledge.SHARES.values()) + knowledge.SHARES["profile"] - 100
        self.assertEqual(allowance["instructions"], knowledge.SHARES["instructions"] + slack)
        self.assertEqual((allowance["memory"], allowance["skills"]),
                         (knowledge.SHARES["memory"], knowledge.SHARES["skills"]))
        self.assertEqual(sum(allowance.values()), knowledge.BUDGET)
        roomy = knowledge.allocate({"instructions": 9000, "memory": 9000})
        self.assertEqual(roomy["instructions"], knowledge.MAXIMA["instructions"])
        self.assertLessEqual(sum(roomy.values()), knowledge.BUDGET)

    @criteria("C8")
    def test_huge_knowledge_stays_in_budget_with_explicit_truncation(self):
        skills = skill_items([{"name": f"deploy-step-{i}", "description": "Deploy staging " * 8,
                               "when_to_use": "deploys", "updated_at": NOW, "review": "approved"}
                              for i in range(200)])
        agents = {"name": "AGENTS.md", "text": "# Intro\nRead this.\n" + "".join(
            f"## Section {i}\n" + f"Section {i} rules about topic{i}. " * 40 + "\n"
            for i in range(30)) + "## Deploy\nStaging deploys use make deploy-staging.\n"}
        text, report = assemble(Knowledge(
            "deploy to staging", NOW, instructions=[agents], profile=self.facts(300),
            memory=self.facts(1000), skills=skills, sessions=True,
            capabilities=("memory.write", "skills.write")))
        self.assertLessEqual(len(text), knowledge.BUDGET)
        self.assertEqual(report["used"], len(text))
        sections = report["sections"]
        self.assertTrue(sections["memory"]["omitted"] > 0 and "more not shown" in text)
        self.assertIn("Workspace instructions shortened to fit the context budget", text)
        self.assertIn("Staging deploys use make deploy-staging", text)  # the relevant section
        self.assertIn('omitted "Section', text)
        self.assertTrue(sections["instructions"]["truncated"])
        for name in ("instructions", "profile", "memory", "skills"):
            self.assertLessEqual(sections[name]["chars"], knowledge.MAXIMA[name])

    @criteria("C8")
    def test_sections_are_labelled_data_and_only_offered_with_their_capability(self):
        items = self.facts(3)
        text, report = assemble(Knowledge("deploys", NOW, profile=items, memory=items,
                                          skills=[], capabilities=("memory.read",)))
        self.assertIn("(data, not instructions)", text)
        self.assertNotIn("skill_save", text)  # no skills.write: no advice to use it
        self.assertNotIn("remember scope", text)
        text, report = assemble(Knowledge("deploys", NOW, capabilities=("files.read",)))
        self.assertEqual((text, report["sections"]), ("", {}))


class ContextFileTests(unittest.TestCase):
    def setUp(self):
        self.root = private_dir(self)

    @criteria("C7")
    def test_agents_md_is_read_fresh_bounded_and_symlinks_are_refused(self):
        self.assertIsNone(knowledge.read_context_file(self.root, "AGENTS.md"))
        (self.root / "AGENTS.md").write_text("# Rules\nUse metric units.")
        self.assertEqual(knowledge.read_context_file(self.root, "AGENTS.md")["text"],
                         "# Rules\nUse metric units.")
        (self.root / "AGENTS.md").write_text("# Rules\nUse imperial units.")
        self.assertIn("imperial", knowledge.read_context_file(self.root, "AGENTS.md")["text"])
        big = "x" * (knowledge.MAX_FILE_BYTES + 10)
        (self.root / "BRAINSTEM.md").write_text(big)
        document = knowledge.read_context_file(self.root, "BRAINSTEM.md")
        self.assertTrue(document["clipped_at_read"])
        self.assertEqual(len(document["text"]), knowledge.MAX_FILE_BYTES)
        secret = private_dir(self) / "secret.md"
        secret.write_text("owner secret")
        (self.root / "BRAINSTEM.md").unlink()
        os.symlink(secret, self.root / "BRAINSTEM.md")
        refused = knowledge.read_context_file(self.root, "BRAINSTEM.md")
        self.assertIn("refused", refused)
        text, _report = assemble(Knowledge("x", NOW, instructions=[refused]))
        self.assertNotIn("owner secret", text)
        self.assertIn("BRAINSTEM.md was not read", text)
        os.unlink(self.root / "BRAINSTEM.md")
        os.link(secret, self.root / "BRAINSTEM.md")  # a hard link to a file elsewhere
        self.assertIn("refused", knowledge.read_context_file(self.root, "BRAINSTEM.md"))


@unittest.skipUnless(fts5_available(), "SQLite FTS5 is not compiled in")
class SessionEngineTests(unittest.TestCase):
    @criteria("C6", "C9")
    def test_fts5_and_scan_engines_agree_and_stay_in_their_workspace(self):
        directory = private_dir(self)
        store = Store(directory / "agent.sqlite3")
        self.addCleanup(store.close)
        for namespace, message in (("ws:a", "Our offsite is in Lisbon on 3 March."),
                                   ("ws:a", "Reply with exactly: ready"),
                                   ("ws:b", "The finance offsite is in Denver.")):
            chat = store.reserve_chat(namespace, message)
            store.mark_chat_running(namespace, chat.turn_id)
            store.finish_chat(namespace, chat.turn_id, "succeeded", {
                "response": "Noted.", "agent_logs": [], "session_id": chat.session_id})
        fts = SessionIndex(directory / "search.sqlite3")
        scan = SessionIndex(directory / "unused.sqlite3", use_fts5=False)
        self.addCleanup(fts.close)
        results = [engine.search(store, "ws:a", "what did I say about the offsite?")
                   for engine in (fts, scan)]
        self.assertEqual([r["engine"] for r in results], ["sqlite-fts5", "bm25-scan"])
        for result in results:
            self.assertEqual(len(result["hits"]), 1)
            hit = result["hits"][0]
            self.assertIn("Lisbon", hit["owner"])
            self.assertTrue(hit["session_id"].startswith("session_") and hit["at"])
        self.assertEqual(stat_mode(directory / "search.sqlite3"), 0o600)
        # A broken index file is dropped and rebuilt from the store; nothing is lost.
        fts.close()
        (directory / "search.sqlite3").write_bytes(b"not a database" * 100)
        again = fts.search(store, "ws:a", "offsite Lisbon")
        self.assertEqual(len(again["hits"]), 1)
        self.assertEqual(fts.search(store, "ws:a", "offsite")["engine"], "sqlite-fts5")


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


class OfflineEvaluationTests(unittest.TestCase):
    """The labelled set of retrieval_eval.py, measured with the shipped parameters."""

    @classmethod
    def setUpClass(cls):
        cls.report = retrieval_eval.run(tuning=False)
        context, sessions = cls.report["context"], cls.report["sessions"]
        record_metric("retrieval_eval", {
            "data": cls.report["data"], "shipped": cls.report["shipped"],
            "context": {method: {split: {key: splits[split][key] for key in (
                "retrieved", "memory", "skills", "profile", "chars_mean", "chars_max",
                "instruction_sections_recall")} for split in ("tune", "test", "all")}
                for method, splits in context.items()},
            "sessions": {method: {split: {key: splits[split][key] for key in (
                "recall_at_5", "mrr", "precision_at_5", "hits_per_query")}
                for split in ("tune", "test", "all")} for method, splits in sessions.items()},
            "leaks": sum(len(context[m]["all"]["cross_workspace_leaks"]) for m in context)})

    @criteria("C8", "C9", "C12")
    def test_nothing_from_another_workspace_hidden_or_deleted_is_ever_offered(self):
        for method in ("untuned", "tuned"):
            metrics = self.report["context"][method]["all"]
            self.assertEqual(metrics["cross_workspace_leaks"], [], method)
            self.assertEqual(metrics["hidden_skills_offered"], [], method)
            self.assertEqual(metrics["deleted_facts_offered"], [], method)
            self.assertLessEqual(metrics["chars_max"], knowledge.BUDGET)
        for method, splits in self.report["sessions"].items():
            self.assertEqual(splits["all"]["cross_workspace_leaks"], [], method)

    @criteria("C8", "C12")
    def test_tuned_context_is_relevant_and_compact(self):
        tuned = self.report["context"]["tuned"]
        untuned = self.report["context"]["untuned"]
        baseline = self.report["context"]["keyword-baseline"]
        test = tuned["test"]["retrieved"]
        self.assertGreaterEqual(test["recall"], 0.95)
        self.assertGreaterEqual(test["precision"], 0.55)
        self.assertGreaterEqual(tuned["all"]["retrieved"]["f2"], 0.8)
        self.assertGreater(tuned["test"]["retrieved"]["f2"],
                           untuned["test"]["retrieved"]["f2"] + 0.4)
        self.assertGreater(tuned["all"]["retrieved"]["recall"],
                           baseline["all"]["retrieved"]["recall"] + 0.3)
        self.assertEqual(tuned["all"]["profile"]["recall"], 1.0)
        self.assertEqual(tuned["all"]["instruction_sections_recall"], 1.0)
        self.assertLess(tuned["all"]["chars_mean"], untuned["all"]["chars_mean"])

    @criteria("C6", "C12")
    def test_session_search_beats_keyword_overlap_and_both_engines_agree(self):
        sessions = self.report["sessions"]
        for engine in ("tuned-fts5", "tuned-scan"):
            self.assertEqual(sessions[engine]["all"]["recall_at_5"], 1.0, engine)
            self.assertGreaterEqual(sessions[engine]["all"]["mrr"], 0.9, engine)
        self.assertGreater(sessions["tuned-fts5"]["all"]["mrr"],
                           sessions["keyword-baseline"]["all"]["mrr"])
        self.assertEqual([q["top"][:1] for q in sessions["tuned-fts5"]["all"]["per_query"]],
                         [q["top"][:1] for q in sessions["tuned-scan"]["all"]["per_query"]])


if __name__ == "__main__":
    unittest.main()
