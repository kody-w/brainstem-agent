"""Run the acceptance suite (Cell v1 A1-A11, always-on cell B1-B12, learning cell C1-C12,
long-horizon cell D1-D12, reaching cell E1-E12, companion surfaces G1-G12, operable cell
H1-H12) and write sanitized, traceable evidence (A11, B12, C12, D12, E12, G12, H12).

    PYTHONPATH=runtime python runtime/tests/run_acceptance.py [--real-core] [--live] \
        [--python-matrix /path/to/python3.13] [--label NAME] --output evidence.json

Each test carries the criteria it proves (``@criteria``); its evidence class comes
from its module (unit, real-core, live). The file never contains credential
values: it is scanned for the installed credential and token-shaped strings
before it is written.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent
REPO = RUNTIME.parent
PREEXISTING = ("test_adapter", "test_harness", "test_organ_base", "test_state")
# The suites each later milestone added (the rest proves Cell v1 and the offline harness).
ALWAYS_ON = ("test_cell_schedules", "test_cell_daemon", "test_real_daemon", "test_live_daemon")
LEARNING = ("test_cell_retrieval", "test_cell_skills", "test_cell_learning", "test_real_learning",
            "test_live_learning", "test_cell_hardening")
LONG_HORIZON = ("test_cell_longturn", "test_cell_delegation", "test_cell_programmatic",
                "test_cell_durability", "test_real_longturn", "test_live_longturn",
                "test_cell_longturn_hardening")
REACHING = ("test_cell_reach", "test_real_reach", "test_live_reach", "test_cell_reach_hardening")
COMPANION = ("test_companion_security", "test_companion_api", "test_companion_repl",
             "test_companion_browser", "test_live_companion")
OPERABLE = ("test_cell_operable_release", "test_cell_operable_health",
            "test_cell_operable_credential", "test_cell_operable_resources",
            "test_cell_operable_observability", "test_cell_operable_backup",
            "test_cell_operable_migration", "test_cell_operable_upgrade",
            "test_cell_operable_uninstall", "test_cell_operable_hardening",
            "test_real_operable", "test_live_operable")
CRITERIA = {
    "A1": ("doctor", "real-core"),
    "A2": ("live tool turn", "live"),
    "A3": ("memory across processes", "live"),
    "A4": ("session continuity", "live"),
    "A5": ("sandboxed shell", "unit"),
    "A6": ("isolation", "real-core"),
    "A7": ("integrity", "real-core"),
    "A8": ("failure semantics", "live"),
    "A9": ("replay", "live"),
    "A10": ("regression on 3.11 and 3.13", "unit"),
    "A11": ("sanitized evidence", "unit"),
    "B1": ("daemon lifecycle", "real-core"),
    "B2": ("warm path", "live"),
    "B3": ("live schedule from conversation", "live"),
    "B4": ("management from CLI and chat tools", "unit"),
    "B5": ("time correctness", "unit"),
    "B6": ("missed runs", "real-core"),
    "B7": ("overlap", "unit"),
    "B8": ("crash safety", "unit"),
    "B9": ("authority", "unit"),
    "B10": ("always-on install", "unit"),
    "B11": ("regression on 3.11 and 3.13", "unit"),
    "B12": ("sanitized evidence with fire delays and warm latency", "unit"),
    "C1": ("skill creation", "live"),
    "C2": ("skill reuse", "live"),
    "C3": ("skill refinement", "live"),
    "C4": ("skill governance", "unit"),
    "C5": ("profile memory", "live"),
    "C6": ("session search", "live"),
    "C7": ("workspace context file", "real-core"),
    "C8": ("context budget", "real-core"),
    "C9": ("isolation and poisoning", "live"),
    "C10": ("scheduled and daemon parity", "live"),
    "C11": ("regression on 3.11 and 3.13", "unit"),
    "C12": ("sanitized evidence with live transcript summaries", "unit"),
    "D1": ("continuation across Grail requests", "live"),
    "D2": ("a combined task and learning request", "live"),
    "D3": ("budgets and honest partial results", "unit"),
    "D4": ("delegation to parallel helpers", "live"),
    "D5": ("delegation bounds", "unit"),
    "D6": ("programmatic tool calls", "unit"),
    "D7": ("background processes", "unit"),
    "D8": ("progress and cancellation", "unit"),
    "D9": ("crash safety of long turns", "real-core"),
    "D10": ("authority of continuations, helpers and inner calls", "unit"),
    "D11": ("regression on 3.11 and 3.13", "unit"),
    "D12": ("sanitized evidence with segment and helper timing and live transcripts", "unit"),
    "E1": ("web fetch", "unit"),
    "E2": ("web research (live)", "live"),
    "E3": ("web search (live)", "live"),
    "E4": ("MCP over stdio (live)", "live"),
    "E5": ("MCP over HTTP", "unit"),
    "E6": ("filtering and capabilities", "real-core"),
    "E7": ("untrusted content", "live"),
    "E8": ("egress policy", "unit"),
    "E9": ("MCP server lifecycle", "unit"),
    "E10": ("parity", "live"),
    "E11": ("regression on 3.11 and 3.13", "unit"),
    "E12": ("sanitized evidence with live transcripts and the outbound request log", "unit"),
    "G1": ("interactive terminal", "unit"),
    "G2": ("web companion", "unit"),
    "G3": ("mirrors and route parity", "unit"),
    "G4": ("owner-only security", "unit"),
    "G5": ("honest states", "unit"),
    "G6": ("accessibility and layout", "unit"),
    "G7": ("speed and size", "unit"),
    "G8": ("same-state parity (live)", "live"),
    "G9": ("browser end-to-end (live)", "live"),
    "G10": ("no new runtime dependencies", "unit"),
    "G11": ("regression on 3.11 and 3.13", "unit"),
    "G12": ("sanitized evidence with axe, size, timing and live transcripts", "unit"),
    "H1": ("install into a fresh virtual environment, online and offline", "real-core"),
    "H2": ("version and release manifest", "unit"),
    "H3": ("backup, restore and export", "real-core"),
    "H4": ("migration safety", "unit"),
    "H5": ("upgrade and rollback", "real-core"),
    "H6": ("uninstall", "unit"),
    "H7": ("liveness versus readiness", "real-core"),
    "H8": ("credential lifecycle", "live"),
    "H9": ("resource hygiene", "unit"),
    "H10": ("local observability", "unit"),
    "H11": ("regression on 3.11 and 3.13", "unit"),
    "H12": ("sanitized evidence: install transcript, backup round trip, fault-injected "
            "migration, live turn after restore", "live"),
}
RANK = {"unit": 0, "real-core": 1, "live": 2}


def evidence_class(module: str) -> str:
    if module.startswith("test_real"):
        return "real-core"
    if module.startswith("test_live"):
        return "live"
    return "unit"


def redact(text: str, needles: list[str]) -> str:
    for needle in needles:
        if needle:
            text = text.replace(needle, "[REDACTED]")
    return re.sub(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]{6,}", r"\1[REDACTED]", text)


class Recorder(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._started = {}

    def startTest(self, test):
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def _record(self, test, outcome, detail=""):
        method = getattr(test, "_testMethodName", "")
        function = getattr(type(test), method, None)
        module = type(test).__module__.split(".")[-1]
        self.records.append({
            "id": test.id(),
            "module": module,
            "class": evidence_class(module),
            "criteria": list(getattr(function, "criteria", ())),
            "outcome": outcome,
            "seconds": round(time.monotonic() - self._started.get(test.id(), time.monotonic()), 3),
            "detail": detail.strip().splitlines()[-1][:300] if detail.strip() else "",
        })

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        if isinstance(test, unittest.TestCase):
            self._record(test, "error", self._exc_info_to_string(err, test))
        else:
            self.records.append({"id": str(test), "module": "?", "class": "unit", "criteria": [],
                                 "outcome": "error", "seconds": 0.0,
                                 "detail": self._exc_info_to_string(err, test).strip().splitlines()[-1][:300]})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "expected-failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected-success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._record(subtest, "failed", self._exc_info_to_string(err, subtest))


def unit_matrix(python: str, real_core: bool = False) -> dict:
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(RUNTIME), "HOME": os.path.expanduser("~"),
           "LANG": "en_US.UTF-8", "BRAINSTEM_AGENT_REAL_CORE": "1" if real_core else "0"}
    env.update({key: os.environ[key] for key in ("TMPDIR", "BRAINSTEM_AGENT_TEST_CACHE")
                if os.environ.get(key)})
    started = time.monotonic()
    result = subprocess.run([python, "-m", "unittest", "discover", "-s", str(HERE), "-q"],
                            capture_output=True, text=True, env=env, cwd=str(REPO), timeout=1800)
    tail = result.stderr.strip().splitlines()[-3:]
    ran = re.search(r"Ran (\d+) tests?", result.stderr)
    version = subprocess.run([python, "-c", "import platform; print(platform.python_version())"],
                             capture_output=True, text=True).stdout.strip()
    return {"python": python, "version": version, "real_core": real_core, "exit": result.returncode,
            "ran": int(ran.group(1)) if ran else 0, "summary": tail[-1] if tail else "",
            "passed": result.returncode == 0, "seconds": round(time.monotonic() - started, 1)}


def git(*arguments) -> str:
    return subprocess.run(["git", "-C", str(REPO), *arguments], capture_output=True,
                          text=True).stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--real-core", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--python-matrix", nargs="*", default=[])
    parser.add_argument("--matrix-real-core", action="store_true",
                        help="also run the real-core tier in the matrix interpreters")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--label", default="local",
                        help="a free-form name for this run, recorded in the evidence")
    # Repeated flags accumulate (``--only a --only b`` runs both), like one flag with two values.
    parser.add_argument("--only", nargs="+", action="extend", default=[],
                        help="test name patterns (unittest -k), to rerun single tests")
    parser.add_argument("--exclude", nargs="+", action="extend", default=[],
                        help="test modules to leave out (their records can be --merge'd)")
    parser.add_argument("--merge", nargs="+", action="extend", type=Path, default=[],
                        help="evidence files of the same commit whose test records and "
                             "metrics are added before criteria are computed")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def report_header(arguments, *, commit: str, dirty: bool) -> dict:
    """The evidence file's identifying fields."""
    return {
        "schema": "brainstem-agent/cell-evidence-v1",
        "product": "Brainstem Agent",
        "label": arguments.label,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "working_tree_dirty": dirty,
    }


