"""The interactive terminal (G1) and its terminal-side threats (T15, T16); unit tier.

Real REPL processes on real pipes and signals: in-process (``repl_support.py``, the real
``Repl`` with a scripted fake Grail) and through a real daemon process
(``companion_support.py serve``) with the real CLI ``brainstem-agent repl``.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from acceptance_support import criteria, private_dir, record_metric, remove_tree
from companion_support import ESCAPES, cell_env

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent
# Unicode's Bidi_Control property: ALM, LRM, RLM, LRE, RLE, PDF, LRO, RLO, LRI, RLI, FSI, PDI.
BIDI_CONTROLS = ("\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d",
                 "\u202e", "\u2066", "\u2067", "\u2068", "\u2069")


class PtyProcess:
    """A REPL on a real pseudo-terminal (readline active), its output read on a thread.

    Waits are for text to appear after a mark, never fixed sleeps, with wide deadlines so a
    loaded machine only makes them slower."""

    def __init__(self, arguments: list[str], env: dict) -> None:
        import pty

        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave,
                                        env={**env, "TERM": "xterm-256color"},
                                        start_new_session=True)
        os.close(slave)
        self._data = bytearray()
        self._lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._data += chunk

    def mark(self) -> int:
        with self._lock:
            return len(self._data)

    def text(self, since: int = 0, until: int | None = None) -> str:
        with self._lock:
            return bytes(self._data[since:until]).decode("utf-8", "replace")

    def wait_for(self, needle: str, since: int = 0, timeout: float = 60.0) -> int:
        """The offset just past the first ``needle`` after ``since`` (waiting for it)."""
        wanted = needle.encode()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                found = self._data.find(wanted, since)
            if found >= 0:
                return found + len(wanted)
            time.sleep(0.02)
        raise AssertionError(f"never saw {needle!r}; output: {self.text(since)[-800:]!r}")

    def turn(self, typed: str, since: int) -> tuple[int, int]:
        """Type a line at the prompt that ended at ``since``; wait for the turn's final line
        and the next prompt. Returns (end of the final line, end of the next prompt)."""
        self.type(typed)
        finished = self.wait_for("[succeeded", since)
        return finished, self.wait_for("you> ", finished)

    def type(self, data: str) -> None:
        os.write(self.master, data.encode())

    def close(self, timeout: float = 60.0) -> int:
        try:
            return self.process.wait(timeout)
        finally:
            os.close(self.master)


class Process:
    """A REPL process whose output is read in raw chunks (streamed text has no newline)."""

    def __init__(self, arguments: list[str], env: dict) -> None:
        self.process = subprocess.Popen(arguments, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.out: queue.Queue = queue.Queue()
        self.stdout_text, self.stderr_text = [], []
        self._partial = ""
        for stream, sink, is_stdout in ((self.process.stdout, self.stdout_text, True),
                                        (self.process.stderr, self.stderr_text, False)):
            threading.Thread(target=self._pump, args=(stream, sink, is_stdout),
                             daemon=True).start()

    def _pump(self, stream, sink, is_stdout) -> None:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                break
            text = decoder.decode(chunk)
            sink.append(text)
            if is_stdout:
                self._partial += text
                *lines, self._partial = self._partial.split("\n")
                for line in lines:
                    self.out.put(line)

    def send(self, line: str) -> None:
        self.process.stdin.write((line + "\n").encode())
        self.process.stdin.flush()

    def send_json(self, document: dict) -> None:
        self.send(json.dumps(document))

    def read_json(self, predicate, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.out.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            try:
                document = json.loads(line)
            except ValueError:
                continue
            if predicate(document):
                return document
        raise AssertionError("no matching line; stdout tail: " + "".join(self.stdout_text)[-800:]
                             + " stderr tail: " + "".join(self.stderr_text)[-800:])

    def wait_text(self, text: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in "".join(self.stdout_text) + "".join(self.stderr_text):
                return
            time.sleep(0.02)
        raise AssertionError(f"never saw {text!r}; stdout: {''.join(self.stdout_text)[-600:]}")

    def close(self, timeout: float = 30.0) -> int:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(10)


class ReplCase(unittest.TestCase):
    def setUp(self):
        self.root = private_dir(self)
        self.env = cell_env(self.root)
        self.home = Path(self.env["BRAINSTEM_AGENT_HOME"])
        self.home.mkdir(mode=0o700, exist_ok=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.env.update({"PYTHONPATH": f"{RUNTIME}:{HERE}", "PATH": "/usr/bin:/bin",
                         "BRAINSTEM_AGENT_WORKSPACE": str(self.workspace)})
        self.processes: list[Process] = []

    def tearDown(self):
        for item in self.processes:
            if item.process.poll() is None:
                item.process.kill()
                item.process.wait(10)

    def local(self, *extra: str) -> Process:
        process = Process([sys.executable, str(HERE / "repl_support.py"), *extra], self.env)
        self.processes.append(process)
        return process

    def cli_repl(self, *extra: str) -> Process:
        process = Process([sys.executable, "-m", "brainstem_agent", "repl", *extra], self.env)
        self.processes.append(process)
        return process

    def serve(self) -> subprocess.Popen:
        """A real daemon process (scripted streaming workers) for this home."""
        cell = subprocess.Popen([sys.executable, str(HERE / "companion_support.py"), "serve",
                                 str(self.root)], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        line = cell.stdout.readline()
        self.assertIn('"ready"', line, cell.stderr.read()[-500:] if not line else "")
        self.addCleanup(lambda: (cell.terminate(), cell.wait(30)))
        return cell


class Terminal(ReplCase):
    @criteria("G1")
    def test_g1_in_process_json_protocol_streams_and_runs_every_slash_command(self):
        repl = self.local("--json")
        ready = repl.read_json(lambda d: d.get("type") == "ready")
        self.assertEqual((ready["protocol"], ready["mode"]), ("brainstem-agent-repl/1",
                                                              "in-process"))
        for command in ("/help", "/new", "/sessions", "/resume", "/skills", "/memory",
                        "/schedules", "/inbox", "/status", "/stop", "/exit"):
            self.assertIn(command, ready["commands"])
        started = time.monotonic()
        repl.send_json({"op": "chat", "text": "[[say \"hello from the terminal\"]]"})
        first = repl.read_json(lambda d: d.get("type") == "delta")
        record_metric("g1_first_delta_seconds_in_process", round(time.monotonic() - started, 3))
        result = repl.read_json(lambda d: d.get("type") == "result")["result"]
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["response"]["response"], "hello from the terminal")
        self.assertTrue(first["text"])
        session = result["session_id"]
        repl.send("[[say \"second turn\"]]")  # plain lines are typed input
        second = repl.read_json(lambda d: d.get("type") == "result")["result"]
        self.assertEqual(second["session_id"], session)  # the session continues
        answers = {}
        for command in ("/sessions", "/history", "/skills", "/memory", "/schedules", "/inbox",
                        "/status", "/stop", "/help"):
            repl.send_json({"op": "command", "text": command})
            answers[command] = repl.read_json(lambda d: d.get("type") == "command")["result"]
        self.assertEqual(answers["/sessions"]["sessions"][0]["session_id"], session)
        self.assertEqual([turn["label"] for turn in answers["/history"]["turns"]],
                         ["succeeded", "succeeded"])
        self.assertEqual(answers["/status"]["mode"], "in-process")
        repl.send("/new")
        repl.read_json(lambda d: d.get("type") == "command")
        repl.send("[[say \"fresh\"]]")
        fresh = repl.read_json(lambda d: d.get("type") == "result")["result"]
        self.assertNotEqual(fresh["session_id"], session)
        repl.send(f"/resume {session}")
        resumed = repl.read_json(lambda d: d.get("type") == "command")["result"]
        self.assertEqual(len(resumed["turns"]), 2)
        repl.send("[[say \"third turn\"]]")
        third = repl.read_json(lambda d: d.get("type") == "result")["result"]
        self.assertEqual(third["session_id"], session)
        repl.send_json({"op": "exit"})
        self.assertEqual(repl.read_json(lambda d: d.get("type") == "bye")["session_id"], session)
        self.assertEqual(repl.close(), 0)

    @criteria("G1")
    def test_g1_ctrl_c_cancels_the_turn_and_keeps_the_session(self):
        for mode in ("in-process", "daemon"):
            with self.subTest(mode=mode):
                if mode == "daemon":
                    self.serve()
                    repl = self.cli_repl()
                else:
                    repl = self.local()
                repl.send("[[say \"before\"]]")
                repl.wait_text("[succeeded")
                repl.send("[[slow 30]]")
                repl.wait_text("tick2")
                started = time.monotonic()
                os.kill(repl.process.pid, signal.SIGINT)
                repl.wait_text("[cancelled", timeout=10)
                seconds = time.monotonic() - started
                self.assertLess(seconds, 5)
                record_metric(f"g1_ctrl_c_cancel_seconds_{mode}", round(seconds, 3))
                self.assertIsNone(repl.process.poll())  # still running
                repl.send("/history")
                repl.wait_text("[cancelled]")
                repl.send("[[say \"after the cancel\"]]")
                repl.wait_text("after the cancel")
                repl.wait_text("[succeeded", timeout=30)
                repl.send("/history")
                repl.wait_text('you: [[say "after the cancel"]]')
                history = "".join(repl.stdout_text)
                for typed in ('[[say "before"]]', "[[slow 30]]", '[[say "after the cancel"]]'):
                    self.assertIn(f"you: {typed}", history)  # one session: all three turns
                self.assertEqual(repl.close(), 0)
                if mode == "daemon":
                    self.assertIn("(through the daemon", "".join(repl.stdout_text))

    @criteria("G1", "G5")
    def test_g1_the_sessions_command_shows_a_partial_last_turn_as_partial(self):
        from brainstem_agent import daemon

        self.serve()
        client = daemon.connect(self.home)
        three = " ".join(['[[list_files {"path": "."}]]'] * 3)
        started = client.call("POST", "/v1/requests",
                              {"message": three, "budget": {"max_segments": 1}})
        finished = [event for event in client.stream(
            f"/v1/requests/{started['request_id']}/events") if event["event"] ==
            "request.finished"]
        self.assertEqual(finished[0]["result"]["state"], "partial")
        repl = self.cli_repl()
        repl.send("/sessions")
        repl.wait_text("last partial")
        self.assertEqual(repl.close(), 0)
        self.assertNotIn("last failed", "".join(repl.stdout_text))

    @criteria("G1")
    def test_g1_through_the_daemon_the_json_protocol_cancels_and_streams(self):
        self.serve()
        repl = self.cli_repl("--json")
        ready = repl.read_json(lambda d: d.get("type") == "ready")
        self.assertEqual(ready["mode"], "daemon")
        repl.send_json({"op": "chat", "text": "[[slow 30]]"})
        repl.read_json(lambda d: d.get("type") == "delta")
        started = time.monotonic()
        repl.send_json({"op": "cancel"})
        result = repl.read_json(lambda d: d.get("type") == "result", timeout=10)["result"]
        self.assertEqual(result["state"], "cancelled")
        self.assertLess(time.monotonic() - started, 5)
        repl.send_json({"op": "chat", "text": "[[say \"still here\"]]"})
        again = repl.read_json(lambda d: d.get("type") == "result")["result"]
        self.assertEqual((again["state"], again["session_id"]), ("succeeded",
                                                                 result["session_id"]))
        repl.send_json({"op": "exit"})
        self.assertEqual(repl.close(), 0)

    @criteria("G1")
    def test_g1_history_is_persisted_owner_only_and_skips_credentials(self):
        repl = self.local()
        repl.send("[[say \"one\"]]")
        repl.wait_text("[succeeded")
        secret = "ghp_" + "A1b2C3d4" * 5
        repl.send(f"my token is {secret} [[say \"noted\"]]")
        repl.wait_text("noted")
        repl.send("/skills")
        self.assertEqual(repl.close(), 0)
        path = self.home / "state" / "repl_history"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        lines = path.read_text().splitlines()
        self.assertEqual(lines, ['[[say "one"]]', "/skills"])
        self.assertNotIn(secret, path.read_text())
        # A later session reads it back (readline, when a terminal is attached) and appends.
        again = self.local()
        again.send("/help")
        self.assertEqual(again.close(), 0)
        self.assertEqual(path.read_text().splitlines()[-1], "/help")

    @criteria("G1", "G4")
    def test_t16_a_typed_credential_is_neither_saved_nor_recalled_in_a_real_terminal(self):
        """On a real terminal (readline), a credential-shaped line is kept out of the history
        file and out of the session's own history: the up arrow recalls the line typed before
        it, and a credential-shaped line found in an old history file is never loaded."""
        state = self.home / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        old_secret = "ghp_" + "Z9y8X7w6" * 5
        (state / "repl_history").write_text(f'[[say "from before"]]\nold {old_secret}\n')
        (state / "repl_history").chmod(0o600)
        terminal = PtyProcess([sys.executable, str(HERE / "repl_support.py")], self.env)
        self.addCleanup(lambda: terminal.process.poll() is None and terminal.process.kill())
        prompt = terminal.wait_for("you> ")
        # The up arrow recalls the newest loaded line: never the credential-shaped one.
        finished, next_prompt = terminal.turn("\x1b[A\r", prompt)
        self.assertIn("from before", terminal.text(prompt, finished))
        self.assertNotIn(old_secret, terminal.text(prompt))
        _finished, prompt = terminal.turn('[[say "safe one"]]\r', next_prompt)
        secret = "ghp_" + "A1b2C3d4" * 5
        finished, next_prompt = terminal.turn(f'my token is {secret} [[say "noted"]]\r', prompt)
        self.assertIn("not kept in history", terminal.text(prompt, finished))
        # The up arrow skips it: the line before comes back, the credential never does.
        finished, prompt = terminal.turn("\x1b[A\r", next_prompt)
        recalled = terminal.text(next_prompt)
        self.assertIn("safe one", recalled)
        self.assertNotIn(secret, recalled)
        self.assertNotIn("noted", terminal.text(next_prompt, finished))
        terminal.type("\x04")  # Ctrl-D quits
        self.assertEqual(terminal.close(), 0)
        saved = (state / "repl_history").read_text()
        self.assertNotIn(secret, saved)
        self.assertEqual(saved.splitlines()[2:], ['[[say "from before"]]', '[[say "safe one"]]',
                                                  '[[say "safe one"]]'])

    @criteria("G1", "G4")
    def test_t16_history_is_never_written_through_a_symlink(self):
        (self.home / "state").mkdir(mode=0o700, exist_ok=True)
        target = self.root / "elsewhere.txt"
        target.write_text("untouched\n")
        (self.home / "state" / "repl_history").symlink_to(target)
        repl = self.local()
        repl.send("[[say \"hi\"]]")
        repl.wait_text("[succeeded")
        self.assertEqual(repl.close(), 0)
        self.assertEqual(target.read_text(), "untouched\n")

    @criteria("G1", "G4")
    def test_t15_every_bidi_control_is_inert_wherever_the_terminal_prints(self):
        """Every Unicode Bidi_Control character (marks, embeddings, overrides, isolates) is
        made visible and never reaches the terminal raw: in streamed text, the final answer,
        memory, the sessions list and a resumed transcript."""
        repl = self.local()
        spoof = "".join(BIDI_CONTROLS)
        remembered = json.dumps({"text": f"fact {spoof}tail", "scope": "workspace"})
        repl.send(f"asked {spoof}here [[delta {json.dumps('streamed ' + spoof)}]] "
                  f"[[remember {remembered}]] [[answer {json.dumps('final ' + spoof)}]]")
        repl.wait_text("[succeeded")
        for command in ("/memory", "/sessions", "/history"):
            repl.send(command)
        repl.wait_text("you: asked")
        repl.send("/exit")
        self.assertEqual(repl.close(), 0)
        printed = "".join(repl.stdout_text) + "".join(repl.stderr_text)
        for raw in BIDI_CONTROLS:
            self.assertNotIn(raw, printed, f"U+{ord(raw):04X} reached the terminal raw")
        visible = "".join(f"\\u{ord(raw):04x}" for raw in BIDI_CONTROLS)
        for shown in (f"streamed {visible}", f"final {visible}", f"fact {visible}tail",
                      f"asked {visible}here"):
            self.assertIn(shown, printed)

    @criteria("G1", "G4")
    def test_t15_untrusted_text_cannot_drive_the_terminal(self):
        repl = self.local()
        evil = ESCAPES + "\u202eevil\u2066"
        repl.send(f"[[delta {json.dumps(evil)}]] [[answer {json.dumps('final ' + evil)}]]")
        repl.wait_text("[succeeded")
        self.assertEqual(repl.close(), 0)
        printed = "".join(repl.stdout_text) + "".join(repl.stderr_text)
        for raw in ("\x1b", "\x07", "\u202e", "\u2066"):
            self.assertNotIn(raw, printed)
        self.assertIn("\\x1b]52;c;cHduZWQ=\\x07", printed)  # visible, inert
        self.assertIn("\\u202eevil", printed)

    @criteria("G1")
    def test_g1_bare_command_opens_the_terminal(self):
        process = Process([sys.executable, "-m", "brainstem_agent"], self.env)
        self.processes.append(process)
        process.send("/help")
        process.send("/exit")
        self.assertEqual(process.close(), 0)
        self.assertIn("/resume", "".join(process.stdout_text))
        self.assertIn("Brainstem Agent (in-process)", "".join(process.stdout_text))


if __name__ == "__main__":
    unittest.main()
