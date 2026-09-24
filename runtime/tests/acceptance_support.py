"""Shared support for the Cell v1 acceptance suite.

Every acceptance test is tagged with the criteria (A1-A11) it proves via
``@criteria``. Evidence class comes from the module: ``test_real_*`` modules are
``real-core`` (unchanged Grail process, no inference), ``test_live*`` modules are
``live`` (real Copilot inference) and everything else is ``unit``.

Gates: real-core tests skip unless ``BRAINSTEM_AGENT_REAL_CORE=1``; live tests
skip unless ``BRAINSTEM_AGENT_LIVE=1``. Both also skip, with the reason, when no
verified Grail seed is available (see ``find_seed``). Nothing here prints secrets:
secret scans report locations and labels, never the matched value.
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parent
REAL_CORE = os.environ.get("BRAINSTEM_AGENT_REAL_CORE") == "1"
LIVE = os.environ.get("BRAINSTEM_AGENT_LIVE") == "1"
PINNED_COMMIT = "49db80c8c6b6caa7647369beaf477d374a8f293c"
KERNEL_SHA256 = "bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4"


def _cached_source(cache: str | os.PathLike | None) -> Path | None:
    """The verified source a prepared cell cache holds (``<cache>/grail/<commit>/...``)."""
    if not cache:
        return None
    root = Path(cache) / "grail" / PINNED_COMMIT / "rapp_brainstem"
    return root if (root / "brainstem.py").is_file() else None


def find_seed(environ=os.environ) -> Path | None:
    """A verified Grail seed for tests that copy the pinned source, or None.

    ``BRAINSTEM_AGENT_GRAIL_SEED`` names one explicitly (a ``rapp_brainstem`` directory; it
    is checked against the pinned inventory when used). Otherwise the cell's own verified
    cache, as ``setup`` creates it, is used read-only: ``BRAINSTEM_AGENT_TEST_CACHE``,
    ``BRAINSTEM_AGENT_CACHE``, ``$BRAINSTEM_AGENT_HOME/cache``, then
    ``~/.brainstem-agent/cache``.
    """
    configured = environ.get("BRAINSTEM_AGENT_GRAIL_SEED")
    if configured:
        return Path(configured)
    home = environ.get("BRAINSTEM_AGENT_HOME") or os.path.expanduser("~/.brainstem-agent")
    for cache in (environ.get("BRAINSTEM_AGENT_TEST_CACHE"), environ.get("BRAINSTEM_AGENT_CACHE"),
                  os.path.join(home, "cache")):
        found = _cached_source(cache)
        if found is not None:
            return found
    return None


SEED = find_seed()
HAVE_SEED = SEED is not None and (SEED / "brainstem.py").is_file()
NO_SEED = (f"BRAINSTEM_AGENT_GRAIL_SEED ({SEED}) holds no brainstem.py" if SEED is not None and
           not HAVE_SEED else
           "no verified Grail seed: set BRAINSTEM_AGENT_GRAIL_SEED to a verified rapp_brainstem "
           "directory, or run `brainstem-agent setup` first")
OWNER_HOME = Path(os.path.expanduser("~")).resolve()
INSTALLED_CREDENTIAL = OWNER_HOME / ".brainstem" / "src" / "rapp_brainstem" / ".copilot_token"
CELL_TOOLS = frozenset({
    "write_file", "read_file", "list_files", "remember", "recall", "forget", "run_command",
})
# Unique per test process so a leak of the fake credential is unambiguous.
CANARY_TOKEN = "ghu_" + "Canary" + secrets.token_hex(15)


def criteria(*ids: str):
    """Tag a test with the acceptance criteria it proves (read by run_acceptance):
    A1-A11 (Cell v1), B1-B12 (always-on cell), C1-C12 (learning cell), D1-D12
    (long-horizon cell), E1-E12 (reaching cell), G1-G12 (companion surfaces) and H1-H12
    (operable cell)."""
    known = ({f"A{number}" for number in range(1, 12)} | {f"B{number}" for number in range(1, 13)}
             | {f"C{number}" for number in range(1, 13)}
             | {f"D{number}" for number in range(1, 13)}
             | {f"E{number}" for number in range(1, 13)}
             | {f"G{number}" for number in range(1, 13)}
             | {f"H{number}" for number in range(1, 13)})
    for item in ids:
        if item not in known:
            raise ValueError(f"unknown criterion {item}")

    def mark(function):
        function.criteria = tuple(ids)
        return function

    return mark


def stop_daemons_in(path: Path, *, wait: float = 15.0) -> list[int]:
    """Stop the daemon of a home at ``path`` or one level below it (by the pid in its private
    record, SIGTERM then SIGKILL), so no test daemon outlives the home it serves."""
    from brainstem_agent import daemon

    path, stopped = Path(path), []
    try:
        homes = [path, *(item for item in path.iterdir() if item.is_dir())]
    except OSError:
        return stopped
    for home in homes:
        record = daemon.read_record(home)
        if record is None:
            continue
        pid = int(record["pid"])
        try:
            os.kill(pid, 15)
        except OSError:
            continue
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and pid_running(pid):
            time.sleep(0.05)
        if pid_running(pid):
            kill_quietly(pid, group=False)
        stopped.append(pid)
    return stopped


def remove_tree(path: Path) -> None:
    """Remove a test tree even when it holds read-only cache files or dirs; a daemon still
    serving a home in it is stopped first."""
    path = Path(path)
    if not path.exists():
        return
    stop_daemons_in(path)
    for root, directories, _files in os.walk(path):
        for name in directories:
            try:
                os.chmod(os.path.join(root, name), 0o700, follow_symlinks=False)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def private_dir(test=None, prefix: str = "ba-test-") -> Path:
    """A resolved (no /var symlink) owner-only temporary directory."""
    path = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    os.chmod(path, 0o700)
    if test is not None:
        test.addCleanup(remove_tree, path)
    return path


def write_token_file(directory: Path, value: str = CANARY_TOKEN, *, name: str = "token.json",
                     as_json: bool = True, mode: int = 0o600) -> Path:
    path = Path(directory) / name
    path.write_text(json.dumps({"access_token": value}) if as_json else value + "\n")
    os.chmod(path, mode)
    return path


def installed_brainstem_home(directory: Path, value: str = CANARY_TOKEN) -> Path:
    """Build a fake installed-brainstem layout: <home>/src/rapp_brainstem/.copilot_token."""
    home = Path(directory) / "brainstem-home"
    token_dir = home / "src" / "rapp_brainstem"
    token_dir.mkdir(parents=True, mode=0o700)
    write_token_file(token_dir, value, name=".copilot_token")
    return home


def base_env(home: Path, **extra: str) -> dict[str, str]:
    """Minimal CLI environment. Callers opt in to credentials explicitly."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(RUNTIME),
        "HOME": str(OWNER_HOME),
        "LANG": "en_US.UTF-8",
        "BRAINSTEM_AGENT_HOME": str(home),
    }
    env.update({key: str(value) for key, value in extra.items()})
    return env