def main() -> int:
    arguments = build_parser().parse_args()
    os.environ["BRAINSTEM_AGENT_REAL_CORE"] = "1" if arguments.real_core else "0"
    os.environ["BRAINSTEM_AGENT_LIVE"] = "1" if arguments.live else "0"
    metrics_file = Path(tempfile.mkdtemp(prefix="ba-metrics-")).resolve() / "metrics.jsonl"
    os.environ["BRAINSTEM_AGENT_METRICS_FILE"] = str(metrics_file)
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(RUNTIME))
    started = time.monotonic()
    loader = unittest.TestLoader()
    loader.testNamePatterns = [f"*{item}*" for item in arguments.only] or None
    suite = loader.discover(str(HERE), pattern=arguments.pattern, top_level_dir=str(HERE))
    if arguments.exclude:
        def kept(item):
            if isinstance(item, unittest.TestSuite):
                return unittest.TestSuite(kept(child) for child in item)
            return item if type(item).__module__.split(".")[-1] not in arguments.exclude \
                else unittest.TestSuite()
        suite = kept(suite)
    runner = unittest.TextTestRunner(resultclass=Recorder, verbosity=1, stream=sys.stderr)
    result = runner.run(suite)
    records = result.records
    merged_metrics, merged_from = [], []
    for path in arguments.merge:
        other = json.loads(path.read_text())
        if other.get("commit") != git("rev-parse", "HEAD"):
            raise SystemExit(f"{path} was produced on commit {other.get('commit')}, not HEAD")
        records += [{**record, "merged_from": path.name} for record in other["tests"]]
        merged_metrics += other.get("metrics", [])
        merged_from.append({"file": path.name, "commands": other.get("commands"),
                            "generated_at": other.get("generated_at"),
                            "tests": len(other["tests"])})

    import acceptance_support

    try:
        from brainstem_agent import grail, sandbox

        grail_info = {"repository": "kody-w/rapp-installer", "commit": grail.PINNED_COMMIT,
                      "version": grail.VERSION, "kernel_sha256": grail.KERNEL_SHA256}
        sandbox_available = sandbox.available()
        environment_id = sandbox.ENVIRONMENT if sandbox_available else "macos-no-sandbox"
    except Exception as error:  # the red run records the absent product honestly
        grail_info = {"unavailable": type(error).__name__}
        sandbox_available = os.path.exists("/usr/bin/sandbox-exec")
        environment_id = "product-unavailable"
    needles = [value for value in acceptance_support.real_credential_needles().values() if value]
    needles.append(acceptance_support.CANARY_TOKEN)
    classes = {}
    for record in records:
        bucket = classes.setdefault(record["class"], {"passed": 0, "failed": 0, "error": 0,
                                                       "skipped": 0})
        key = record["outcome"] if record["outcome"] in bucket else "failed"
        bucket[key] += 1
    criteria = {}
    for criterion, (title, required) in CRITERIA.items():
        tagged = [r for r in records if criterion in r["criteria"]]
        ran = [r for r in tagged if r["outcome"] != "skipped"]
        passed = [r for r in ran if r["outcome"] == "passed"]
        best = max((RANK[r["class"]] for r in passed), default=-1)
        criteria[criterion] = {
            "title": title,
            "required_class": required,
            "tests": len(tagged),
            "ran": len(ran),
            "passed": len(passed),
            "failed": [r["id"] for r in ran if r["outcome"] != "passed"],
            "highest_class_passed": next((k for k, v in RANK.items() if v == best), None),
            "pass": bool(ran) and len(passed) == len(ran) and best >= RANK[required],
        }
    preexisting = [r for r in records if r["module"] in PREEXISTING]
    matrix = [unit_matrix(python, arguments.matrix_real_core) for python in arguments.python_matrix]
    if matrix:
        criteria["A10"]["matrix"] = matrix
        criteria["A10"]["pass"] = criteria["A10"]["pass"] and all(m["passed"] for m in matrix)
    criteria["A10"]["preexisting_tests"] = len(preexisting)
    criteria["A10"]["preexisting_passed"] = sum(r["outcome"] == "passed" for r in preexisting)
    earlier = [r for r in records if r["module"] not in ALWAYS_ON + LEARNING
               and r["outcome"] != "skipped"]
    criteria["B11"]["cell_v1_tests_ran"] = len(earlier)
    criteria["B11"]["cell_v1_passed"] = sum(r["outcome"] == "passed" for r in earlier)
    if matrix:
        criteria["B11"]["matrix"] = matrix
    criteria["B11"]["pass"] = (criteria["B11"]["pass"] and all(m["passed"] for m in matrix)
                               and criteria["B11"]["cell_v1_passed"] == len(earlier))
    metrics = list(merged_metrics)
    if metrics_file.exists():
        metrics += [json.loads(line) for line in metrics_file.read_text().splitlines() if line]
        metrics_file.unlink()
    metrics_file.parent.rmdir()
    criteria["B12"]["metrics_recorded"] = sorted({m["name"] for m in metrics
                                                  if "fire_delay" in m["name"]
                                                  or "chat_seconds" in m["name"]})
    before = [r for r in records if r["module"] not in LEARNING and r["outcome"] != "skipped"]
    criteria["C11"]["pre_learning_tests_ran"] = len(before)
    criteria["C11"]["pre_learning_passed"] = sum(r["outcome"] == "passed" for r in before)
    if matrix:
        criteria["C11"]["matrix"] = matrix
    criteria["C11"]["pass"] = (criteria["C11"]["pass"] and all(m["passed"] for m in matrix)
                               and criteria["C11"]["pre_learning_passed"] == len(before))
    before4 = [r for r in records if r["module"] not in LONG_HORIZON and r["outcome"] != "skipped"]
    criteria["D11"]["pre_long_horizon_tests_ran"] = len(before4)
    criteria["D11"]["pre_long_horizon_passed"] = sum(r["outcome"] == "passed" for r in before4)
    if matrix:
        criteria["D11"]["matrix"] = matrix
    criteria["D11"]["pass"] = (criteria["D11"]["pass"] and all(m["passed"] for m in matrix)
                               and criteria["D11"]["pre_long_horizon_passed"] == len(before4))
    names = {m["name"] for m in metrics}
    criteria["D12"]["transcripts_recorded"] = sorted(
        name for name in names if name.startswith("d") and name.endswith("_transcript"))
    criteria["D12"]["timing_recorded"] = sorted(
        name for name in names if name.startswith("d") and ("segments" in name
                                                            or "seconds" in name
                                                            or "speedup" in name))
    before5 = [r for r in records if r["module"] not in REACHING and r["outcome"] != "skipped"]
    criteria["E11"]["pre_reaching_tests_ran"] = len(before5)
    criteria["E11"]["pre_reaching_passed"] = sum(r["outcome"] == "passed" for r in before5)
    if matrix:
        criteria["E11"]["matrix"] = matrix
    criteria["E11"]["pass"] = (criteria["E11"]["pass"] and all(m["passed"] for m in matrix)
                               and criteria["E11"]["pre_reaching_passed"] == len(before5))
    criteria["E12"]["transcripts_recorded"] = sorted(
        name for name in names if name.startswith("e") and name.endswith("_transcript"))
    criteria["E12"]["egress_logs_recorded"] = sorted(
        name for name in names if name.startswith("e") and name.endswith("_egress"))
    before6 = [r for r in records if r["module"] not in COMPANION and r["outcome"] != "skipped"]
    criteria["G11"]["pre_companion_tests_ran"] = len(before6)
    criteria["G11"]["pre_companion_passed"] = sum(r["outcome"] == "passed" for r in before6)
    if matrix:
        criteria["G11"]["matrix"] = matrix
    criteria["G11"]["pass"] = (criteria["G11"]["pass"] and all(m["passed"] for m in matrix)
                               and criteria["G11"]["pre_companion_passed"] == len(before6))
    criteria["G12"]["metrics_recorded"] = sorted(name for name in names if name.startswith("g"))
    before7 = [r for r in records if r["module"] not in OPERABLE and r["outcome"] != "skipped"]
    criteria["H11"]["pre_operable_tests_ran"] = len(before7)
    criteria["H11"]["pre_operable_passed"] = sum(r["outcome"] == "passed" for r in before7)
    if matrix:
        criteria["H11"]["matrix"] = matrix
    criteria["H11"]["pass"] = (criteria["H11"]["pass"] and all(m["passed"] for m in matrix)
                               and criteria["H11"]["pre_operable_passed"] == len(before7))
    wanted = ("h1_install_transcript", "h3_backup_restore", "h4_fault_injected_migration",
              "h12_live_turn_after_restore")
    criteria["H12"]["evidence_recorded"] = sorted(name for name in names if name in wanted)
    criteria["H12"]["pass"] = criteria["H12"]["pass"] and all(name in names for name in wanted)
    criteria["C12"]["transcripts_recorded"] = sorted({m["name"] for m in metrics
                                                      if m["name"].endswith("_transcript")})
    criteria["C12"]["retrieval_eval_recorded"] = any(m["name"] == "retrieval_eval"
                                                     for m in metrics)
    commands = [" ".join(["PYTHONPATH=runtime", os.path.basename(sys.executable),
                          "runtime/tests/run_acceptance.py", *sys.argv[1:]])]
    report = {
        **report_header(arguments, commit=git("rev-parse", "HEAD"),
                        dirty=bool(git("status", "--porcelain", "--untracked-files=no"))),
        "environment": {
            "id": environment_id,
            "os": f"{platform.system()} {platform.mac_ver()[0]}",
            "arch": platform.machine(),
            "python": platform.python_version(),
            "sandbox_exec": sandbox_available,
        },
        "grail": grail_info,
        "gates": {"real_core": arguments.real_core, "live": arguments.live},
        "evidence_classes": {
            "unit": "fakes, fixtures and the real Seatbelt sandbox; no Grail process",
            "real-core": "the unchanged Grail process started by the cell, without inference",
            "live": "real Copilot inference through the installed brainstem's connection",
        },
        "interpreter": sys.executable,
        "commands": commands,
        "merged": merged_from,
        "seconds": round(time.monotonic() - started, 1),
        "totals": {"tests": len(records), "by_class": classes,
                   "passed": result.wasSuccessful() and all(
                       r["outcome"] in ("passed", "skipped") for r in records)},
        "criteria": criteria,
        "metrics": metrics,
        "tests": records,
    }
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    clean = redact(text, needles)
    report_sanitized = clean == text
    if not report_sanitized:
        clean = clean.replace('"schema"', '"redactions_applied": true,\n  "schema"', 1)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(clean)
    print(f"evidence: {arguments.output} tests={len(records)} ok={result.wasSuccessful()} "
          f"criteria_pass={sum(c['pass'] for c in criteria.values())}/{len(criteria)}",
          file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
