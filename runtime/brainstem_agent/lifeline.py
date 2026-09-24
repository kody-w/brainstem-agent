"""Lifeline: process groups started by a host never outlive it.

A ``Supervisor`` (one per ``AgentHost``) records every process group the host
starts (Grail workers, helpers' workers, shell commands, scripts, background
processes) under ``<home>/run/hosts/<host_id>/``
while it holds an exclusive ``flock`` on that directory's ``lease`` file.

- Lifeline: the first tracked group also starts a tiny watchdog in its own
  session that holds the read end of a pipe. When the host dies, even by
  SIGKILL, the pipe reaches EOF and the watchdog SIGKILLs every recorded group
  whose leader still has the exact recorded start time, then exits.
- Reaper: every host start inspects hosts whose lease nobody holds (their host
  is dead). It kills a recorded group only when it can prove the group is that
  host's: same pid, same start identity and one of the recorded programs. It
  then removes the recorded trees and the dead host's records.
- Launch gate (``gated_popen``): a tracked program runs only after its pid is
  recorded, so a host that dies between ``Popen`` and the record never leaves an
  unrecorded program running.

macOS answers ``killpg`` with EPERM when every member of a group is a zombie,
so group state comes from the kernel (libproc), never from ``killpg`` alone.
Limit: a descendant that calls ``setsid()`` leaves its group, so no group kill
reaches it; it stays inside the sandbox it was started in.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = ["ALIVE", "GONE", "ZOMBIES", "LifelineError", "Supervisor", "Tracked", "gated_popen",
           "group_state", "program", "remove_tree", "start_time", "stop_group"]

ALIVE, ZOMBIES, GONE = "alive", "zombies", "gone"
_PACKAGE_PARENT = Path(__file__).resolve().parents[1]
_REAPABLE = (("workers",), ("run", "shell"), ("run", "scripts"), ("run", "processes"))
_SPAWN_TOLERANCE = 2.0
_WATCHDOG = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "from brainstem_agent.lifeline import _watch; _watch(sys.argv[2])")
# The launch gate: the child waits for one byte before it execs the real program. The host
# sends it only after the pid is recorded, so a host killed in between (found by a fault-injection
# soak) leaves a gate that reads EOF and exits, never an unrecorded program no lifeline knows.
_GATE = ("import os, sys\n"
         "fd = int(sys.argv[1])\n"
         "opened = os.read(fd, 1) == b'g'\n"
         "os.close(fd)\n"
         "if not opened:\n"
         "    os._exit(111)\n"
         "os.execv(sys.argv[2], sys.argv[2:])\n")


class LifelineError(RuntimeError):
    """The lifeline directory or watchdog cannot be used."""


def _gate_programs() -> tuple[str, ...]:
    """What a gated leader runs before its exec: this interpreter (framework builds re-exec
    the binary inside ``Resources/Python.app``)."""
    real = os.path.realpath(sys.executable)
    app = Path(real).parent.parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return (real, *((str(app),) if app.is_file() else ()))


def gated_popen(argv: Sequence[str], *, tracked: Tracked | None, expect: Sequence[str] = (),
                **options: Any) -> subprocess.Popen:
    """Popen ``argv`` (absolute program path) so that it runs only once ``tracked`` recorded it."""
    if tracked is None:
        return subprocess.Popen(list(argv), **options)
    if not os.path.isabs(argv[0]):
        raise LifelineError("A gated launch needs an absolute program path.")
    read_end, write_end = os.pipe()
    extra = tuple(options.pop("pass_fds", ()))
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _GATE, str(read_end), *argv],
            pass_fds=(read_end, *extra), **options)
    except BaseException:
        os.close(read_end)
        os.close(write_end)
        raise
    os.close(read_end)
    try:
        tracked.started(process.pid, expect=(*expect, *_gate_programs()), process=process)
        os.write(write_end, b"g")
    except BaseException:
        os.close(write_end)  # the gate reads EOF and exits without running the program
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        raise
    os.close(write_end)
    return process


class _BSDInfo(ctypes.Structure):
    """``struct proc_bsdinfo`` (<sys/proc_info.h>), flavor PROC_PIDTBSDINFO."""

    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid",
        "svgid", "rfu")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32), ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32), ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64)]


_LIB: Any = None


def _libproc():
    """libproc from libSystem, or None where it does not exist (not macOS)."""
    global _LIB
    if _LIB is None:
        try:
            lib = ctypes.CDLL(None, use_errno=True)
            lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_int]
            lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            lib.proc_listpgrppids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            for function in (lib.proc_pidinfo, lib.proc_pidpath, lib.proc_listpgrppids):
                function.restype = ctypes.c_int
            _LIB = lib
        except (OSError, AttributeError):
            _LIB = False
    return _LIB or None


def _bsd_info(pid: int) -> tuple[_BSDInfo | None, int]:
    """Kernel facts about a live process; (None, ESRCH) for zombies and the dead."""
    lib = _libproc()
    if lib is None:
        return None, errno.ENOSYS
    info = _BSDInfo()
    size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        return None, ctypes.get_errno() or errno.ESRCH
    return info, 0


def start_time(pid: int) -> float | None:
    """Kernel start time of a live process (unchanged by exec), or None."""
    info, _ = _bsd_info(pid)
    return None if info is None else info.start_sec + info.start_usec / 1_000_000


def program(pid: int) -> str | None:
    """Resolved path of the executable a live process runs now, or None."""
    lib = _libproc()
    if lib is None:
        return None
    buffer = ctypes.create_string_buffer(4096)
    length = lib.proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
    return os.path.realpath(os.fsdecode(buffer.value)) if length > 0 else None


def _members(pgid: int) -> list[int] | None:
    lib = _libproc()
    if lib is None:
        return None
    capacity = 256
    while True:
        buffer = (ctypes.c_int * capacity)()
        count = lib.proc_listpgrppids(pgid, buffer, ctypes.sizeof(buffer))
        if count < 0:
            return None
        if count < capacity or capacity >= 65536:
            return [buffer[index] for index in range(count) if buffer[index] > 0]
        capacity *= 4


def group_state(pgid: int) -> str:
    """``alive`` (a member can still run), ``zombies`` (only unreaped exits) or ``gone``."""
    try:
        os.killpg(pgid, 0)
        return ALIVE
    except ProcessLookupError:
        return GONE
    except PermissionError:
        pass
    members = _members(pgid)
    if members is None:
        return ZOMBIES
    for pid in members:
        info, error = _bsd_info(pid)
        if (info is not None and info.status != 5) or error == errno.EPERM:
            return ALIVE
    return ZOMBIES if members else GONE


def _signal(pgid: int, number: int) -> bool:
    try:
        os.killpg(pgid, number)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _poll(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    delay = 0.001
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.05)
    return True


def stop_group(pgid: int, *, process: subprocess.Popen | None = None, grace: float = 2.0,
               timeout: float = 5.0, settle: float = 0.5) -> dict:
    """Stop process group ``pgid``, reap our leader ``process`` and confirm the outcome.

    SIGTERM (when ``grace`` > 0) and SIGKILL are both sent before the leader is
    reaped, so the group id cannot be recycled in between. Zombies whose parent
    is not us are awaited for at most ``settle`` seconds.
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    if grace > 0 and _signal(pgid, signal.SIGTERM):
        _poll(lambda: group_state(pgid) != ALIVE, min(grace, max(0.0, deadline - started)))
    _signal(pgid, signal.SIGKILL)
    exit_code = None
    if process is not None:
        try:
            exit_code = process.wait(max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            exit_code = process.poll()
    state, settle_until, delay = group_state(pgid), None, 0.002
    while state != GONE:
        now = time.monotonic()
        if state == ALIVE:
            if now >= deadline:
                break
            _signal(pgid, signal.SIGKILL)
        else:
            settle_until = settle_until or min(deadline, now + settle)
            if now >= settle_until:
                break
        time.sleep(delay)
        delay = min(delay * 2, 0.05)
        state = group_state(pgid)
    return {"group_state": state, "group_gone": state == GONE, "exit_code": exit_code,
            "seconds": round(time.monotonic() - started, 3)}


def remove_tree(path: Path | str) -> None:
    """Delete a tree we created, including read-only directories; never follows a leaf link."""
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            path.unlink()
            return
        os.chmod(path, 0o700)
    except OSError:
        return
    for directory, names, _files in os.walk(path):
        for name in names:
            try:
                os.chmod(os.path.join(directory, name), 0o700, follow_symlinks=False)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _verdict(record: Mapping[str, Any], *, exact: bool, check_program: bool) -> str:
    """Can we prove the recorded leader is the process that host started?"""
    pid = record.get("pid")
    if type(pid) is not int or pid <= 1:
        return "never-started"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "not-ours"
    info, error = _bsd_info(pid)
    if info is None:
        return "zombies" if error == errno.ESRCH else "not-ours"
    started = info.start_sec + info.start_usec / 1_000_000
    recorded, spawned = record.get("start"), record.get("spawned_at")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        same = abs(started - recorded) < 0.001
    else:
        same = (not exact and isinstance(spawned, (int, float)) and not isinstance(spawned, bool)
                and abs(started - spawned) <= _SPAWN_TOLERANCE)
    if not same or info.pgid != pid or record.get("pgid", pid) != pid or info.uid != os.geteuid():
        return "not-ours"
    if check_program:
        expected = {os.path.realpath(item) for item in record.get("expect") or ()
                    if isinstance(item, str) and os.path.isabs(item)}
        if program(pid) not in expected:
            return "not-ours"
    return "ours"


def _records(directory: Path) -> list[tuple[Path, dict]]:
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found = []
    for name in names:
        if name.startswith(".") or not name.endswith(".json"):
            continue
        try:
            descriptor = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as handle:
                record = json.loads(handle.read(65536))
        except (OSError, ValueError):
            record = None
        found.append((directory / name, record if isinstance(record, dict) else {}))
    return found


def _watch(directory: str) -> None:
    """Watchdog main: wait for the host's end of the lifeline to close, then kill."""
    _libproc()
    while True:
        try:
            if not os.read(0, 4096):
                break
        except OSError:
            break
    for _path, record in _records(Path(directory)):
        if _verdict(record, exact=True, check_program=False) == "ours":
            _signal(record["pid"], signal.SIGKILL)


def _private_dirs(root: Path, parts: Sequence[str]) -> Path:
    path = root
    for part in parts:
        path = path / part
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise LifelineError(f"{path} must be a directory owned by you.")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(path, 0o700)
    return path


def _write_json(directory: Path, name: str, document: Mapping[str, Any]) -> None:
    temporary = directory / f".{name}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(dict(document), handle)
    os.replace(temporary, directory / name)


class Tracked:
    """One recorded process group; ``started`` once it exists, ``finish`` once it is stopped."""

    def __init__(self, supervisor: Supervisor, kind: str, path: Path) -> None:
        self._supervisor = supervisor
        self.kind, self.path = kind, Path(path)
        self.name = f"{kind}-{secrets.token_hex(6)}.json"
        self.pid: int | None = None
        self.process: subprocess.Popen | None = None
        self.record: dict[str, Any] = {"kind": kind, "path": str(self.path), "pid": None,
                                       "pgid": None, "spawned_at": None, "start": None,
                                       "expect": []}

    def started(self, pid: int, *, expect: Sequence[str] = (),
                process: subprocess.Popen | None = None) -> None:
        """Record the session leader ``pid`` (its own group) and the programs it may run."""
        self.pid, self.process = pid, process
        self.record.update(pid=pid, pgid=pid, spawned_at=time.time(), start=start_time(pid),
                           expect=[os.path.realpath(item) for item in expect])
        self._supervisor._save(self)

    def finish(self) -> None:
        """The owner stopped the group itself; forget it (idempotent)."""
        self._supervisor._forget(self)


class Supervisor:
    def __init__(self, home: Path | str, *, python: str | None = None) -> None:
        self.home = Path(os.path.realpath(home))
        self._hosts = _private_dirs(self.home, ("run", "hosts"))
        self._python = python or sys.executable
        self._lock = threading.Lock()
        self._tracked: dict[str, Tracked] = {}
        self._watchdog: subprocess.Popen | None = None
        self._closed = False
        self.recovered: list[dict] = self._reap()
        self.host_id = "h" + secrets.token_hex(6)
        staging = self._hosts / ("." + self.host_id)
        staging.mkdir(mode=0o700)
        self._lease = -1
        try:
            self._lease = os.open(staging / "lease", os.O_RDWR | os.O_CREAT | os.O_EXCL
                                  | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self._lease, json.dumps({"pid": os.getpid()}).encode())
            self.directory = self._hosts / self.host_id
            os.rename(staging, self.directory)
        except BaseException:
            if self._lease >= 0:
                os.close(self._lease)
            remove_tree(staging)
            raise

    @property
    def watchdog_pid(self) -> int | None:
        return self._watchdog.pid if self._watchdog is not None else None

    # -- tracking ------------------------------------------------------------------
    def track(self, kind: str, path: Path | str) -> Tracked:
        """Record a group before it starts, so a dead host's trees are always found."""
        with self._lock:
            if self._closed:
                raise LifelineError("The lifeline supervisor is closed.")
            if self._watchdog is None or self._watchdog.poll() is not None:
                self._watchdog = subprocess.Popen(
                    [self._python, "-I", "-S", "-c", _WATCHDOG, str(_PACKAGE_PARENT),
                     str(self.directory)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin"}, cwd="/", start_new_session=True)
            tracked = Tracked(self, kind, path)
            self._tracked[tracked.name] = tracked
        self._save(tracked)
        return tracked

    def _save(self, tracked: Tracked) -> None:
        with self._lock:
            if tracked.name in self._tracked:
                _write_json(self.directory, tracked.name, tracked.record)

    def _forget(self, tracked: Tracked) -> None:
        with self._lock:
            if self._tracked.pop(tracked.name, None) is not None:
                (self.directory / tracked.name).unlink(missing_ok=True)

    # -- reaping -------------------------------------------------------------------
    def _reapable(self, value: Any) -> Path | None:
        if not isinstance(value, str) or not os.path.isabs(value):
            return None
        candidate = Path(os.path.realpath(os.path.dirname(value))) / os.path.basename(value)
        for parts in _REAPABLE:
            root = self.home.joinpath(*parts)
            if root in candidate.parents:
                return candidate
        return None

    def _reap_record(self, host: str, record: Mapping[str, Any]) -> dict:
        entry = {"host": host, "kind": record.get("kind"), "path": record.get("path"),
                 "pid": record.get("pid")}
        verdict = _verdict(record, exact=False, check_program=True)
        if verdict == "ours":
            stopped = stop_group(record["pid"], grace=0, timeout=3.0)
            entry.update(action="killed", group_state=stopped["group_state"],
                         group_gone=stopped["group_gone"])
        else:
            action = {"gone": "already-gone"}.get(verdict, verdict)
            entry["action"] = action
            if action in ("already-gone", "zombies"):
                state = group_state(record["pid"])
                entry.update(group_state=state, group_gone=state == GONE)
        tree = self._reapable(record.get("path"))
        if tree is not None:
            remove_tree(tree)
            if tree.parent.parent == self.home / "workers":
                try:  # the emptied workers/<id> directory of that generation
                    tree.parent.rmdir()
                except OSError:
                    pass
        return entry

    def _reap(self) -> list[dict]:
        recovered = []
        try:
            names = sorted(os.listdir(self._hosts))
        except OSError:
            return recovered
        for name in names:
            directory = self._hosts / name
            if name.startswith("."):
                try:
                    if time.time() - directory.lstat().st_mtime > 60:
                        remove_tree(directory)
                except OSError:
                    pass
                continue
            try:
                lease = os.open(directory / "lease", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                for path, record in _records(directory):
                    if record:
                        recovered.append(self._reap_record(name, record))
                    path.unlink(missing_ok=True)
                remove_tree(directory)
            finally:
                os.close(lease)
        return recovered

    # -- shutdown ------------------------------------------------------------------
    def close(self, *, timeout: float = 3.0) -> None:
        """Stop groups still tracked, drop this host's records and retire the watchdog."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            leftovers = list(self._tracked.values())
            self._tracked.clear()
        remaining = False
        for tracked in leftovers:
            if tracked.pid is not None:
                stopped = stop_group(tracked.pid, process=tracked.process, grace=0, timeout=timeout)
                if stopped["group_state"] == ALIVE:
                    remaining = True
                    continue
            tree = self._reapable(str(tracked.path))
            if tree is not None:
                remove_tree(tree)
            (self.directory / tracked.name).unlink(missing_ok=True)
        if not remaining:
            remove_tree(self.directory)
        os.close(self._lease)
        watchdog = self._watchdog
        if watchdog is not None:
            watchdog.stdin.close()
            try:
                watchdog.wait(timeout)
            except subprocess.TimeoutExpired:
                watchdog.kill()
                watchdog.wait(timeout)