def isolated_env(home: Path, scratch: Path, **extra: str) -> dict[str, str]:
    """CLI environment that can never see the owner's installed credential."""
    empty = Path(scratch) / "no-brainstem"
    empty.mkdir(exist_ok=True, mode=0o700)
    return base_env(home, BRAINSTEM_HOME=str(empty), **extra)


def run_cli(arguments, env, *, timeout: float = 120, python: str | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [python or sys.executable, "-m", "brainstem_agent", *arguments],
        capture_output=True, text=True, timeout=timeout, env=env,
        cwd=str(cwd) if cwd else None,
    )


def cli_json(result: subprocess.CompletedProcess) -> dict:
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise AssertionError(
            f"CLI did not print JSON (exit {result.returncode}); stderr tail: "
            f"{redact(result.stderr[-400:])}"
        ) from error
    if not isinstance(value, dict):
        raise AssertionError("CLI JSON output must be an object")
    return value


_TOKEN_PATTERNS = ("ghu_", "gho_", "ghp_", "ghs_", "ghr_", "github_pat_")


def redact(text: str) -> str:
    """Redact anything shaped like a GitHub token from diagnostic text."""
    import re

    text = text.replace(CANARY_TOKEN, "[CANARY]")
    return re.sub(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]{6,}", r"\1[REDACTED]", text)


