"""Companion parity, size and dependency specs (G3, G7, G10; unit tier).

G3: every route the companion's code can call is a documented daemon route marked for the
companion, and the owner's CLI reaches each one (``brainstem-agent api``) with the same
answer the companion gets; the dedicated CLI views (``sessions``, ``skills``, ``memory``,
``schedules``, ``inbox``) return the same data as the routes. G7: assets are small and
self-contained. G10: the new modules import only the standard library, and the companion ships
as package data, apart from the public site.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

from acceptance_support import criteria, private_dir, record_metric, remove_tree, run_cli
from companion_support import Harness, seed_hostile

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parent
UI = RUNTIME / "brainstem_agent" / "ui"
NEW_MODULES = ("api", "companion", "surface", "streaming", "views", "repl")
VOLATILE = ("workers", "last_errors", "companion", "active_turn", "health", "processes",
            "scheduler", "mcp", "signed_in")
# Measurements that move between two reads of the same state (uptimes, event clocks and
# counters that tick, free disk space): their values are masked, the keys stay, so structure,
# ids, states and every other value still compare.
MEASURED = frozenset({"uptime_seconds", "t", "seq", "free_mb", "total_mb"})
MEASURED_TEXT = re.compile(r"\d+(?:\.\d+)? MiB (?=free\b|are free\b)")


def companion_routes() -> dict[str, tuple[str, str]]:
    source = (UI / "app.js").read_text()
    block = source[source.index("const ROUTES = {"):source.index("};", source.index("const ROUTES"))]
    return {name: (method, path) for name, method, path in
            re.findall(r"(\w+):\s*\['(GET|POST)',\s*'([^']+)'\]", block)}


def calls_in(source: str) -> set[str]:
    """Every ROUTES key the page's code actually uses."""
    return set(re.findall(r"api\('(\w+)'", source)) | set(re.findall(r"ROUTES\.(\w+)", source))


def readiness_shape(report: dict) -> dict:
    return {"live": report["live"], "checks": [(check["id"], check["kind"], check["required"])
                                              for check in report["checks"]]}


def stable(document):
    if isinstance(document, dict):
        return {key: "<measured>" if key in MEASURED else stable(value)
                for key, value in document.items() if key not in VOLATILE}
    if isinstance(document, list):
        return [stable(item) for item in document]
    if isinstance(document, str):  # "97735 MiB free (floor 512 MiB)": the floor still compares
        return MEASURED_TEXT.sub("<measured> MiB ", document)
    return document


class RouteParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = private_dir(prefix="ba-parity-")
        cls.harness = Harness(cls.root)
        seed_hostile(cls.harness)
        cls.env = {**cls.harness.env, "PYTHONPATH": str(RUNTIME), "PATH": "/usr/bin:/bin",
                   "BRAINSTEM_AGENT_WORKSPACE": str(cls.harness.workspace)}

    @classmethod
    def tearDownClass(cls):
        cls.harness.stop()
        remove_tree(cls.root)

    def cli(self, *arguments):
        result = run_cli(list(arguments), self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        return json.loads(result.stdout)

    @criteria("G3")
    def test_g3_every_companion_route_is_documented_and_used_routes_exist(self):
        from brainstem_agent import api

        routes = companion_routes()
        self.assertGreaterEqual(len(routes), 20)
        documented = {(route.method, route.pattern): route for route in api.ROUTES}
        for name, key in routes.items():
            self.assertIn(key, documented, name)
            self.assertTrue(documented[key].companion, f"{name} is not a companion route")
            self.assertTrue(documented[key].cli, name)
        source = (UI / "app.js").read_text()
        self.assertEqual(calls_in(source) - set(routes), set())  # nothing outside the table
        self.assertEqual(set(re.findall(r"fetch\(\s*url\(ROUTES\.(\w+)", source)), {"events"})
        self.assertEqual(len(re.findall(r"\bfetch\(", source)), 2)  # api() and the stream
        login = (UI / "login.js").read_text()
        self.assertEqual(re.findall(r"fetch\('([^']+)'", login), ["/v1/companion/session"])
        # The table the daemon serves is the one in the code, and the README lists it all.
        served = self.harness.bearer("GET", "/v1/api").json()["routes"]
        self.assertEqual([(item["method"], item["path"]) for item in served],
                         [(route.method, route.pattern) for route in api.ROUTES])
        readme = (RUNTIME / "README.md").read_text()
        for route in api.ROUTES:
            self.assertIn(f"`{route.method} {route.pattern}`", readme, route.name)

    @criteria("G3")
    def test_g3_the_cli_reaches_every_companion_route_with_the_same_answer(self):
        h = self.harness
        session = h.login()
        store, namespace = h.cell.host.store, h.cell.host.namespace
        some_session = store.list_sessions(namespace)[0]["session_id"]
        turn = store.session_turns(namespace, some_session)[0]["turn_id"]
        request = session.call("POST", "/v1/requests", {"message": "[[say \"parity\"]]"}).json()
        session.events(request["request_id"])
        params = {"session_id": some_session, "turn_id": turn, "name": "hostile-skill",
                  "request_id": request["request_id"]}
        checked = []
        for name, (method, pattern) in companion_routes().items():
            if method != "GET" or name == "events":
                continue
            path = re.sub(r"\{(\w+)\}", lambda match: params[match.group(1)], pattern)
            browser = session.call("GET", path)
            self.assertEqual(browser.status, 200, (name, browser.body[:200]))
            owner = self.cli("api", "GET", path)
            if name == "health":  # free space and the warm-up move between two calls
                owner, seen = readiness_shape(owner), readiness_shape(browser.json())
                self.assertEqual(owner, seen, name)
                self.assertEqual(readiness_shape(self.cli("status", "--json")["readiness"]),
                                 seen, "status --json carries the same readiness")
            else:
                self.assertEqual(stable(owner), stable(browser.json()), name)
            checked.append(name)
        events = run_cli(["api", "GET", f"/v1/requests/{request['request_id']}/events"],
                         self.env, timeout=60)
        streamed = [json.loads(line) for line in events.stdout.splitlines()]
        self.assertEqual(streamed[-1]["event"], "request.finished")
        self.assertEqual(stable(streamed), stable(session.events(request["request_id"])))
        checked.append("events")
        # State-changing companion routes, each reached by the CLI too (fresh targets).
        for text in ("fact one", "fact two"):
            h.bearer("POST", "/v1/turn", {"message": "remember [[remember " + json.dumps(
                {"text": text, "scope": "workspace"}) + "]]"})
        facts = {fact["text"]: fact["fact_id"] for fact in
                 session.call("GET", "/v1/memory").json()["facts"]}
        self.assertEqual(session.call("POST", "/v1/memory/edit", {
            "scope": "workspace", "fact_id": facts["fact one"], "text": "fact one, edited"})
            .status, 200)
        self.assertEqual(self.cli("memory", "edit", facts["fact two"], "--scope", "workspace",
                                  "--text", "fact two, edited", "--json")["fact"]["text"],
                         "fact two, edited")
        self.assertEqual(session.call("POST", "/v1/memory/forget", {
            "scope": "workspace", "fact_id": facts["fact one"]}).status, 200)
        self.assertTrue(self.cli("memory", "forget", facts["fact two"], "--scope",
                                 "workspace", "--json")["ok"])
        left = {fact["text"] for fact in self.cli("memory", "--json")["facts"]}
        self.assertFalse(left & {"fact one", "fact one, edited", "fact two, edited"})
        schedule = self.cli("schedules", "create", "--prompt", "[[say \"x\"]]", "--in", "3600",
                            "--name", "parity", "--json")["schedule"]["schedule_id"]
        self.assertEqual(session.call("POST", f"/v1/schedules/{schedule}/pause").json()
                         ["schedule"]["state"], "paused")
        self.assertEqual(self.cli("schedules", "resume", schedule, "--json")["schedule"]["state"],
                         "active")
        self.assertEqual(self.cli("api", "POST", f"/v1/schedules/{schedule}/remove")["schedule"]
                         ["state"], "removed")
        self.assertEqual(session.call("POST", "/v1/skills/hostile-skill/disable").json()
                         ["skill"]["state"], "disabled")
        self.assertEqual(self.cli("skills", "enable", "hostile-skill", "--json")["skill"]
                         ["state"], "active")
        approved = session.call("POST", "/v1/skills/hostile-skill/approve", {"version": 1})
        self.assertEqual(approved.json()["skill"]["review"], "approved")
        self.assertEqual(self.cli("skills", "show", "hostile-skill", "--json")["skill"]["review"],
                         "approved")
        slow = session.call("POST", "/v1/requests", {"message": "[[slow 10]]"}).json()
        self.assertTrue(self.cli("api", "POST", "/v1/cancel", "--body", json.dumps(
            {"request_id": slow["request_id"]}))["cancelled"])
        self.assertEqual(session.events(slow["request_id"])[-1]["result"]["state"], "cancelled")
        other = h.login()
        self.assertEqual(other.call("POST", "/v1/companion/logout").status, 200)
        self.assertGreaterEqual(self.cli("open", "--sign-out-all", "--json")["revoked"], 1)
        self.assertEqual(session.call("GET", "/v1/status").status, 401)
        posts = {"start", "cancel", "memoryEdit", "memoryForget", "scheduleChange",
                 "skillReview", "logout"}
        self.assertEqual(set(checked) | posts, set(companion_routes()))  # every one of them
        record_metric("g3_routes_checked", sorted(set(checked) | posts))

    @criteria("G3")
    def test_g3_the_dedicated_cli_views_show_what_the_companion_shows(self):
        session = self.harness.login()
        some = self.harness.cell.host.store.list_sessions(self.harness.cell.host.namespace)[0]
        pairs = [(("sessions", "--json"), "/v1/sessions"),
                 (("sessions", "show", some["session_id"], "--json"),
                  f"/v1/sessions/{some['session_id']}"),
                 (("skills", "list", "--json"), "/v1/skills"),
                 (("skills", "show", "hostile-skill", "--json"), "/v1/skills/hostile-skill"),
                 (("memory", "--json"), "/v1/memory"),
                 (("schedules", "list", "--json"), "/v1/schedules"),
                 (("inbox", "--json"), "/v1/inbox"),
                 (("receipts", "--json"), "/v1/receipts")]
        for command, path in pairs:
            owner = self.cli(*command)
            browser = session.call("GET", path).json()
            for key in browser:
                if key in ("history", "markdown"):
                    continue
                self.assertEqual(stable(owner[key]), stable(browser[key]), (command, key))


class McpMirror(unittest.TestCase):
    @criteria("G3", "G4")
    def test_g3_mcp_servers_and_their_tools_are_mirrored_and_results_stay_data(self):
        from brainstem_agent import sandbox
        from companion_support import HOSTILE
        from reach_support import notes_server

        if not sandbox.available():
            self.skipTest("Seatbelt sandbox-exec is unavailable")
        root = private_dir(self)
        harness = Harness(root)
        try:
            store = root / "notes-store"
            store.mkdir(mode=0o700)
            (harness.home / "reach.json").write_text(json.dumps(
                {"mcpServers": {"notes": notes_server(store)}}))
            session = harness.login()
            before = session.call("GET", "/v1/mcp").json()
            self.assertEqual([(item["server"], item["state"]) for item in before["servers"]],
                             [("notes", "not started")])
            for name, arguments in (("mcp__notes__note_put", {"key": "k", "text": HOSTILE}),
                                    ("mcp__notes__note_get", {"key": "k"})):
                answer = harness.bearer("POST", "/v1/tool", {
                    "name": name, "arguments": arguments, "capabilities": ["mcp.notes"]})
                self.assertTrue(answer.json()["ok"], answer.body[:300])
            after = session.call("GET", "/v1/mcp")
            [server] = after.json()["servers"]
            self.assertEqual((server["state"], server["transport"]), ("ready", "stdio"))
            self.assertIn("mcp__notes__note_get", server["tools"])
            self.assertNotIn(str(store).encode(), after.body)  # no commands, paths or env
            env = {**harness.env, "PYTHONPATH": str(RUNTIME), "PATH": "/usr/bin:/bin"}
            owner = json.loads(run_cli(["api", "GET", "/v1/mcp"], env).stdout)
            self.assertEqual(stable(owner), stable(after.json()))
            tools = session.call("GET", "/v1/tools").json()["tools"]
            self.assertIn(("mcp__notes__note_get", "mcp.notes", True),
                          [(item["name"], item["capability"], item["chat_turn"]) for item in tools])
            receipts = session.call("GET", "/v1/receipts")
            self.assertNotIn(b"<", receipts.body)  # hostile MCP arguments: escaped JSON
            kept = {item["tool"]: item for item in receipts.json()["receipts"]}
            self.assertIn("window.__pwned", kept["mcp__notes__note_put"]["request"]["text"])
            # The result itself is kept as length and digest, never as text to render.
            self.assertEqual(set(kept["mcp__notes__note_get"]["result"]) & {"content", "text"},
                             set())
            self.assertIn("content_sha256", kept["mcp__notes__note_get"]["result"])
            # Changed or new tool definitions (possible tool poisoning) are withheld until the
            # owner trusts the server: the mirror says which and why, as `mcp status` does.
            (harness.home / "reach.json").write_text(json.dumps({"mcpServers": {
                "notes": notes_server(store, fixture_args=["--variant", "poisoned"])}}))
            harness.cell.host.mcp_organ.prepare(["mcp.notes"])
            [changed] = session.call("GET", "/v1/mcp").json()["servers"]
            self.assertEqual(sorted(changed["withheld"]), ["note_export", "note_get"])
            self.assertIn("changed", changed["withheld"]["note_get"])
            self.assertNotIn("mcp__notes__note_get", changed["tools"])
            owner = json.loads(run_cli(["api", "GET", "/v1/mcp"], env).stdout)
            self.assertEqual(owner["servers"][0]["withheld"], changed["withheld"])
            self.assertIn("mcp trust", (UI / "app.js").read_text())
        finally:
            harness.stop()


class AssetsAndDependencies(unittest.TestCase):
    @criteria("G7", "G10")
    def test_g7_assets_are_small_self_contained_and_need_no_build(self):
        files = sorted(path for path in UI.iterdir() if path.is_file())
        names = {path.name for path in files}
        self.assertEqual(names, {"index.html", "login.html", "app.js", "login.js", "app.css",
                                 "icon.svg"})
        total = sum(path.stat().st_size for path in files)
        self.assertLessEqual(total, 150 * 1024)
        record_metric("g7_asset_bytes", total)
        for path in files:
            text = path.read_text()
            self.assertNotRegex(text, r"(?i)(https?:)?//(?!www\.w3\.org/2000/svg)[a-z0-9.-]+\.[a-z]")
            self.assertNotIn("sourceMappingURL", text)
        from brainstem_agent.companion import STATIC
        self.assertEqual({name for name, _kind in STATIC.values()}, names)
        pyproject = (RUNTIME / "pyproject.toml").read_text()
        self.assertIn('"ui/*"', pyproject)
        self.assertIn("dependencies = []", pyproject)

    @criteria("G10")
    def test_g10_new_modules_import_only_the_standard_library(self):
        for name in NEW_MODULES:
            tree = ast.parse((RUNTIME / "brainstem_agent" / f"{name}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names, name)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertIn(node.module.split(".")[0], sys.stdlib_module_names, name)

    @criteria("G10")
    def test_g10_the_companion_ships_as_package_data_apart_from_the_public_site(self):
        """The companion's assets are package data under runtime/brainstem_agent (pyproject
        ships ``ui/*``); no public site file (``index.html``, ``assets/``) is or loads one of
        them; and the Pages workflow still stages only ``index.html`` and ``assets/``."""
        import hashlib

        from brainstem_agent.companion import STATIC

        names = sorted({name for name, _kind in STATIC.values()})
        self.assertIn('"ui/*"', (RUNTIME / "pyproject.toml").read_text())
        for name in names:
            self.assertTrue((UI / name).is_file(), name)
        digests = {hashlib.sha256((UI / name).read_bytes()).hexdigest(): name for name in names}
        site = [REPO / "index.html", *sorted(path for path in (REPO / "assets").rglob("*")
                                             if path.is_file())]
        urls = [url for url in STATIC if url.startswith("/ui/")] + ["/v1/companion"]
        for path in site:
            data = path.read_bytes()
            self.assertNotIn(hashlib.sha256(data).hexdigest(), digests, path.name)
            if path.suffix in (".html", ".css", ".js", ".svg", ".json"):
                text = data.decode("utf-8", "replace")
                for url in urls:
                    self.assertNotIn(url, text, f"{path.name} loads the companion's {url}")
        workflow = (REPO / ".github" / "workflows" / "pages.yml").read_text()
        copies = re.findall(r"^\s*cp\s+(?:-\w+\s+)*(\S+)\s+(\S+)\s*$", workflow, re.M)
        self.assertEqual(sorted(source for source, _target in copies), ["assets", "index.html"])
        self.assertTrue(all(target.startswith("_site") for _source, target in copies), copies)
        for line in workflow.splitlines():
            if "_site" in line:  # made, filled by the copies above, uploaded; nothing else
                self.assertRegex(line.strip(), r"^(mkdir _site|cp .+ _site\S*|path: _site)$")
        self.assertRegex(workflow, r"upload-pages-artifact@\S+[^\n]*\n\s+with:\n\s+path: _site\n")


def shell_lines(path: Path) -> list[str]:
    """Every command line of a document's ``sh`` blocks (continuations joined)."""
    lines = []
    for block in re.findall(r"```sh\n(.*?)```", path.read_text(), re.S):
        lines += [line for line in block.replace("\\\n", " ").splitlines() if line.strip()]
    return lines


def cli_argv(line: str) -> list[str] | None:
    """The brainstem-agent arguments of one command line (angle-bracket placeholders filled
    in), or None when the line runs something else."""
    import shlex

    words = shlex.split(re.sub(r"<([A-Za-z_]+)>", r"\1", line))
    while words and re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S*", words[0]):  # VAR=value prefixes
        words = words[1:]
    if words and (words[0] == "brainstem-agent" or words[0].endswith("/brainstem-agent")):
        return words[1:]
    if words[:1] and words[0].startswith("python") and words[1:3] == ["-m", "brainstem_agent"]:
        return words[3:]
    return None


class Documentation(unittest.TestCase):
    """The docs say what the code does: the README's route table is the code's, every route's
    CLI line and every documented command is a real command, and command blocks are safe to
    paste into zsh (no inline # notes, no a|b alternatives, no [--opt] forms; placeholders
    are <name>, which a shell refuses instead of running)."""

    @criteria("G3")
    def test_g3_the_readme_route_table_is_the_code_table(self):
        from brainstem_agent import api

        self.assertIn(api.markdown_table(), (RUNTIME / "README.md").read_text())

    @criteria("G3", "G12")
    def test_g3_every_documented_command_is_real_and_paste_safe(self):
        from brainstem_agent import api
        from brainstem_agent.cli import build_parser

        commands = [f"brainstem-agent {route.cli}" for route in api.ROUTES]
        for document in (REPO / "README.md", RUNTIME / "README.md"):
            for line in shell_lines(document):
                self.assertNotIn("#", line, (document.name, line))
                self.assertIsNone(re.search(r"\[-", line), (document.name, line))
                self.assertIsNone(re.search(r"[\w/-]\|[\w/-]", line), (document.name, line))
                commands += [part for part in re.split(r"&&|;", line)]
        parsed = 0
        for command in commands:
            argv = cli_argv(command)
            if argv is None or argv[:1] == ["fixture"]:  # fixture: the source form's own
                continue
            with self.subTest(command=command):
                self.assertNotRegex(command, r"[|\[#]")
                try:
                    build_parser().parse_args(argv)
                except SystemExit:
                    self.fail(f"not a real command: {command}")
                parsed += 1
        self.assertGreater(parsed, 60)
        record_metric("g12_documented_commands_parsed", parsed)


class Evidence(unittest.TestCase):
    @criteria("G11")
    def test_g11_every_earlier_milestones_suite_is_still_part_of_discovery(self):
        import importlib

        import run_acceptance
        earlier = (run_acceptance.ALWAYS_ON + run_acceptance.LEARNING
                   + run_acceptance.LONG_HORIZON + run_acceptance.REACHING)
        for name in (*run_acceptance.PREEXISTING, *earlier, *run_acceptance.COMPANION):
            importlib.import_module(name)
        self.assertEqual(set(run_acceptance.COMPANION) & set(earlier), set())

    @criteria("G12")
    def test_g12_the_evidence_runner_traces_every_g_criterion(self):
        import run_acceptance
        for number in range(1, 13):
            self.assertIn(f"G{number}", run_acceptance.CRITERIA)
        self.assertEqual(run_acceptance.CRITERIA["G8"][1], "live")
        self.assertEqual(run_acceptance.CRITERIA["G9"][1], "live")
        self.assertEqual(run_acceptance.evidence_class("test_live_companion"), "live")
        self.assertEqual(run_acceptance.evidence_class("test_companion_browser"), "unit")


if __name__ == "__main__":
    unittest.main()
