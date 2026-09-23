"""A10/A11 specs: the suite itself is traceable and its evidence is sanitized."""

import importlib
import os
import pwd
import re
import subprocess
import unittest
from pathlib import Path

from acceptance_support import CANARY_TOKEN, REPO, criteria

import run_acceptance

HERE = Path(__file__).resolve().parent


def tagged_tests():
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test*.py", top_level_dir=str(HERE))
    found = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        elif isinstance(item, unittest.TestCase):
            function = getattr(type(item), item._testMethodName, None)
            module = type(item).__module__.split(".")[-1]
            found.append((module, getattr(function, "criteria", ())))

    walk(suite)
    return found


class TraceabilityTests(unittest.TestCase):
    @criteria("A11")
    def test_every_criterion_has_tests_in_its_required_evidence_class(self):
        coverage = {}
        for module, ids in tagged_tests():
            for item in ids:
                coverage.setdefault(item, set()).add(run_acceptance.evidence_class(module))
        for criterion, (_title, required) in run_acceptance.CRITERIA.items():
            with self.subTest(criterion):
                self.assertIn(required, coverage.get(criterion, set()),
                              f"{criterion} lacks a {required} test")

    @criteria("A10")
    def test_preexisting_suites_are_still_part_of_discovery(self):
        modules = {module for module, _ in tagged_tests()}
        for name in run_acceptance.PREEXISTING:
            self.assertIn(name, modules)
            importlib.import_module(name)

    @criteria("A11")
    def test_evidence_classes_follow_module_names(self):
        self.assertEqual(run_acceptance.evidence_class("test_real_core"), "real-core")
        self.assertEqual(run_acceptance.evidence_class("test_live"), "live")
        self.assertEqual(run_acceptance.evidence_class("test_cell_host"), "unit")

    @criteria("A11")
    def test_redaction_covers_known_values_and_token_shapes(self):
        text = f"a {CANARY_TOKEN} b ghp_{'A' * 36} c github_pat_{'b' * 30} d secret-value"
        clean = run_acceptance.redact(text, ["secret-value"])
        self.assertNotIn(CANARY_TOKEN, clean)
        self.assertNotIn("A" * 36, clean)
        self.assertNotIn("b" * 30, clean)
        self.assertNotIn("secret-value", clean)

    @criteria("A11")
    def test_tracked_files_never_name_this_machines_home(self):
        """The repository is public: no tracked file (code, fixture, data or doc) holds the
        home directory of the account running the tests."""
        homes = {os.path.realpath(os.path.expanduser("~")), pwd.getpwuid(os.getuid()).pw_dir}
        patterns = [re.compile(re.escape(home.encode()) + rb"(?![A-Za-z0-9._-])")
                    for home in sorted(homes) if len(Path(home).parts) >= 3]
        try:
            listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                                     capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            listing = None
        if listing is None or listing.returncode != 0:
            self.skipTest("not a git checkout (git ls-files is unavailable)")
        found = []
        for name in filter(None, os.fsdecode(listing.stdout).split("\0")):
            try:
                data = (REPO / name).read_bytes()
            except OSError:
                continue
            if any(pattern.search(data) for pattern in patterns):
                found.append(name)
        self.assertEqual(found, [], "tracked files name this machine's home directory")

    @criteria("A11")
    def test_the_evidence_label_is_neutral_and_recorded(self):
        parser = run_acceptance.build_parser()
        defaults = parser.parse_args(["--output", "evidence.json"])
        self.assertEqual(defaults.label, "local")
        self.assertEqual(parser.parse_args(["--output", "e.json", "--label", "ci"]).label, "ci")
        report = run_acceptance.report_header(defaults, commit="0" * 40, dirty=False)
        self.assertEqual(report["label"], "local")
        self.assertFalse(report["working_tree_dirty"])
        self.assertEqual(set(report), {"schema", "product", "label", "generated_at", "commit",
                                       "working_tree_dirty"})


if __name__ == "__main__":
    unittest.main()