def leaks(needles: dict[str, str], *, roots=(), texts=()) -> list[str]:
    """Return 'label@location' for every needle found; never returns the needle."""
    found = []
    encoded = {label: value.encode() for label, value in needles.items() if value}
    for index, text in enumerate(texts):
        data = text.encode() if isinstance(text, str) else bytes(text)
        found.extend(f"{label}@text[{index}]" for label, value in encoded.items() if value in data)
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for directory, _dirs, files in os.walk(root):
            for name in files:
                path = Path(directory) / name
                try:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                found.extend(
                    f"{label}@{path.relative_to(root)}"
                    for label, value in encoded.items() if value in data
                )
    return found


def real_credential_needles() -> dict[str, str]:
    """The installed credential and its 8-character prefix (Grail logs prefixes)."""
    try:
        raw = INSTALLED_CREDENTIAL.read_text().strip()
    except OSError:
        return {}
    value = json.loads(raw).get("access_token", "") if raw.startswith("{") else raw
    if not value:
        return {}
    return {"credential": value, "credential-prefix8": value[:8] if len(value) >= 16 else ""}


_CACHE: Path | None = None


def prepared_cache() -> Path:
    """One verified Grail cache + hash-locked worker venv per test process.

    ``BRAINSTEM_AGENT_TEST_CACHE`` reuses an existing cache directory across runs
    (development speed); otherwise a fresh temporary cache is built through the
    public ``setup`` command from the verified seed (``find_seed``) and removed at
    exit. Without a prepared cache or a seed the calling test (or class) skips.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    configured = os.environ.get("BRAINSTEM_AGENT_TEST_CACHE")
    if _cached_source(configured) is None and not HAVE_SEED:
        raise unittest.SkipTest(NO_SEED)
    if configured:
        cache = Path(configured).resolve()
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    else:
        cache = private_dir(prefix="ba-cache-")
        atexit.register(remove_tree, cache)
    home = private_dir(prefix="ba-setup-home-")
    try:
        extra = {"BRAINSTEM_AGENT_GRAIL_SEED": str(SEED)} if HAVE_SEED else {}
        env = base_env(home, BRAINSTEM_AGENT_CACHE=str(cache), **extra)
        result = run_cli(["setup", "--json"], env, timeout=900)
        if result.returncode != 0:
            raise AssertionError(
                f"setup failed (exit {result.returncode}): {redact(result.stdout[-600:])} "
                f"{redact(result.stderr[-600:])}"
            )
    finally:
        remove_tree(home)
    _CACHE = cache
    return cache


def pid_running(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie (zombies run no code)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                           text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def group_exists(pgid: int) -> bool:
    """True while any process (running or zombie) is still in process group ``pgid``."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    listing = subprocess.run(["/bin/ps", "-A", "-o", "pgid="], capture_output=True,
                             text=True).stdout.split()
    return str(pgid) in listing


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def kill_quietly(pid: int, *, group: bool = True) -> None:
    """Test cleanup: SIGKILL a process (group) the test started; never raises."""
    import signal

    for kill in ((os.killpg, os.kill) if group else (os.kill,)):
        try:
            kill(pid, signal.SIGKILL)
        except OSError:
            pass


def record_metric(name: str, value) -> None:
    """Append a metric for the evidence runner when BRAINSTEM_AGENT_METRICS_FILE is set."""
    target = os.environ.get("BRAINSTEM_AGENT_METRICS_FILE")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"name": name, "value": value}) + "\n")
