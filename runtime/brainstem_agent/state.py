"""Original SQLite state for offline fixtures, not an execution sandbox.

``owner`` is an authenticated caller binding supplied by the embedding code,
never authority inferred from model input. JSON equality here means equality of
this application's deterministic JSON encoding, not RAPP canonicalization or
content addressing. Returned JSON values are detached snapshots.

Use an existing, owner-only directory for the database. Path checks protect
ordinary private fixtures; they do not isolate hostile processes with the same
OS identity or make filesystem effects atomic with database commits.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import stat
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .credentials import credential_kinds


class StateError(Exception):
    """Invalid fixture state, input, storage, or database lifecycle."""


class ConflictError(StateError):
    """An existing binding or state prevents the requested operation."""


class NotFoundError(StateError):
    """No record exists in the authenticated caller's namespace."""


class CredentialRefused(StateError):
    """Credential-shaped text offered as knowledge (a fact or a skill): never stored.

    ``kinds`` names the shapes found; the message never repeats the text."""

    def __init__(self, kinds: list[str]):
        self.kinds = list(kinds)
        super().__init__(
            f"This text looks like a credential ({', '.join(self.kinds)}); credentials are never "
            "stored in memory, the owner's profile or skills. Keep the secret in a secret "
            "manager or an environment variable and save only where to find it.")


def _no_credentials(*texts: Any) -> None:
    kinds: list[str] = []
    for text in texts:
        kinds += [kind for kind in credential_kinds(text) if kind not in kinds]
    if kinds:
        raise CredentialRefused(kinds)


@dataclass(frozen=True)
class ChatReservation:
    turn_id: str
    session_id: str
    state: str
    response: dict[str, Any] | None
    created: bool


@dataclass(frozen=True)
class JobReservation:
    job_id: str
    state: str
    request: Any
    result: Any
    created: bool


_VERSION = 2
_APPLICATION_ID = 0x4D304653
_MAX_ID_BYTES = 512
_MAX_TEXT_BYTES = 64 * 1024
_MAX_JSON_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 10_000
_TERMINAL = frozenset({"succeeded", "failed", "uncertain", "cancelled"})
_CHAT_STATES = _TERMINAL | {"reserved", "running"}
_JOB_STATES = _TERMINAL | {"accepted", "running"}
_JOB_TRANSITIONS = {
    "accepted": frozenset({"running", "cancelled"}),
    "running": _TERMINAL,
}
_SCHEMA = {
    "sessions": """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        owner TEXT NOT NULL,
        UNIQUE (owner, session_id)
    )""",
    "chats": """CREATE TABLE chats (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        user_input TEXT NOT NULL,
        key_session TEXT NOT NULL,
        idempotency_key TEXT,
        state TEXT NOT NULL CHECK (
            state IN ('reserved', 'running', 'succeeded', 'failed', 'uncertain', 'cancelled')
        ),
        response_json TEXT,
        FOREIGN KEY (owner, session_id) REFERENCES sessions (owner, session_id),
        CHECK (key_session = '' OR key_session = session_id),
        CHECK (state <> 'succeeded' OR response_json IS NOT NULL),
        CHECK (state NOT IN ('reserved', 'running') OR response_json IS NULL)
    )""",
    "chat_replay": """CREATE UNIQUE INDEX chat_replay
        ON chats (owner, key_session, idempotency_key)
        WHERE idempotency_key IS NOT NULL""",
    "chat_active": """CREATE UNIQUE INDEX chat_active
        ON chats (owner, session_id) WHERE state IN ('reserved', 'running')""",
    "jobs": """CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        owner TEXT NOT NULL,
        replay_key TEXT NOT NULL,
        request_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('accepted', 'running', 'succeeded', 'failed', 'uncertain', 'cancelled')
        ),
        result_json TEXT,
        UNIQUE (owner, replay_key),
        CHECK (state NOT IN ('accepted', 'running') OR result_json IS NULL)
    )""",
    "grants": """CREATE TABLE grants (
        grant_id TEXT PRIMARY KEY NOT NULL,
        binding_json TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
    )""",
}
_V1_TABLES = frozenset(_SCHEMA)
_SCHEMA.update({
    "receipts": """CREATE TABLE receipts (
        receipt_id TEXT PRIMARY KEY NOT NULL,
        namespace TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        capability TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('started', 'succeeded', 'failed', 'denied', 'uncertain')
        ),
        request_json TEXT NOT NULL,
        result_json TEXT,
        created_at REAL NOT NULL,
        finished_at REAL,
        UNIQUE (namespace, turn_id, call_id)
    )""",
    "facts": """CREATE TABLE facts (
        fact_id TEXT PRIMARY KEY NOT NULL,
        namespace TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        source_turn TEXT
    )""",
    "facts_namespace": """CREATE INDEX facts_namespace ON facts (namespace, updated_at)""",
    "run_events": """CREATE TABLE run_events (
        owner TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (owner, turn_id, seq)
    )""",
})
# Durable scheduling extends v2 additively: a store with exactly the Cell v1 v2 tables gains
# these in one transaction on open (never reset); older binaries refuse the extended schema.
_V2_TABLES = frozenset(_SCHEMA)
_SCHEMA.update({
    "schedules": """CREATE TABLE schedules (
        schedule_id TEXT PRIMARY KEY NOT NULL,
        namespace TEXT NOT NULL,
        workspace TEXT NOT NULL,
        name TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        timezone TEXT NOT NULL,
        prompt TEXT NOT NULL,
        capabilities_json TEXT NOT NULL,
        missed_policy TEXT NOT NULL CHECK (missed_policy IN ('run-latest-once-late', 'skip')),
        state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'completed', 'removed')),
        next_fire_at INTEGER,
        run_now_at REAL,
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (state <> 'active' OR next_fire_at IS NOT NULL)
    )""",
    "schedules_next_fire": """CREATE INDEX schedules_next_fire ON schedules (state, next_fire_at)""",
    "occurrences": """CREATE TABLE occurrences (
        occurrence_id TEXT PRIMARY KEY NOT NULL,
        schedule_id TEXT NOT NULL REFERENCES schedules (schedule_id),
        namespace TEXT NOT NULL,
        scheduled_at INTEGER NOT NULL,
        manual INTEGER NOT NULL CHECK (manual IN (0, 1)),
        state TEXT NOT NULL CHECK (
            state IN ('running', 'succeeded', 'failed', 'uncertain', 'cancelled', 'skipped')
        ),
        reason TEXT,
        late_seconds REAL,
        missed_from INTEGER,
        turn_id TEXT,
        claimed_at REAL NOT NULL,
        finished_at REAL,
        result_json TEXT
    )""",
    "occurrences_recent": """CREATE INDEX occurrences_recent ON occurrences (namespace, claimed_at)""",
})
# The learning cell extends v2 the same way: a store with exactly the Cell v1 or the
# scheduling v2 tables gains skills (every version retained), fact usage and turn times.
_V2S_TABLES = frozenset(_SCHEMA)
_SCHEMA.update({
    "skills": """CREATE TABLE skills (
        skill_id TEXT PRIMARY KEY NOT NULL,
        scope TEXT NOT NULL,
        name TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'disabled')),
        review TEXT NOT NULL CHECK (review IN ('approved', 'unreviewed', 'quarantined')),
        version INTEGER NOT NULL,
        pending INTEGER,
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        uses INTEGER NOT NULL DEFAULT 0,
        last_used_at REAL,
        UNIQUE (scope, name)
    )""",
    "skill_versions": """CREATE TABLE skill_versions (
        skill_id TEXT NOT NULL REFERENCES skills (skill_id),
        version INTEGER NOT NULL,
        description TEXT NOT NULL,
        when_to_use TEXT NOT NULL,
        steps_json TEXT NOT NULL,
        note TEXT,
        author TEXT NOT NULL,
        review TEXT NOT NULL,
        tainted INTEGER NOT NULL CHECK (tainted IN (0, 1)),
        session_id TEXT,
        turn_id TEXT,
        workspace TEXT,
        created_at REAL NOT NULL,
        PRIMARY KEY (skill_id, version)
    )""",
    "fact_uses": """CREATE TABLE fact_uses (
        fact_id TEXT PRIMARY KEY NOT NULL,
        uses INTEGER NOT NULL,
        last_used_at REAL NOT NULL
    )""",
    "turn_log": """CREATE TABLE turn_log (
        turn_id TEXT PRIMARY KEY NOT NULL,
        namespace TEXT NOT NULL,
        session_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL
    )""",
})
# Long turns extend v2 the same way: the journal of every turn's segments and children
# (``turn_steps``) and the background processes the cell started (``processes``).
_V2L_TABLES = frozenset(_SCHEMA)
_SCHEMA.update({
    "turn_steps": """CREATE TABLE turn_steps (
        step_id TEXT PRIMARY KEY NOT NULL,
        turn_id TEXT NOT NULL,
        parent_id TEXT,
        namespace TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('turn', 'segment', 'child')),
        seq INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('running', 'succeeded', 'partial', 'failed', 'uncertain', 'cancelled')
        ),
        detail_json TEXT NOT NULL,
        result_json TEXT,
        started_at REAL NOT NULL,
        finished_at REAL,
        UNIQUE (turn_id, kind, seq)
    )""",
    "turn_steps_running": """CREATE INDEX turn_steps_running ON turn_steps (state, kind)""",
    "processes": """CREATE TABLE processes (
        process_id TEXT PRIMARY KEY NOT NULL,
        namespace TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        name TEXT NOT NULL,
        command TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('starting', 'running', 'exited', 'stopped', 'failed', 'lost')
        ),
        pid INTEGER,
        exit_code INTEGER,
        output_bytes INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL,
        finished_at REAL,
        detail_json TEXT
    )""",
    "processes_state": """CREATE INDEX processes_state ON processes (namespace, state)""",
})
_KNOWN_LAYOUTS = {1: (_V1_TABLES,), 2: (_V2_TABLES, _V2S_TABLES, _V2L_TABLES)}


def schema_digest(statements: dict[str, str]) -> str:
    """SHA-256 of a schema (object name -> CREATE statement, whitespace normalized)."""
    text = "\n".join(f"{name}\t{' '.join(sql.split())}" for name, sql in sorted(statements.items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SCHEMA_VERSION = _VERSION
SCHEMA_DIGEST = schema_digest(_SCHEMA)
# The older layouts this version migrates in place (after a pre-migration backup).
MIGRATES_FROM = tuple({"schema_version": version,
                       "schema_sha256": schema_digest({name: _SCHEMA[name] for name in layout})}
                      for version, layouts in _KNOWN_LAYOUTS.items() for layout in layouts)
# Test hook (like ``longturn.crash_point``): SIGKILL this process in the middle of a store
# migration when the owner's environment names ``store.migrating`` in BRAINSTEM_AGENT_CRASH_AT.
STORE_CRASH_POINTS = frozenset({"store.migrating"})


def _store_crash_point(name: str) -> None:
    spec = os.environ.get("BRAINSTEM_AGENT_CRASH_AT")
    if spec and name in STORE_CRASH_POINTS and name in (
            item.strip().partition("#")[0] for item in spec.split(",")):
        os.kill(os.getpid(), signal.SIGKILL)


def _schema_of(connection: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: " ".join(row[1].split()) for row in connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE substr(name, 1, 7) <> 'sqlite_'")
        if type(row[1]) is str}


def classify_schema(version: int, application_id: int, actual: dict[str, str]) -> str:
    """``current``, ``migrates`` (a known older layout), ``newer`` (a later version wrote it:
    a higher version, or every current table plus unknown ones), ``foreign`` or ``unknown``."""
    ours = {name: " ".join(statement.split()) for name, statement in _SCHEMA.items()}
    if application_id != _APPLICATION_ID:
        return "foreign"
    if version == _VERSION and actual == ours:
        return "current"
    for layout in _KNOWN_LAYOUTS.get(version, ()):
        if actual == {name: ours[name] for name in layout}:
            return "migrates"
    if version > _VERSION or (version == _VERSION and actual.items() > ours.items()):
        return "newer"
    return "unknown"


def inspect_database(path: str | os.PathLike[str]) -> dict[str, Any]:
    """What a store file holds, read-only (never migrates, never writes)."""
    path = Path(path)
    if not path.exists():
        return {"exists": False}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            actual = _schema_of(connection)
        finally:
            connection.close()
    except sqlite3.Error as error:
        return {"exists": True, "readable": False, "error": str(error)[:200]}
    return {"exists": True, "readable": True, "schema_version": version,
            "schema_sha256": schema_digest(actual), "objects": len(actual),
            "bytes": path.stat().st_size,
            "compatibility": classify_schema(version, application, actual)}


def read_counts(path: str | os.PathLike[str]) -> dict[str, int]:
    """Row counts of the main tables, read-only (never migrates, never writes)."""
    connection = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=5.0)
    try:
        present = _schema_of(connection)
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("chats", "facts", "skills", "schedules", "occurrences", "receipts")
                if table in present}
    finally:
        connection.close()


class _StillLocked(Exception):
    pass


def backup_database(source: str | os.PathLike[str], target: str | os.PathLike[str], *,
                    timeout: float = 60.0) -> dict:
    """A consistent copy of a store with SQLite's online backup API (writers elsewhere may
    keep working), written to a new 0600 ``target``, integrity-checked and synced. A store
    another connection keeps locked (a write transaction mid-commit) for ``timeout`` seconds
    fails the copy instead of waiting forever (Python's backup retries a busy source
    endlessly); nothing is left at ``target``."""
    source, target = Path(source), Path(target)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    started = time.monotonic()
    deadline = started + max(0.0, timeout)

    def progress(status: int, _remaining: int, _total: int) -> None:
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and \
                time.monotonic() >= deadline:
            raise _StillLocked()

    try:
        reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True,
                                 timeout=max(0.05, min(5.0, timeout)))
        writer = sqlite3.connect(target, timeout=30.0)
        try:
            reader.backup(writer, progress=progress)
            writer.commit()
            check = [row[0] for row in writer.execute("PRAGMA integrity_check")]
            version = writer.execute("PRAGMA user_version").fetchone()[0]
            application = writer.execute("PRAGMA application_id").fetchone()[0]
            actual = _schema_of(writer)
            counts = {}
            for table in ("chats", "facts", "skills", "schedules", "occurrences", "receipts",
                          "turn_steps"):
                if table in actual:
                    counts[table] = writer.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            writer.close()
            reader.close()
    except _StillLocked:
        target.unlink(missing_ok=True)
        raise StateError(f"The store stayed locked by another connection for {timeout:g}s "
                         "(a write transaction was open), so it was not copied; nothing was "
                         "written. Try again when the cell is idle.") from None
    except sqlite3.Error as error:
        target.unlink(missing_ok=True)
        if time.monotonic() >= deadline and "locked" in str(error):
            raise StateError(f"The store stayed locked by another connection for {timeout:g}s "
                             "(a write transaction was open), so it was not copied; nothing "
                             "was written. Try again when the cell is idle.") from None
        raise StateError(f"The store could not be backed up: {error}") from None
    os.chmod(target, 0o600)
    with open(target, "rb") as handle:
        os.fsync(handle.fileno())
    if check != ["ok"]:
        target.unlink(missing_ok=True)
        raise StateError("The store's backup copy failed its integrity check")
    return {"method": "sqlite-online-backup", "integrity_check": "ok", "schema_version": version,
            "schema_sha256": schema_digest(actual),
            "compatibility": classify_schema(version, application, actual), "counts": counts,
            "bytes": target.stat().st_size, "seconds": round(time.monotonic() - started, 3)}


_STEP_STATES = frozenset({"running", "succeeded", "partial", "failed", "uncertain", "cancelled"})
_STEP_KINDS = frozenset({"turn", "segment", "child"})
_PROCESS_STATES = frozenset({"starting", "running", "exited", "stopped", "failed", "lost"})
_PROCESS_TERMINAL = frozenset({"exited", "stopped", "failed", "lost"})
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SKILL_REVIEWS = frozenset({"approved", "unreviewed", "quarantined"})
_SKILL_STATES = frozenset({"active", "disabled"})
_MAX_SKILLS = 500
_MAX_SKILL_VERSIONS = 200
_SCHEDULE_STATES = frozenset({"active", "paused", "completed", "removed"})
_MISSED_POLICIES = frozenset({"run-latest-once-late", "skip"})
_OCCURRENCE_TERMINAL = frozenset({"succeeded", "failed", "uncertain", "cancelled", "skipped"})
_SCHEDULE_CHANGES = frozenset({"name", "spec", "timezone", "prompt", "capabilities",
                               "missed_policy", "state", "next_fire_at", "run_now_at",
                               "created_by"})
_RECEIPT_TERMINAL = frozenset({"succeeded", "failed", "denied", "uncertain"})
_MAX_FACT_CHARS = 2000
_MAX_FACTS = 5000
# What a forgotten turn keeps in place of the owner's words and the answers.
FORGOTTEN = "[forgotten by the owner]"
_STOPWORDS = frozenset(
    "a an and are as at be but by did do does for from had has have i in is it its me my "
    "of on or our so that the their them then this to told tell was we were what when where "
    "which who why will with you your answer word one please remember".split()
)


def _text(value: Any, label: str, limit: int, *, blank: bool = False) -> str:
    if type(value) is not str or (not blank and not value.strip()):
        raise StateError(f"{label} must be a nonempty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise StateError(f"{label} contains invalid Unicode") from exc
    if size > limit:
        raise StateError(f"{label} exceeds its byte limit")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _text(value, label, _MAX_ID_BYTES)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StateError(f"{label} contains control characters")
    return value


def _check_json(value: Any) -> None:
    nodes = 0
    string_bytes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes, string_bytes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise StateError("JSON exceeds its structural limit")
        kind = type(item)
        if kind is str:
            _text(item, "JSON string", _MAX_JSON_BYTES, blank=True)
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > _MAX_JSON_BYTES:
                raise StateError("JSON exceeds its byte limit")
        elif kind is float:
            if not math.isfinite(item):
                raise StateError("JSON numbers must be finite")
        elif item is None or kind in (bool, int):
            return
        elif kind is list:
            for child in item:
                visit(child, depth + 1)
        elif kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise StateError("JSON object keys must be strings")
                visit(key, depth + 1)
                visit(child, depth + 1)
        else:
            raise StateError("Value is not a JSON type")

    visit(value, 0)


def _encode_json(value: Any) -> str:
    _check_json(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise StateError("Cannot encode bounded JSON") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise StateError("JSON exceeds its encoded byte limit")
    return encoded


def _decode_json(encoded: Any) -> Any:
    _text(encoded, "Stored JSON", _MAX_JSON_BYTES)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise StateError("Stored JSON contains duplicate members")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise StateError("Stored JSON contains a non-finite number")

    try:
        value = json.loads(encoded, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise StateError("Stored JSON is malformed") from exc
    _check_json(value)
    return value


def _response_json(response: Any, session_id: str) -> str:
    if type(response) is not dict or set(response) != {"response", "agent_logs", "session_id"}:
        raise StateError("Response must have exactly the normalized response fields")
    _text(response["response"], "Response text", _MAX_TEXT_BYTES)
    if _identifier(response["session_id"], "Response session") != session_id:
        raise StateError("Response session does not match the reserved session")
    if type(response["agent_logs"]) is not list:
        raise StateError("Response logs must be a list of strings")
    for entry in response["agent_logs"]:
        _text(entry, "Response log", _MAX_TEXT_BYTES, blank=True)
    return _encode_json(response)


def _private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise StateError("Database and sidecars must be private, single-link 0600 regular files")


def _private_directory(path: Path) -> None:
    for directory in reversed((path, *path.parents)):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise StateError("Database directory components must be real directories, not symlinks")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise StateError("Database parent must be an existing owner-only 0700 directory")


def _database_path(path: Any) -> Path:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise StateError("Database path must be a filesystem path") from exc
    if (
        type(raw) is not str
        or not raw.strip()
        or "\0" in raw
        or raw == ":memory:"
        or raw.startswith("file:")
        or ".." in Path(raw).parts
    ):
        raise StateError("Database requires a plain local file path without parent traversal")
    try:
        raw.encode("utf-8")
    except UnicodeError as exc:
        raise StateError("Database path contains invalid Unicode") from exc
    return Path(os.path.abspath(raw))


def _open_lock(directory: Path, timeout: float = 30.0) -> int:
    """Exclusive flock on the (already verified) database directory, bounded in time."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                         | getattr(os, "O_DIRECTORY", 0))
    deadline = time.monotonic() + timeout
    delay = 0.001
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError("The fixture database is busy being opened elsewhere")
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
    except BaseException:
        os.close(descriptor)
        raise


class Store:
    """Transactional private-fixture store; opening never performs recovery.

    Opening holds a short exclusive ``flock`` on the database directory while the
    file is created (or validated) and its schema committed, so processes that
    open a brand-new store at the same instant never observe a half-created one.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._path = _database_path(path)
        descriptor = None
        guard = None
        try:
            _private_directory(self._path.parent)
            guard = _open_lock(self._path.parent)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                descriptor = os.open(self._path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                os.fchmod(descriptor, 0o600)
                created = True
            except FileExistsError:
                _private_file(self._path.lstat())
                descriptor = os.open(self._path, flags)
                created = False
            info = os.fstat(descriptor)
            _private_file(info)
            self._identity = (info.st_dev, info.st_ino)
            self._check_path()
            self._connection = sqlite3.connect(
                self._path, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA trusted_schema = OFF")
            self._connection.execute("PRAGMA secure_delete = ON")
            self.pre_migration_backup: dict | None = None
            if not created:
                self._before_migration()
            with self._transaction() as connection:
                if created:
                    for statement in _SCHEMA.values():
                        connection.execute(statement)
                    connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version = {_VERSION}")
                else:
                    self._migrate(connection)
                self._validate_database(connection)
            if self._connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StateError("Fixture database must use DELETE journaling")
            self._connection.execute("PRAGMA synchronous = FULL")
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise StateError(f"Cannot open the cell's private store "
                             f"({type(exc).__name__}: {str(exc)[:200]})") from exc
        except BaseException:
            self.close()
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if guard is not None:
                os.close(guard)

    def __enter__(self) -> Store:
        with self._lock:
            self._open_connection()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                connection, self._connection = self._connection, None
                try:
                    connection.close()
                except sqlite3.Error as exc:
                    raise StateError("Cannot close fixture database") from exc

    def _check_path(self) -> None:
        _private_directory(self._path.parent)
        info = self._path.lstat()
        _private_file(info)
        if (info.st_dev, info.st_ino) != self._identity:
            raise StateError("Database path was replaced")
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                info = Path(str(self._path) + suffix).lstat()
            except FileNotFoundError:
                continue
            _private_file(info)

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StateError("Store is closed")
        try:
            self._check_path()
        except OSError as exc:
            raise StateError("Private fixture path is unavailable") from exc
        return self._connection

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._open_connection()
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
            except BaseException as exc:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except sqlite3.Error as rollback_error:
                    raise StateError("Fixture transaction rollback failed") from rollback_error
                if isinstance(exc, sqlite3.Error):
                    raise StateError("Fixture database transaction failed") from exc
                raise

    def _before_migration(self) -> None:
        """Read-only look before any write: a store from a newer version is refused (nothing
        is written), and an older layout is copied aside before it is migrated, so a failed
        or interrupted migration never costs the original."""
        connection = self._connection
        with self._lock:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            actual = _schema_of(connection)
        kind = classify_schema(version, application, actual)
        if kind == "newer":
            extra = sorted(set(actual) - set(_SCHEMA))
            raise StateError(
                f"Unknown store schema (version {version}"
                + (f", unknown tables {', '.join(extra[:5])}" if extra else "")
                + f"): it was written by a newer Brainstem Agent; this version reads schema "
                f"{_VERSION}. Nothing was changed. Upgrade Brainstem Agent, or restore a backup "
                "made by this version.")
        if kind != "migrates":
            return
        folder = self._path.parent / "pre-migration"
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = folder / f"{self._path.stem}-v{version}-{schema_digest(actual)[:8]}-{stamp}.sqlite3"
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            _private_directory(folder)
            if target.exists():
                target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:6]}.sqlite3")
            self.pre_migration_backup = {"path": str(target), "from_schema_version": version,
                                         **backup_database(self._path, target)}
        except (OSError, StateError) as error:
            raise StateError(f"The store needs a migration, but its pre-migration backup could "
                             f"not be written ({error}); nothing was changed.") from None

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Upgrade an exact v1, Cell v1 v2 or scheduling v2 schema inside the open transaction;
        never reset."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if (
            version not in _KNOWN_LAYOUTS
            or connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
        ):
            return
        actual = {
            row["name"]: " ".join(row["sql"].split())
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE substr(name, 1, 7) <> 'sqlite_'")
            if type(row["sql"]) is str
        }
        for known in _KNOWN_LAYOUTS[version]:
            if actual == {name: " ".join(_SCHEMA[name].split()) for name in known}:
                for name, statement in _SCHEMA.items():
                    if name not in known:
                        connection.execute(statement)
                        _store_crash_point("store.migrating")
                connection.execute(f"PRAGMA user_version = {_VERSION}")
                return

    def _validate_database(self, connection: sqlite3.Connection) -> None:
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] != _VERSION
            or connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
        ):
            raise StateError("Unknown fixture database schema or version; refusing to reset")
        actual = {}
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE substr(name, 1, 7) <> 'sqlite_'"
        ):
            if type(row["sql"]) is not str:
                raise StateError("Fixture database contains an invalid schema definition")
            actual[row["name"]] = " ".join(row["sql"].split())
        if actual != {name: " ".join(statement.split()) for name, statement in _SCHEMA.items()}:
            raise StateError("Fixture database schema does not match its version")
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise StateError("Fixture database failed its integrity check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateError("Fixture database contains invalid owner/session bindings")
        for row in connection.execute("SELECT * FROM sessions"):
            _identifier(row["owner"], "Stored owner")
            _identifier(row["session_id"], "Stored session")
        for row in connection.execute("SELECT * FROM chats"):
            _identifier(row["owner"], "Stored owner")
            _text(row["user_input"], "Stored input", _MAX_TEXT_BYTES)
            if row["idempotency_key"] is not None:
                _identifier(row["idempotency_key"], "Stored chat key")
            self._chat(row)
        for row in connection.execute("SELECT * FROM jobs"):
            _identifier(row["owner"], "Stored owner")
            _identifier(row["replay_key"], "Stored job key")
            self._job(row)
        for row in connection.execute("SELECT * FROM grants"):
            _identifier(row["grant_id"], "Stored grant ID")
            self._grant(row)

    @staticmethod
    def _chat(row: sqlite3.Row, *, created: bool = False) -> ChatReservation:
        _identifier(row["turn_id"], "Stored turn ID")
        _identifier(row["session_id"], "Stored session")
        if row["state"] not in _CHAT_STATES:
            raise StateError("Stored chat state is invalid")
        response = None if row["response_json"] is None else _decode_json(row["response_json"])
        if response is not None:
            _response_json(response, row["session_id"])
        if row["state"] == "succeeded" and response is None:
            raise StateError("Successful chat has no normalized response")
        if row["state"] not in _TERMINAL and response is not None:
            raise StateError("Pending chat has a terminal response")
        return ChatReservation(row["turn_id"], row["session_id"], row["state"], response, created)

    @staticmethod
    def _job(row: sqlite3.Row, *, created: bool = False) -> JobReservation:
        _identifier(row["job_id"], "Stored job ID")
        if row["state"] not in _JOB_STATES:
            raise StateError("Stored job state is invalid")
        request = _decode_json(row["request_json"])
        result = None if row["result_json"] is None else _decode_json(row["result_json"])
        if row["state"] not in _TERMINAL and result is not None:
            raise StateError("Pending job has a terminal result")
        return JobReservation(row["job_id"], row["state"], request, result, created)

    @staticmethod
    def _grant(row: sqlite3.Row) -> dict[str, Any]:
        binding = _decode_json(row["binding_json"])
        if type(binding) is not dict or type(row["revoked"]) is not int or row["revoked"] not in (0, 1):
            raise StateError("Stored synthetic grant is malformed")
        binding["revoked"] = bool(row["revoked"])
        return binding

    @staticmethod
    def _owned_chat(connection: sqlite3.Connection, owner: str, turn_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM chats WHERE owner = ? AND turn_id = ?", (owner, turn_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("Chat turn was not found for this owner")
        return row

    @staticmethod
    def _owned_job(connection: sqlite3.Connection, owner: str, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE owner = ? AND job_id = ?", (owner, job_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("Job was not found for this owner")
        return row

    def reserve_chat(
        self, owner: str, user_input: str, session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ChatReservation:
        _identifier(owner, "Owner")
        _text(user_input, "User input", _MAX_TEXT_BYTES)
        if session_id is not None:
            _identifier(session_id, "Session ID")
        if idempotency_key is not None:
            _identifier(idempotency_key, "Idempotency key")
        key_session = "" if session_id is None else session_id
        with self._transaction() as connection:
            if session_id is not None:
                binding = connection.execute(
                    "SELECT owner FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if binding is not None and binding["owner"] != owner:
                    raise ConflictError("Session is bound to another owner")
            if idempotency_key is not None:
                existing = connection.execute(
                    """SELECT * FROM chats
                       WHERE owner = ? AND key_session = ? AND idempotency_key = ?""",
                    (owner, key_session, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._chat(existing)
            if session_id is None:
                session_id = "session_" + uuid.uuid4().hex
            if connection.execute(
                """SELECT 1 FROM chats WHERE owner = ? AND session_id = ?
                   AND state IN ('reserved', 'running')""", (owner, session_id)
            ).fetchone() is not None:
                raise ConflictError("Session already has an active turn")
            connection.execute(
                "INSERT OR IGNORE INTO sessions (owner, session_id) VALUES (?, ?)",
                (owner, session_id),
            )
            turn_id = "turn_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO chats
                   (turn_id, owner, session_id, user_input, key_session, idempotency_key, state)
                   VALUES (?, ?, ?, ?, ?, ?, 'reserved')""",
                (turn_id, owner, session_id, user_input, key_session, idempotency_key),
            )
            connection.execute(
                """INSERT INTO turn_log (turn_id, namespace, session_id, started_at)
                   VALUES (?, ?, ?, ?)""", (turn_id, owner, session_id, time.time()))
            return self._chat(self._owned_chat(connection, owner, turn_id), created=True)

    def has_replay(self, owner: str, session_id: str | None, idempotency_key: str) -> bool:
        """True when ``reserve_chat`` with this key would return an existing chat (read-only)."""
        _identifier(owner, "Owner")
        _identifier(idempotency_key, "Idempotency key")
        with self._transaction(write=False) as connection:
            return connection.execute(
                """SELECT 1 FROM chats WHERE owner = ? AND key_session = ?
                   AND idempotency_key = ?""",
                (owner, "" if session_id is None else session_id, idempotency_key),
            ).fetchone() is not None

    def mark_chat_running(self, owner: str, turn_id: str) -> ChatReservation:
        _identifier(owner, "Owner")
        _identifier(turn_id, "Turn ID")
        with self._transaction() as connection:
            self._owned_chat(connection, owner, turn_id)
            changed = connection.execute(
                """UPDATE chats SET state = 'running'
                   WHERE owner = ? AND turn_id = ? AND state = 'reserved'""", (owner, turn_id)
            ).rowcount
            if changed != 1:
                raise ConflictError("Only a reserved turn can start, exactly once")
            return self._chat(self._owned_chat(connection, owner, turn_id))

    def finish_chat(
        self, owner: str, turn_id: str, state: str, response: dict[str, Any] | None = None,
    ) -> ChatReservation:
        _identifier(owner, "Owner")
        _identifier(turn_id, "Turn ID")
        if type(state) is not str or state not in _TERMINAL:
            raise StateError("Chat completion requires a terminal state")
        with self._transaction() as connection:
            row = self._owned_chat(connection, owner, turn_id)
            if row["state"] in _TERMINAL:
                raise ConflictError("Terminal chat records are immutable")
            if state == "succeeded" and response is None:
                raise StateError("Successful chat requires a normalized response")
            encoded = None if response is None else _response_json(response, row["session_id"])
            connection.execute(
                "UPDATE chats SET state = ?, response_json = ? WHERE owner = ? AND turn_id = ?",
                (state, encoded, owner, turn_id),
            )
            connection.execute("UPDATE turn_log SET finished_at = ? WHERE turn_id = ?",
                               (time.time(), turn_id))
            return self._chat(self._owned_chat(connection, owner, turn_id))

    def history(self, owner: str, session_id: str) -> list[dict[str, str]]:
        _identifier(owner, "Owner")
        _identifier(session_id, "Session ID")
        with self._transaction(write=False) as connection:
            if connection.execute(
                "SELECT 1 FROM sessions WHERE owner = ? AND session_id = ?", (owner, session_id)
            ).fetchone() is None:
                raise NotFoundError("Session was not found for this owner")
            messages = []
            for row in connection.execute(
                """SELECT * FROM chats WHERE owner = ? AND session_id = ?
                   AND state = 'succeeded' AND user_input <> ? ORDER BY ordinal""",
                (owner, session_id, FORGOTTEN)
            ):
                reservation = self._chat(row)
                _text(row["user_input"], "Stored input", _MAX_TEXT_BYTES)
                messages.extend((
                    {"role": "user", "content": row["user_input"]},
                    {"role": "assistant", "content": reservation.response["response"]},
                ))
            return messages

    def admit_job(self, owner: str, key: str, request: Any) -> JobReservation:
        _identifier(owner, "Owner")
        _identifier(key, "Job key")
        encoded = _encode_json(request)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE owner = ? AND replay_key = ?", (owner, key)
            ).fetchone()
            if existing is not None:
                reservation = self._job(existing)
                if _encode_json(reservation.request) != encoded:
                    raise ConflictError("Job key is already bound to a different request")
                return reservation
            job_id = "job_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO jobs (job_id, owner, replay_key, request_json, state)
                   VALUES (?, ?, ?, ?, 'accepted')""", (job_id, owner, key, encoded)
            )
            return self._job(self._owned_job(connection, owner, job_id), created=True)

    def transition_job(
        self, owner: str, job_id: str, expected: str, new_state: str, result: Any = None,
    ) -> JobReservation:
        _identifier(owner, "Owner")
        _identifier(job_id, "Job ID")
        if (
            type(expected) is not str or type(new_state) is not str
            or expected not in _JOB_STATES or new_state not in _JOB_STATES
        ):
            raise StateError("Unknown job state")
        if new_state not in _JOB_TRANSITIONS.get(expected, ()):
            raise ConflictError("Job transition is not allowed")
        if result is not None and new_state not in _TERMINAL:
            raise StateError("Only terminal jobs may retain a result")
        encoded = None if result is None else _encode_json(result)
        with self._transaction() as connection:
            self._owned_job(connection, owner, job_id)
            changed = connection.execute(
                """UPDATE jobs SET state = ?, result_json = ?
                   WHERE owner = ? AND job_id = ? AND state = ?""",
                (new_state, encoded, owner, job_id, expected),
            ).rowcount
            if changed != 1:
                raise ConflictError("Job compare-and-set state did not match")
            return self._job(self._owned_job(connection, owner, job_id))

    def get_job(self, owner: str, job_id: str) -> JobReservation:
        _identifier(owner, "Owner")
        _identifier(job_id, "Job ID")
        with self._transaction(write=False) as connection:
            return self._job(self._owned_job(connection, owner, job_id))

    def recover_interrupted(self) -> None:
        """Exclusive supervisor only: preserve uncertainty, never redispatch work.

        A ``running`` occurrence becomes ``uncertain`` when its turn (found through the
        occurrence id, which is the turn's idempotency key) had started a tool, else
        ``failed``; it is never run again. Long-turn journals are settled the same way
        (``_recover_journal``); other running chats stay conservatively ``uncertain``.
        """
        now = time.time()
        with self._transaction() as connection:
            for row in connection.execute(
                """SELECT o.occurrence_id, o.result_json, c.turn_id FROM occurrences o
                   LEFT JOIN chats c ON c.owner = o.namespace AND c.key_session = ''
                   AND c.idempotency_key = o.occurrence_id
                   WHERE o.state = 'running'""").fetchall():
                turn = row["turn_id"]
                started = turn is not None and connection.execute(
                    "SELECT 1 FROM receipts WHERE turn_id = ? AND state <> 'denied'",
                    (turn,)).fetchone() is not None
                result = {"error": "The daemon stopped during this run; it was not run again."
                          + (" A tool had started, so its effects are uncertain." if started
                             else "")}
                connection.execute(
                    """UPDATE occurrences SET state = ?, turn_id = ?, finished_at = ?,
                       result_json = ? WHERE occurrence_id = ? AND state = 'running'""",
                    ("uncertain" if started else "failed", turn, now,
                     _encode_json(self._with_missed(result, row["result_json"])),
                     row["occurrence_id"]))
            self._recover_journal(connection, now)
            connection.execute("UPDATE chats SET state = 'uncertain' WHERE state = 'running'")
            connection.execute("UPDATE jobs SET state = 'uncertain' WHERE state = 'running'")
            connection.execute(
                "UPDATE receipts SET state = 'uncertain', finished_at = ? WHERE state = 'started'",
                (now,))

    def store_grant(self, grant_id: str, binding: dict[str, Any]) -> None:
        _identifier(grant_id, "Grant ID")
        if type(binding) is not dict:
            raise StateError("Synthetic grant binding must be a JSON object")
        encoded = _encode_json(binding)
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone() is not None:
                raise ConflictError("Grant ID is already bound")
            connection.execute(
                "INSERT INTO grants (grant_id, binding_json) VALUES (?, ?)", (grant_id, encoded)
            )

    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        _identifier(grant_id, "Grant ID")
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            return None if row is None else self._grant(row)

    def revoke_grant(self, grant_id: str) -> None:
        _identifier(grant_id, "Grant ID")
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE grants SET revoked = 1 WHERE grant_id = ?", (grant_id,)
            ).rowcount
            if changed != 1:
                raise NotFoundError("Synthetic grant was not found")

    # -- Store v2: chats/sessions queries, receipts, facts and run events ------------

    def get_chat(self, owner: str, turn_id: str) -> ChatReservation:
        _identifier(owner, "Owner")
        _identifier(turn_id, "Turn ID")
        with self._transaction(write=False) as connection:
            return self._chat(self._owned_chat(connection, owner, turn_id))

    def list_sessions(self, owner: str, *, limit: int = 50) -> list[dict[str, Any]]:
        _identifier(owner, "Owner")
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT session_id, count(*) AS turns, max(ordinal) AS last FROM chats
                   WHERE owner = ? GROUP BY session_id ORDER BY last DESC LIMIT ?""",
                (owner, max(1, min(int(limit), 500))),
            ).fetchall()
            sessions = []
            for row in rows:
                last = connection.execute(
                    """SELECT c.user_input, c.state, t.started_at FROM chats c
                       LEFT JOIN turn_log t ON t.turn_id = c.turn_id WHERE c.ordinal = ?""",
                    (row["last"],)).fetchone()
                sessions.append({
                    "session_id": row["session_id"], "turns": row["turns"],
                    "last_input": last["user_input"][:120], "last_state": last["state"],
                    "last_at": last["started_at"],
                })
            return sessions

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "receipt_id": row["receipt_id"], "turn_id": row["turn_id"], "call_id": row["call_id"],
            "tool": row["tool"], "capability": row["capability"], "state": row["state"],
            "request": _decode_json(row["request_json"]),
            "result": None if row["result_json"] is None else _decode_json(row["result_json"]),
            "created_at": row["created_at"], "finished_at": row["finished_at"],
        }

    def begin_receipt(self, namespace: str, turn_id: str, call_id: str, tool: str,
                      capability: str, request: Any) -> str:
        for value, label in ((namespace, "Namespace"), (turn_id, "Turn ID"), (call_id, "Call ID"),
                             (tool, "Tool"), (capability, "Capability")):
            _identifier(value, label)
        encoded = _encode_json(request)
        receipt_id = "receipt_" + uuid.uuid4().hex
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM receipts WHERE namespace = ? AND turn_id = ? AND call_id = ?",
                (namespace, turn_id, call_id),
            ).fetchone() is not None:
                raise ConflictError("A receipt already exists for this call")
            connection.execute(
                """INSERT INTO receipts (receipt_id, namespace, turn_id, call_id, tool, capability,
                   state, request_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?)""",
                (receipt_id, namespace, turn_id, call_id, tool, capability, encoded, time.time()),
            )
        return receipt_id

    def finish_receipt(self, receipt_id: str, state: str, result: Any) -> None:
        _identifier(receipt_id, "Receipt ID")
        if type(state) is not str or state not in _RECEIPT_TERMINAL:
            raise StateError("Receipts finish in succeeded, failed, denied or uncertain")
        encoded = _encode_json(result)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Receipt was not found")
            if row["state"] != "started":
                raise ConflictError("A receipt finishes exactly once")
            connection.execute(
                """UPDATE receipts SET state = ?, result_json = ?, finished_at = ?
                   WHERE receipt_id = ? AND state = 'started'""",
                (state, encoded, time.time(), receipt_id),
            )

    def list_receipts(self, namespace: str, *, turn_id: str | None = None,
                      limit: int | None = 100) -> list[dict[str, Any]]:
        """Receipts, oldest first: at most ``limit`` (1000 at most), or, for one turn with
        ``limit=None``, every receipt of that turn (settling a turn must see them all)."""
        _identifier(namespace, "Namespace")
        query = "SELECT * FROM receipts WHERE namespace = ?"
        parameters: list[Any] = [namespace]
        if turn_id is not None:
            _identifier(turn_id, "Turn ID")
            query += " AND turn_id = ?"
            parameters.append(turn_id)
        query += " ORDER BY rowid"
        if limit is not None or turn_id is None:
            query += " LIMIT ?"
            parameters.append(max(1, min(int(limit or 100), 1000)))
        with self._transaction(write=False) as connection:
            return [self._receipt(row) for row in connection.execute(query, parameters)]

    @staticmethod
    def _fact(row: sqlite3.Row) -> dict[str, Any]:
        return {"fact_id": row["fact_id"], "text": row["text"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "source_turn": row["source_turn"]}

    @staticmethod
    def _fact_text(text: Any) -> str:
        if type(text) is not str or not text.strip():
            raise StateError("Fact text must be nonempty")
        if len(text) > _MAX_FACT_CHARS:
            raise StateError(f"Fact text exceeds {_MAX_FACT_CHARS} characters")
        _text(text, "Fact text", _MAX_TEXT_BYTES)
        _no_credentials(text)
        return text.strip()

    def add_fact(self, namespace: str, text: str, *, source_turn: str | None = None) -> dict[str, Any]:
        _identifier(namespace, "Namespace")
        text = self._fact_text(text)
        if source_turn is not None:
            _identifier(source_turn, "Source turn")
        fact_id = "fact_" + uuid.uuid4().hex[:16]
        now = time.time()
        with self._transaction() as connection:
            count = connection.execute(
                "SELECT count(*) FROM facts WHERE namespace = ?", (namespace,)).fetchone()[0]
            if count >= _MAX_FACTS:
                raise ConflictError("This memory namespace is full")
            connection.execute(
                """INSERT INTO facts (fact_id, namespace, text, created_at, updated_at, source_turn)
                   VALUES (?, ?, ?, ?, ?, ?)""", (fact_id, namespace, text, now, now, source_turn))
            return self._fact(connection.execute(
                "SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone())

    def update_fact(self, namespace: str, fact_id: str, text: str) -> dict[str, Any]:
        _identifier(namespace, "Namespace")
        _identifier(fact_id, "Fact ID")
        text = self._fact_text(text)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE facts SET text = ?, updated_at = ? WHERE namespace = ? AND fact_id = ?",
                (text, time.time(), namespace, fact_id)).rowcount
            if changed != 1:
                raise NotFoundError("Fact was not found in this namespace")
            return self._fact(connection.execute(
                "SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone())

    def delete_fact(self, namespace: str, fact_id: str) -> bool:
        _identifier(namespace, "Namespace")
        _identifier(fact_id, "Fact ID")
        with self._transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM facts WHERE namespace = ? AND fact_id = ?", (namespace, fact_id)
            ).rowcount == 1
            if deleted:
                connection.execute("DELETE FROM fact_uses WHERE fact_id = ?", (fact_id,))
            return deleted

    def list_facts(self, namespace: str, *, limit: int = 200) -> list[dict[str, Any]]:
        _identifier(namespace, "Namespace")
        with self._transaction(write=False) as connection:
            return [self._fact(row) for row in connection.execute(
                """SELECT * FROM facts WHERE namespace = ?
                   ORDER BY updated_at DESC, rowid DESC LIMIT ?""",
                (namespace, max(1, min(int(limit), _MAX_FACTS))))]

    @staticmethod
    def _terms(text: str) -> set[str]:
        terms = set()
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            if len(word) < 2 or word in _STOPWORDS:
                continue
            terms.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
        return terms

    def search_facts(self, namespace: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Keyword-overlap retrieval, best match first, newest first on ties."""
        _identifier(namespace, "Namespace")
        wanted = self._terms(query if type(query) is str else "")
        if not wanted:
            return []
        scored = []
        for fact in self.list_facts(namespace, limit=_MAX_FACTS):
            score = len(wanted & self._terms(fact["text"]))
            if score:
                scored.append((-score, -fact["updated_at"], fact["fact_id"], fact))
        scored.sort(key=lambda item: item[:3])
        return [item[3] for item in scored[: max(1, min(int(limit), 200))]]

    def append_run_event(self, owner: str, turn_id: str, event: Any) -> int:
        _identifier(owner, "Owner")
        _identifier(turn_id, "Turn ID")
        encoded = _encode_json(event)
        with self._transaction() as connection:
            seq = connection.execute(
                "SELECT coalesce(max(seq), 0) + 1 FROM run_events WHERE owner = ? AND turn_id = ?",
                (owner, turn_id)).fetchone()[0]
            connection.execute(
                """INSERT INTO run_events (owner, turn_id, seq, event_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""", (owner, turn_id, seq, encoded, time.time()))
            return seq

    def run_events(self, owner: str, turn_id: str, *, after: int = 0,
                   limit: int = 1000) -> list[tuple[int, Any]]:
        _identifier(owner, "Owner")
        _identifier(turn_id, "Turn ID")
        with self._transaction(write=False) as connection:
            return [(row["seq"], _decode_json(row["event_json"])) for row in connection.execute(
                """SELECT seq, event_json FROM run_events WHERE owner = ? AND turn_id = ?
                   AND seq > ? ORDER BY seq LIMIT ?""",
                (owner, turn_id, int(after), max(1, min(int(limit), 10000))))]

    # -- Long turns: the journal of turns, segments and children; background processes --

    @staticmethod
    def _step(row: sqlite3.Row) -> dict[str, Any]:
        return {"step_id": row["step_id"], "turn_id": row["turn_id"],
                "parent_id": row["parent_id"], "kind": row["kind"], "seq": row["seq"],
                "state": row["state"], "detail": _decode_json(row["detail_json"]),
                "result": None if row["result_json"] is None else _decode_json(row["result_json"]),
                "started_at": row["started_at"], "finished_at": row["finished_at"]}

    def begin_step(self, namespace: str, turn_id: str, kind: str, seq: int, detail: Any, *,
                   step_id: str | None = None, parent_id: str | None = None) -> str:
        """Journal a unit of a long turn as ``running`` before it has any effect."""
        _identifier(namespace, "Namespace")
        _identifier(turn_id, "Turn ID")
        if kind not in _STEP_KINDS or type(seq) is not int or seq < 0:
            raise StateError("Unknown journal step")
        if parent_id is not None:
            _identifier(parent_id, "Parent step")
        step_id = step_id or f"{kind}_{uuid.uuid4().hex}"
        _identifier(step_id, "Step ID")
        _encode_json(detail)
        with self._transaction() as connection:
            if kind == "segment" and isinstance(detail, dict):
                # Exact, clock-free attribution for recovery: one segment of a turn runs at
                # a time, so what the turn started after this count belongs to this segment.
                detail = {**detail, "receipts_before": connection.execute(
                    "SELECT count(*) FROM receipts WHERE turn_id = ? AND state <> 'denied'",
                    (turn_id,)).fetchone()[0]}
            encoded = _encode_json(detail)
            try:
                connection.execute(
                    """INSERT INTO turn_steps (step_id, turn_id, parent_id, namespace, kind, seq,
                       state, detail_json, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                    (step_id, turn_id, parent_id, namespace, kind, seq, encoded, time.time()))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("That journal step already exists") from exc
        return step_id

    def finish_step(self, step_id: str, state: str, result: Any = None, *,
                    detail: Any = None) -> None:
        """Settle a running step exactly once (``result`` and, optionally, final ``detail``)."""
        _identifier(step_id, "Step ID")
        if state not in _STEP_STATES or state == "running":
            raise StateError("Journal steps finish in a terminal state")
        encoded = None if result is None else _encode_json(result)
        with self._transaction() as connection:
            row = connection.execute("SELECT state FROM turn_steps WHERE step_id = ?",
                                     (step_id,)).fetchone()
            if row is None:
                raise NotFoundError("Journal step was not found")
            if row["state"] != "running":
                raise ConflictError("A journal step finishes exactly once")
            if detail is not None:
                connection.execute("UPDATE turn_steps SET detail_json = ? WHERE step_id = ?",
                                   (_encode_json(detail), step_id))
            connection.execute(
                """UPDATE turn_steps SET state = ?, result_json = ?, finished_at = ?
                   WHERE step_id = ? AND state = 'running'""",
                (state, encoded, time.time(), step_id))

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        _identifier(step_id, "Step ID")
        with self._transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM turn_steps WHERE step_id = ?",
                                     (step_id,)).fetchone()
            return None if row is None else self._step(row)

    @staticmethod
    def _tree(connection: sqlite3.Connection, turn_id: str) -> list[str]:
        """``turn_id`` and every descendant child turn, breadth first (bounded)."""
        tree, frontier = [turn_id], [turn_id]
        while frontier and len(tree) < 256:
            marks = ", ".join("?" for _ in frontier)
            frontier = [row["step_id"] for row in connection.execute(
                f"""SELECT step_id FROM turn_steps WHERE kind = 'child' AND turn_id IN ({marks})
                    ORDER BY started_at, seq""", frontier)]
            tree += [item for item in frontier if item not in tree]
        return tree

    def turn_tree(self, turn_id: str) -> list[str]:
        _identifier(turn_id, "Turn ID")
        with self._transaction(write=False) as connection:
            return self._tree(connection, turn_id)

    def journal(self, namespace: str, turn_id: str) -> dict[str, Any]:
        """The whole journal of one turn: its steps and those of every child turn, and the
        receipts of the turn and its children (each tagged with its own turn)."""
        _identifier(namespace, "Namespace")
        _identifier(turn_id, "Turn ID")
        with self._transaction(write=False) as connection:
            tree = self._tree(connection, turn_id)
            marks = ", ".join("?" for _ in tree)
            steps = [self._step(row) for row in connection.execute(
                f"""SELECT * FROM turn_steps WHERE namespace = ? AND turn_id IN ({marks})
                    ORDER BY started_at, rowid""", (namespace, *tree))]
            receipts = [self._receipt(row) for row in connection.execute(
                f"""SELECT * FROM receipts WHERE namespace = ? AND turn_id IN ({marks})
                    ORDER BY rowid LIMIT 2000""", (namespace, *tree))]
        return {"turn_id": turn_id, "tree": tree, "steps": steps, "receipts": receipts}

    def step_parents(self, child_ids: Any) -> dict[str, str]:
        """``{child turn: parent turn}`` for the given helper turns."""
        wanted = [str(item) for item in child_ids][:500]
        if not wanted:
            return {}
        marks = ", ".join("?" for _ in wanted)
        with self._transaction(write=False) as connection:
            return {row["step_id"]: row["turn_id"] for row in connection.execute(
                f"""SELECT step_id, turn_id FROM turn_steps WHERE kind = 'child'
                    AND step_id IN ({marks})""", wanted)}

    def list_journals(self, namespace: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Recent turn journals in ``namespace``, newest first."""
        _identifier(namespace, "Namespace")
        with self._transaction(write=False) as connection:
            return [self._step(row) for row in connection.execute(
                """SELECT * FROM turn_steps WHERE namespace = ? AND kind = 'turn'
                   ORDER BY started_at DESC, rowid DESC LIMIT ?""",
                (namespace, max(1, min(int(limit), 500))))]

    @staticmethod
    def _process(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in ("process_id", "turn_id", "call_id", "name", "command",
                                          "state", "pid", "exit_code", "output_bytes",
                                          "started_at", "finished_at")} | {
            "detail": None if row["detail_json"] is None else _decode_json(row["detail_json"])}

    def create_process(self, namespace: str, process_id: str, *, turn_id: str, call_id: str,
                       name: str, command: str, detail: Any = None) -> None:
        """Journal a background process as ``starting`` before it is launched."""
        for value, label in ((namespace, "Namespace"), (process_id, "Process ID"),
                             (turn_id, "Turn ID"), (call_id, "Call ID")):
            _identifier(value, label)
        _text(name, "Process name", 1024)
        _text(command, "Process command", _MAX_TEXT_BYTES)
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO processes (process_id, namespace, turn_id, call_id, name, command,
                   state, started_at, detail_json) VALUES (?, ?, ?, ?, ?, ?, 'starting', ?, ?)""",
                (process_id, namespace, turn_id, call_id, name, command, time.time(),
                 None if detail is None else _encode_json(detail)))

    def update_process(self, process_id: str, state: str, *, pid: int | None = None,
                       exit_code: int | None = None, output_bytes: int | None = None,
                       detail: Any = None) -> None:
        """Move a process forward (``starting`` -> ``running`` -> a final state), never back
        and never out of a final state."""
        _identifier(process_id, "Process ID")
        if state not in _PROCESS_STATES:
            raise StateError("Unknown process state")
        with self._transaction() as connection:
            row = connection.execute("SELECT state FROM processes WHERE process_id = ?",
                                     (process_id,)).fetchone()
            if row is None:
                raise NotFoundError("Process was not found")
            if row["state"] in _PROCESS_TERMINAL or (row["state"] == "running"
                                                     and state == "starting"):
                raise ConflictError("A finished process record is immutable")
            connection.execute(
                """UPDATE processes SET state = ?, pid = coalesce(?, pid),
                   exit_code = coalesce(?, exit_code), output_bytes = coalesce(?, output_bytes),
                   detail_json = coalesce(?, detail_json),
                   finished_at = CASE WHEN ? THEN ? ELSE finished_at END
                   WHERE process_id = ?""",
                (state, pid, exit_code, output_bytes,
                 None if detail is None else _encode_json(detail),
                 state in _PROCESS_TERMINAL, time.time(), process_id))

    def get_process(self, namespace: str | None, process_id: str) -> dict[str, Any] | None:
        _identifier(process_id, "Process ID")
        query, parameters = "SELECT * FROM processes WHERE process_id = ?", [process_id]
        if namespace is not None:
            _identifier(namespace, "Namespace")
            query += " AND namespace = ?"
            parameters.append(namespace)
        with self._transaction(write=False) as connection:
            row = connection.execute(query, parameters).fetchone()
            return None if row is None else self._process(row)

    def list_processes(self, namespace: str | None = None, *, active: bool = False,
                       limit: int = 50) -> list[dict[str, Any]]:
        query, parameters = "SELECT * FROM processes WHERE 1 = 1", []
        if namespace is not None:
            _identifier(namespace, "Namespace")
            query += " AND namespace = ?"
            parameters.append(namespace)
        if active:
            query += " AND state IN ('starting', 'running')"
        query += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        parameters.append(max(1, min(int(limit), 1000)))
        with self._transaction(write=False) as connection:
            return [self._process(row) for row in connection.execute(query, parameters)]

    @staticmethod
    def _recover_journal(connection: sqlite3.Connection, now: float) -> None:
        """Settle what a dead host left running in the long-turn journal, never rerunning it.

        A turn or child whose tree started any tool (a non-denied receipt) is ``uncertain``,
        otherwise ``failed``; a segment is ``uncertain`` when a tool of its turn started
        after it was dispatched (one segment of a turn runs at a time), otherwise
        ``failed``. The turn's chat gets the same state. Background processes of a dead
        host are ``lost`` (the lifeline killed their groups)."""
        def count(turns: list[str]) -> int:
            marks = ", ".join("?" for _ in turns)
            return connection.execute(
                f"SELECT count(*) FROM receipts WHERE turn_id IN ({marks}) "
                "AND state <> 'denied'", list(turns)).fetchone()[0]

        def started(turns: list[str]) -> bool:
            return count(turns) > 0

        rows = connection.execute(
            """SELECT step_id, turn_id, namespace, kind, started_at, detail_json FROM turn_steps
               WHERE state = 'running' ORDER BY started_at""").fetchall()
        for row in rows:
            if row["kind"] == "segment":
                before = _decode_json(row["detail_json"])
                before = before.get("receipts_before") if isinstance(before, dict) else None
                if isinstance(before, int) and not isinstance(before, bool):
                    touched = count([row["turn_id"]]) > before
                else:  # no count recorded: conservatively, anything the turn started
                    touched = started([row["turn_id"]])
            else:
                tree = Store._tree(connection, row["step_id"] if row["kind"] == "child"
                                   else row["turn_id"])
                touched = started(tree)
            state = "uncertain" if touched else "failed"
            connection.execute(
                """UPDATE turn_steps SET state = ?, finished_at = ?, result_json = ?
                   WHERE step_id = ? AND state = 'running'""",
                (state, now, _encode_json({"recovered": True, "tool_started": touched,
                                           "error": "The cell stopped while this was running; "
                                                    "it was not run again."}),
                 row["step_id"]))
            if row["kind"] == "turn":
                connection.execute(
                    """UPDATE chats SET state = ? WHERE owner = ? AND turn_id = ?
                       AND state = 'running'""", (state, row["namespace"], row["turn_id"]))
        connection.execute(
            """UPDATE processes SET state = 'lost', finished_at = ?
               WHERE state IN ('starting', 'running')""", (now,))

    # -- Durable scheduling: schedules with a next-fire index, occurrences ------------

    @staticmethod
    def _schedule(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schedule_id": row["schedule_id"], "name": row["name"], "state": row["state"],
            "spec": _decode_json(row["spec_json"]), "timezone": row["timezone"],
            "prompt": row["prompt"], "capabilities": _decode_json(row["capabilities_json"]),
            "missed_policy": row["missed_policy"], "overlap_policy": "skip",
            "next_fire_at": row["next_fire_at"], "run_now_at": row["run_now_at"],
            "namespace": row["namespace"], "workspace": row["workspace"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _occurrence(row: sqlite3.Row) -> dict[str, Any]:
        result = None if row["result_json"] is None else _decode_json(row["result_json"])
        missed = None
        if isinstance(result, dict) and "missed_count" in result:
            result = dict(result)
            missed = result.pop("missed_count")
            result = result or None
        return {
            "occurrence_id": row["occurrence_id"], "schedule_id": row["schedule_id"],
            "scheduled_at": row["scheduled_at"], "manual": bool(row["manual"]),
            "state": row["state"], "reason": row["reason"], "late_seconds": row["late_seconds"],
            "missed_from": row["missed_from"], "missed_count": missed, "turn_id": row["turn_id"],
            "claimed_at": row["claimed_at"], "finished_at": row["finished_at"],
            "result": result,
        }

    @staticmethod
    def _with_missed(result: Any, previous_json: Any) -> Any:
        """``result`` keeping the ``missed_count`` recorded when the occurrence was claimed."""
        previous = None if previous_json is None else _decode_json(previous_json)
        if not isinstance(previous, dict) or "missed_count" not in previous:
            return result
        return {**(result if isinstance(result, dict) else {}),
                "missed_count": previous["missed_count"]}

    @staticmethod
    def _schedule_values(changes: dict[str, Any]) -> dict[str, Any]:
        values = {}
        for key, value in changes.items():
            if key not in _SCHEDULE_CHANGES:
                raise StateError(f"Unknown schedule field {key!r}")
            if key in ("spec", "capabilities"):
                values[key + "_json"] = _encode_json(value)
            elif key == "state" and value not in _SCHEDULE_STATES:
                raise StateError("Unknown schedule state")
            elif key == "missed_policy" and value not in _MISSED_POLICIES:
                raise StateError("Unknown missed-run policy")
            elif key in ("name", "timezone", "prompt"):
                values[key] = _text(value, f"Schedule {key}", _MAX_TEXT_BYTES)
            elif key == "created_by":
                _identifier(value, "Schedule creator")
            elif key in ("next_fire_at", "run_now_at") and value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)):
                raise StateError(f"Schedule {key} must be a timestamp")
            if key not in ("spec", "capabilities", "name", "timezone", "prompt"):
                values[key] = value
        return values

    def create_schedule(self, namespace: str, **fields: Any) -> dict[str, Any]:
        _identifier(namespace, "Namespace")
        workspace = _text(fields.pop("workspace"), "Workspace", _MAX_ID_BYTES)
        created_by = _identifier(fields.pop("created_by"), "Schedule creator")
        values = self._schedule_values({"state": "active", **fields})
        schedule_id = "sch_" + uuid.uuid4().hex[:12]
        now = time.time()
        columns = ["schedule_id", "namespace", "workspace", "created_by", "created_at",
                   "updated_at", *values]
        with self._transaction() as connection:
            connection.execute(
                f"INSERT INTO schedules ({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                (schedule_id, namespace, workspace, created_by, now, now, *values.values()))
            return self._schedule(connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)).fetchone())

    def get_schedule(self, namespace: str | None, schedule_id: str) -> dict[str, Any]:
        """One schedule in ``namespace`` (``None``: any namespace, for the daemon)."""
        _identifier(schedule_id, "Schedule ID")
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ? AND (? IS NULL OR namespace = ?)",
                (schedule_id, namespace, namespace)).fetchone()
            if row is None:
                raise NotFoundError("Schedule was not found in this workspace")
            return self._schedule(row)

    def list_schedules(self, namespace: str | None = None, *,
                       include_removed: bool = False) -> list[dict[str, Any]]:
        with self._transaction(write=False) as connection:
            return [self._schedule(row) for row in connection.execute(
                """SELECT * FROM schedules WHERE (? IS NULL OR namespace = ?)
                   AND (? OR state <> 'removed') ORDER BY created_at, schedule_id""",
                (namespace, namespace, bool(include_removed)))]

    def update_schedule(self, namespace: str | None, schedule_id: str,
                        **changes: Any) -> dict[str, Any]:
        values = self._schedule_values(changes)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ? AND (? IS NULL OR namespace = ?)",
                (schedule_id, namespace, namespace)).fetchone()
            if row is None:
                raise NotFoundError("Schedule was not found in this workspace")
            if row["state"] == "removed":
                raise ConflictError("A removed schedule cannot change")
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE schedules SET {assignments}, updated_at = ? WHERE schedule_id = ?",
                (*values.values(), time.time(), schedule_id))
            return self._schedule(connection.execute(
                "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)).fetchone())

    def next_wake(self) -> float | None:
        """The earliest pending instant over every schedule: the next-fire index."""
        with self._transaction(write=False) as connection:
            return connection.execute(
                """SELECT min(t) FROM (
                     SELECT min(next_fire_at) AS t FROM schedules WHERE state = 'active'
                     UNION ALL SELECT min(run_now_at) FROM schedules WHERE state <> 'removed')"""
            ).fetchone()[0]

    def claim_due(self, now: float, plan, *, schedule_id: str | None = None) -> dict | None:
        """Atomically claim the earliest due schedule's next occurrence, or return None.

        ``plan(schedule, last_run, now)`` runs inside the write transaction on the fresh
        row and returns the occurrence (``scheduled_at``, ``manual``, ``state``, ``reason``,
        ``late_seconds``, ``missed_from``, ``missed_count``) plus the schedule's advanced
        ``next_fire_at``, ``schedule_state`` and whether the run-now request is consumed,
        or None to claim nothing. The occurrence id is derived from (schedule, instant,
        manual), so an instant is claimed at most once,
        even across restarts; a repeated claim only advances the schedule
        (``duplicate``: True).
        """
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT * FROM schedules WHERE state <> 'removed' AND (? IS NULL OR schedule_id = ?)
                   AND ((state = 'active' AND next_fire_at <= ?) OR run_now_at <= ?)
                   ORDER BY min(coalesce(run_now_at, next_fire_at), coalesce(next_fire_at,
                   run_now_at)) LIMIT 1""", (schedule_id, schedule_id, now, now)).fetchone()
            if row is None:
                return None
            schedule = self._schedule(row)
            last = connection.execute(
                """SELECT * FROM occurrences WHERE schedule_id = ? AND state <> 'skipped'
                   ORDER BY claimed_at DESC LIMIT 1""", (schedule["schedule_id"],)).fetchone()
            decision = plan(schedule, None if last is None else self._occurrence(last), now)
            if decision is None:
                return None
            instant, manual = int(decision["scheduled_at"]), bool(decision["manual"])
            occurrence_id = f"occ_{schedule['schedule_id']}_{instant}{'_m' if manual else ''}"
            existing = connection.execute(
                "SELECT * FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if existing is None:
                result = dict(decision.get("result") or {})
                if decision.get("missed_count") is not None:
                    result["missed_count"] = int(decision["missed_count"])
                connection.execute(
                    """INSERT INTO occurrences (occurrence_id, schedule_id, namespace,
                       scheduled_at, manual, state, reason, late_seconds, missed_from,
                       claimed_at, finished_at, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (occurrence_id, schedule["schedule_id"], schedule["namespace"], instant,
                     int(manual), decision["state"], decision.get("reason"),
                     decision.get("late_seconds"), decision.get("missed_from"), now,
                     None if decision["state"] == "running" else now,
                     _encode_json(result) if result else None))
            changes = {"next_fire_at": decision["next_fire_at"],
                       "state": decision["schedule_state"]}
            if decision.get("consume_run_now"):
                changes["run_now_at"] = None
            values = self._schedule_values(changes)
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(f"UPDATE schedules SET {assignments} WHERE schedule_id = ?",
                               (*values.values(), schedule["schedule_id"]))
            occurrence = self._occurrence(connection.execute(
                "SELECT * FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone())
            occurrence["duplicate"] = existing is not None
            occurrence["schedule"] = schedule
            return occurrence

    def finish_occurrence(self, occurrence_id: str, state: str, *, turn_id: str | None = None,
                          result: Any = None, now: float | None = None) -> dict[str, Any]:
        _identifier(occurrence_id, "Occurrence ID")
        if state not in _OCCURRENCE_TERMINAL:
            raise StateError("Occurrences finish in a terminal state")
        if result is not None:
            _encode_json(result)  # refuse an unstorable result before touching the row
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM occurrences WHERE occurrence_id = ? AND state = 'running'",
                (occurrence_id,)).fetchone()
            if row is None:
                raise ConflictError("An occurrence finishes exactly once, from running")
            merged = self._with_missed(result, row["result_json"])
            connection.execute(
                """UPDATE occurrences SET state = ?, turn_id = ?, result_json = ?, finished_at = ?
                   WHERE occurrence_id = ? AND state = 'running'""",
                (state, turn_id, None if merged is None else _encode_json(merged),
                 time.time() if now is None else now, occurrence_id))
            return self._occurrence(connection.execute(
                "SELECT * FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone())

    def get_occurrence(self, occurrence_id: str) -> dict[str, Any] | None:
        _identifier(occurrence_id, "Occurrence ID")
        with self._transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM occurrences WHERE occurrence_id = ?",
                                     (occurrence_id,)).fetchone()
            return None if row is None else self._occurrence(row)

    def list_occurrences(self, namespace: str | None = None, *, schedule_id: str | None = None,
                         states: tuple[str, ...] | None = None,
                         limit: int = 50) -> list[dict[str, Any]]:
        """Newest first; ``namespace`` None spans every workspace (daemon status)."""
        query = ("SELECT * FROM occurrences WHERE (? IS NULL OR namespace = ?)"
                 " AND (? IS NULL OR schedule_id = ?)")
        parameters: list[Any] = [namespace, namespace, schedule_id, schedule_id]
        if states:
            query += f" AND state IN ({', '.join('?' for _ in states)})"
            parameters.extend(states)
        query += " ORDER BY claimed_at DESC, occurrence_id DESC LIMIT ?"
        parameters.append(max(1, min(int(limit), 1000)))
        with self._transaction(write=False) as connection:
            return [self._occurrence(row) for row in connection.execute(query, parameters)]

    # -- The learning cell: skills (every version retained), fact usage, past turns ------

    @staticmethod
    def skill_name(name: Any) -> str:
        if type(name) is not str or _SKILL_NAME.fullmatch(name) is None:
            raise StateError("Skill names are 1-64 lowercase letters, digits and hyphens")
        return name

    @staticmethod
    def _skill_fields(description: Any, when_to_use: Any, steps: Any) -> tuple[str, str, str]:
        for value, label, limit in ((description, "Skill description", 300),
                                    (when_to_use, "Skill when_to_use", 600)):
            if type(value) is not str or not value.strip() or len(value) > limit:
                raise StateError(f"{label} must be non-empty text of at most {limit} characters")
        if (type(steps) is not list or not 1 <= len(steps) <= 40
                or any(type(step) is not str or not step.strip() or len(step) > 1000
                       for step in steps) or sum(len(step) for step in steps) > 12000):
            raise StateError("Skill steps must be 1-40 non-empty steps of at most 1,000 "
                             "characters (12,000 in total)")
        return (" ".join(description.split()), " ".join(when_to_use.split()),
                _encode_json([step.strip() for step in steps]))

    @staticmethod
    def _skill_view(connection: sqlite3.Connection, row: sqlite3.Row,
                    version: int | None = None) -> dict[str, Any]:
        chosen = row["version"] if version is None else version
        found = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
            (row["skill_id"], chosen)).fetchone()
        if found is None:
            raise NotFoundError("That skill version does not exist")
        return {
            "skill_id": row["skill_id"], "scope": row["scope"], "name": row["name"],
            "state": row["state"], "review": row["review"], "version": row["version"],
            "pending": row["pending"], "created_by": row["created_by"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "uses": row["uses"], "last_used_at": row["last_used_at"],
            "shown_version": chosen, "description": found["description"],
            "when_to_use": found["when_to_use"], "steps": _decode_json(found["steps_json"]),
            "note": found["note"], "author": found["author"], "version_review": found["review"],
            "tainted": bool(found["tainted"]), "session_id": found["session_id"],
            "turn_id": found["turn_id"], "workspace": found["workspace"],
            "version_created_at": found["created_at"],
        }

    def save_skill(self, scope: str, name: str, *, description: Any, when_to_use: Any,
                   steps: Any, author: str, review: str, tainted: bool = False,
                   note: str | None = None, session_id: str | None = None,
                   turn_id: str | None = None, workspace: str | None = None,
                   pending: bool = False, allow_disabled: bool = False) -> dict[str, Any]:
        """Create a skill or add a version; every earlier version is kept.

        ``pending`` (and any non-owner save to a quarantined skill) stores the version for
        the owner's review without changing what is offered. A disabled skill only changes
        with ``allow_disabled`` (the owner)."""
        _identifier(scope, "Skill scope")
        self.skill_name(name)
        _identifier(author, "Skill author")
        if review not in _SKILL_REVIEWS:
            raise StateError("Unknown skill review state")
        description, when_to_use, steps_json = self._skill_fields(description, when_to_use, steps)
        if note is not None:
            note = _text(note, "Skill change note", 2000, blank=True)[:500] or None
        _no_credentials(description, when_to_use, *steps, note or "")
        for value, label in ((session_id, "Session ID"), (turn_id, "Turn ID")):
            if value is not None:
                _identifier(value, label)
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM skills WHERE scope = ? AND name = ?",
                                     (scope, name)).fetchone()
            if row is None:
                if connection.execute("SELECT count(*) FROM skills WHERE scope = ?",
                                      (scope,)).fetchone()[0] >= _MAX_SKILLS:
                    raise ConflictError("This skill scope is full")
                skill_id = "skill_" + uuid.uuid4().hex[:16]
                review = "quarantined" if pending else review
                connection.execute(
                    """INSERT INTO skills (skill_id, scope, name, state, review, version,
                       created_by, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, 1, ?, ?, ?)""",
                    (skill_id, scope, name, review, author, now, now))
                version, outcome = 1, "created"
            else:
                skill_id = row["skill_id"]
                if row["state"] == "disabled" and not allow_disabled:
                    raise ConflictError("The owner disabled this skill; it cannot be changed "
                                        "from a conversation")
                version = connection.execute(
                    "SELECT max(version) FROM skill_versions WHERE skill_id = ?",
                    (skill_id,)).fetchone()[0] + 1
                if version > _MAX_SKILL_VERSIONS:
                    raise ConflictError("This skill has too many versions; delete it or edit it "
                                        "as the owner")
                if pending or (row["review"] == "quarantined" and author != "owner"):
                    review, outcome = "quarantined", "pending"
                    connection.execute("UPDATE skills SET pending = ? WHERE skill_id = ?",
                                       (version, skill_id))
                else:
                    outcome = "updated"
                    connection.execute(
                        """UPDATE skills SET version = ?, review = ?, updated_at = ?
                           WHERE skill_id = ?""", (version, review, now, skill_id))
            connection.execute(
                """INSERT INTO skill_versions (skill_id, version, description, when_to_use,
                   steps_json, note, author, review, tainted, session_id, turn_id, workspace,
                   created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (skill_id, version, description, when_to_use, steps_json, note, author, review,
                 int(bool(tainted)), session_id, turn_id, workspace, now))
            row = connection.execute("SELECT * FROM skills WHERE skill_id = ?",
                                     (skill_id,)).fetchone()
            view = self._skill_view(connection, row, version)
            view["outcome"] = outcome
            return view

    def get_skill(self, scopes: Any, name: str, *, version: int | None = None) -> dict | None:
        """The named skill from the first scope that has it (``version``: that version)."""
        scopes = [scopes] if isinstance(scopes, str) else list(scopes)
        with self._transaction(write=False) as connection:
            for scope in scopes:
                row = connection.execute("SELECT * FROM skills WHERE scope = ? AND name = ?",
                                         (scope, name)).fetchone()
                if row is not None:
                    return self._skill_view(connection, row, version)
        return None

    def list_skills(self, scopes: Any, *, offered: bool = False) -> list[dict[str, Any]]:
        """Skills in ``scopes`` (a name in an earlier scope hides the same name later);
        ``offered``: only those a turn may see (active, not quarantined)."""
        scopes = [scopes] if isinstance(scopes, str) else list(scopes)
        seen: set[str] = set()
        result = []
        with self._transaction(write=False) as connection:
            for scope in scopes:
                for row in connection.execute(
                        "SELECT * FROM skills WHERE scope = ? ORDER BY name", (scope,)).fetchall():
                    if row["name"] in seen:
                        continue
                    seen.add(row["name"])
                    if offered and (row["state"] != "active" or row["review"] == "quarantined"):
                        continue
                    result.append(self._skill_view(connection, row))
        return result

    def skill_history(self, skill_id: str) -> list[dict[str, Any]]:
        _identifier(skill_id, "Skill ID")
        with self._transaction(write=False) as connection:
            return [{"version": row["version"], "description": row["description"],
                     "when_to_use": row["when_to_use"], "steps": _decode_json(row["steps_json"]),
                     "note": row["note"], "author": row["author"], "review": row["review"],
                     "tainted": bool(row["tainted"]), "session_id": row["session_id"],
                     "turn_id": row["turn_id"], "workspace": row["workspace"],
                     "created_at": row["created_at"]}
                    for row in connection.execute(
                        "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY version",
                        (skill_id,))]

    def _owned_skill(self, connection: sqlite3.Connection, skill_id: str) -> sqlite3.Row:
        _identifier(skill_id, "Skill ID")
        row = connection.execute("SELECT * FROM skills WHERE skill_id = ?", (skill_id,)).fetchone()
        if row is None:
            raise NotFoundError("Skill was not found")
        return row

    def set_skill(self, skill_id: str, *, state: str | None = None,
                  scope: str | None = None) -> dict[str, Any]:
        """Owner changes: enable/disable, or move to another scope (for example the profile)."""
        with self._transaction() as connection:
            row = self._owned_skill(connection, skill_id)
            if state is not None:
                if state not in _SKILL_STATES:
                    raise StateError("Unknown skill state")
                connection.execute("UPDATE skills SET state = ?, updated_at = ? WHERE skill_id = ?",
                                   (state, time.time(), skill_id))
            if scope is not None and scope != row["scope"]:
                _identifier(scope, "Skill scope")
                if connection.execute("SELECT 1 FROM skills WHERE scope = ? AND name = ?",
                                      (scope, row["name"])).fetchone() is not None:
                    raise ConflictError("A skill with that name already exists in that scope")
                connection.execute("UPDATE skills SET scope = ?, updated_at = ? WHERE skill_id = ?",
                                   (scope, time.time(), skill_id))
            return self._skill_view(connection, self._owned_skill(connection, skill_id))

    def approve_skill(self, skill_id: str, *, version: int | None = None) -> dict[str, Any]:
        """Approve the offered (current) version, or exactly ``version``: the current one or the
        pending one. A pending (quarantined) version is approved only when it is named, never
        as a side effect; one proposed before the current version (a stale proposal, for
        example from before an owner edit) is refused, since approving it would undo that
        edit. Approval never changes whether the skill is enabled."""
        if version is not None and (type(version) is not int or version < 1):
            raise StateError("A skill version is a positive integer")
        with self._transaction() as connection:
            row = self._owned_skill(connection, skill_id)
            chosen = row["version"] if version is None else version
            if chosen == row["pending"]:
                if row["pending"] < row["version"]:
                    raise ConflictError(
                        f"Version {chosen} was proposed before the current version "
                        f"{row['version']}, so approving it would undo later changes; reject "
                        "it, or edit the skill.")
                connection.execute(
                    """UPDATE skills SET version = ?, pending = NULL, review = 'approved',
                       updated_at = ? WHERE skill_id = ?""", (chosen, time.time(), skill_id))
            elif chosen == row["version"]:
                connection.execute(
                    "UPDATE skills SET review = 'approved', updated_at = ? WHERE skill_id = ?",
                    (time.time(), skill_id))
            else:
                raise ConflictError(
                    f"Version {chosen} is neither the current version ({row['version']}) nor a "
                    "pending one, so it cannot be approved.")
            connection.execute(
                "UPDATE skill_versions SET review = 'approved' WHERE skill_id = ? AND version = ?",
                (skill_id, chosen))
            return self._skill_view(connection, self._owned_skill(connection, skill_id))

    def reject_pending_skill(self, skill_id: str) -> dict[str, Any]:
        """Discard a pending version (its text is deleted); the offered version is unchanged."""
        with self._transaction() as connection:
            row = self._owned_skill(connection, skill_id)
            if row["pending"] is None:
                raise ConflictError("This skill has no pending version")
            connection.execute("DELETE FROM skill_versions WHERE skill_id = ? AND version = ?",
                               (skill_id, row["pending"]))
            connection.execute("UPDATE skills SET pending = NULL WHERE skill_id = ?", (skill_id,))
            return self._skill_view(connection, self._owned_skill(connection, skill_id))

    def delete_skill(self, skill_id: str) -> bool:
        """Delete the skill and every version's text."""
        _identifier(skill_id, "Skill ID")
        with self._transaction() as connection:
            connection.execute("DELETE FROM skill_versions WHERE skill_id = ?", (skill_id,))
            return connection.execute("DELETE FROM skills WHERE skill_id = ?",
                                      (skill_id,)).rowcount == 1

    def note_skill_use(self, skill_id: str) -> None:
        _identifier(skill_id, "Skill ID")
        with self._transaction() as connection:
            connection.execute(
                "UPDATE skills SET uses = uses + 1, last_used_at = ? WHERE skill_id = ?",
                (time.time(), skill_id))

    def note_fact_uses(self, fact_ids: Any) -> None:
        now = time.time()
        with self._transaction() as connection:
            for fact_id in list(dict.fromkeys(fact_ids))[:200]:
                _identifier(fact_id, "Fact ID")
                if connection.execute("SELECT 1 FROM facts WHERE fact_id = ?",
                                      (fact_id,)).fetchone() is None:
                    continue
                connection.execute(
                    """INSERT INTO fact_uses (fact_id, uses, last_used_at) VALUES (?, 1, ?)
                       ON CONFLICT (fact_id) DO UPDATE SET uses = uses + 1, last_used_at = ?""",
                    (fact_id, now, now))

    def fact_uses(self, namespace: str) -> dict[str, int]:
        _identifier(namespace, "Namespace")
        with self._transaction(write=False) as connection:
            return {row["fact_id"]: row["uses"] for row in connection.execute(
                """SELECT u.fact_id, u.uses FROM fact_uses u JOIN facts f
                   ON f.fact_id = u.fact_id WHERE f.namespace = ?""", (namespace,))}

    def _turn(self, row: sqlite3.Row) -> dict[str, Any]:
        response = _decode_json(row["response_json"])
        return {"ordinal": row["ordinal"], "turn_id": row["turn_id"],
                "session_id": row["session_id"], "user_input": row["user_input"],
                "response": response["response"], "started_at": row["started_at"],
                "finished_at": row["finished_at"]}

    def list_turns(self, namespace: str, *, limit: int = 2000,
                   turn_ids: Any = None) -> list[dict[str, Any]]:
        """Succeeded turns in ``namespace`` (inputs, responses, times), newest first; forgotten
        turns are not listed."""
        _identifier(namespace, "Namespace")
        query = ("""SELECT c.ordinal, c.turn_id, c.session_id, c.user_input, c.response_json,
                    t.started_at, t.finished_at FROM chats c LEFT JOIN turn_log t
                    ON t.turn_id = c.turn_id WHERE c.owner = ? AND c.state = 'succeeded'
                    AND c.user_input <> ?""")
        parameters: list[Any] = [namespace, FORGOTTEN]
        if turn_ids is not None:
            wanted = [str(item) for item in turn_ids][:500]
            if not wanted:
                return []
            query += f" AND c.turn_id IN ({', '.join('?' for _ in wanted)})"
            parameters.extend(wanted)
        query += " ORDER BY c.ordinal DESC LIMIT ?"
        parameters.append(max(1, min(int(limit), 100_000)))
        with self._transaction(write=False) as connection:
            return [self._turn(row) for row in connection.execute(query, parameters)]

    def count_turns(self, namespace: str) -> int:
        _identifier(namespace, "Namespace")
        with self._transaction(write=False) as connection:
            return connection.execute(
                """SELECT count(*) FROM chats WHERE owner = ? AND state = 'succeeded'
                   AND user_input <> ?""", (namespace, FORGOTTEN)).fetchone()[0]

    def forget_session(self, namespace: str, session_id: str) -> dict[str, Any]:
        """Forget one session: its turns keep their rows (replay keys, states, receipts'
        tools) but lose the owner's words, the answers, the answers its scheduled runs left
        in the inbox, their receipts' arguments and results, and any streamed events."""
        _identifier(namespace, "Namespace")
        _identifier(session_id, "Session ID")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT turn_id, state FROM chats WHERE owner = ? AND session_id = ? ORDER BY "
                "ordinal", (namespace, session_id)).fetchall()
            if not rows:
                raise NotFoundError("No session with that id in this workspace")
            if any(row["state"] in ("reserved", "running") for row in rows):
                raise ConflictError("That session has a turn in progress; forget it once the "
                                    "turn has finished")
            turns = [row["turn_id"] for row in rows]
            # A turn's journal (segments, children and their own turns) goes with it.
            tree = list(turns)
            for turn in turns:
                tree += [item for item in self._tree(connection, turn) if item not in tree]
            marks = ", ".join("?" for _ in turns)
            tree_marks = ", ".join("?" for _ in tree)
            blank = _response_json({"response": FORGOTTEN, "agent_logs": [],
                                    "session_id": session_id}, session_id)
            connection.execute(
                """UPDATE chats SET user_input = ?, response_json = CASE WHEN response_json IS
                   NULL THEN NULL ELSE ? END WHERE owner = ? AND session_id = ?""",
                (FORGOTTEN, blank, namespace, session_id))
            answers = 0
            for row in connection.execute(
                    f"""SELECT occurrence_id, result_json FROM occurrences WHERE namespace = ?
                        AND turn_id IN ({marks})""", (namespace, *turns)).fetchall():
                result = None if row["result_json"] is None else _decode_json(row["result_json"])
                if isinstance(result, dict) and result.get("response") is not None:
                    connection.execute(
                        "UPDATE occurrences SET result_json = ? WHERE occurrence_id = ?",
                        (_encode_json({**result, "response": FORGOTTEN}), row["occurrence_id"]))
                    answers += 1
            receipts = 0
            for row in connection.execute(
                    f"""SELECT receipt_id, result_json FROM receipts WHERE namespace = ?
                        AND turn_id IN ({tree_marks})""", (namespace, *tree)).fetchall():
                result = None if row["result_json"] is None else _decode_json(row["result_json"])
                kept = None if result is None else _encode_json(
                    {"ok": result.get("ok") if isinstance(result, dict) else None,
                     "forgotten": True})
                connection.execute(
                    "UPDATE receipts SET request_json = ?, result_json = ? WHERE receipt_id = ?",
                    (_encode_json({"forgotten": True}), kept, row["receipt_id"]))
                receipts += 1
            connection.execute(
                f"""UPDATE turn_steps SET detail_json = ?, result_json = CASE WHEN result_json
                    IS NULL THEN NULL ELSE ? END WHERE namespace = ? AND turn_id IN ({tree_marks})""",
                (_encode_json({"forgotten": True}), _encode_json({"forgotten": True}), namespace,
                 *tree))
            connection.execute(
                f"""UPDATE processes SET command = ?, name = ? WHERE namespace = ?
                    AND turn_id IN ({tree_marks})""", (FORGOTTEN, FORGOTTEN, namespace, *tree))
            events = connection.execute(
                f"DELETE FROM run_events WHERE owner = ? AND turn_id IN ({marks})",
                (namespace, *turns)).rowcount
        return {"session_id": session_id, "turns": turns, "scheduled_answers": answers,
                "receipts": receipts, "events": events}

    def session_turns(self, namespace: str, session_id: str, *,
                      limit: int = 50) -> list[dict[str, Any]]:
        """Every turn of one session in any state (oldest first, the newest ``limit``): the
        owner's words, the stored state and, only for succeeded turns, the answer.
        Read-only; the companion and ``sessions show`` render it."""
        _identifier(namespace, "Namespace")
        _identifier(session_id, "Session ID")
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT c.ordinal, c.turn_id, c.state, c.user_input, c.response_json,
                   t.started_at, t.finished_at FROM chats c LEFT JOIN turn_log t
                   ON t.turn_id = c.turn_id WHERE c.owner = ? AND c.session_id = ?
                   ORDER BY c.ordinal DESC LIMIT ?""",
                (namespace, session_id, max(1, min(int(limit), 500)))).fetchall()
        turns = []
        for row in reversed(rows):
            response = None if row["response_json"] is None else _decode_json(row["response_json"])
            turns.append({"ordinal": row["ordinal"], "turn_id": row["turn_id"],
                          "state": row["state"], "user_input": row["user_input"],
                          "response": response["response"] if row["state"] == "succeeded"
                          and isinstance(response, dict) else None,
                          "started_at": row["started_at"], "finished_at": row["finished_at"]})
        return turns

    # -- operations: retention, statistics and compaction ------------------------------

    @staticmethod
    def _provenance_turns(connection: sqlite3.Connection) -> set[str]:
        """Turns whose receipts a schedule's taint provenance reads (the conversation that
        wrote a schedule's prompt and the turns above it): retention never prunes them."""
        found: set[str] = set()
        for (creator,) in connection.execute(
                "SELECT DISTINCT created_by FROM schedules WHERE created_by LIKE 'turn:%'"):
            turn, level = creator[5:], 0
            while turn and turn not in found and level < 8:
                found.add(turn)
                row = connection.execute("SELECT turn_id FROM turn_steps WHERE kind = 'child' "
                                         "AND step_id = ?", (turn,)).fetchone()
                turn, level = (row[0] if row else None), level + 1
        return found

    def retention(self, *, receipts_before: float | None = None,
                  run_events_before: float | None = None, inbox_before: float | None = None,
                  inbox_keep: int = 20, dry_run: bool = True) -> dict[str, Any]:
        """Records past their cutoff: finished receipts (never those of a turn in progress or
        of a schedule's provenance), streamed run events of finished turns, and finished inbox
        entries (each schedule keeps its newest ``inbox_keep``, at least one). ``dry_run``
        only counts; otherwise everything chosen is deleted in one transaction."""
        keep = max(1, int(inbox_keep))
        report: dict[str, Any] = {}
        with self._transaction(write=not dry_run) as connection:
            active = {row[0] for row in connection.execute(
                "SELECT turn_id FROM chats WHERE state IN ('reserved', 'running')")}
            protected = self._provenance_turns(connection)
            plans: dict[str, tuple[str, str, list]] = {}
            if receipts_before is not None:
                rows = connection.execute(
                    """SELECT receipt_id, turn_id, created_at FROM receipts WHERE state <> 'started'
                       AND created_at < ?""", (receipts_before,)).fetchall()
                chosen = [row for row in rows if row["turn_id"] not in protected
                          and row["turn_id"] not in active]
                plans["receipts"] = ("receipts", "receipt_id", chosen)
                report["receipts"] = {"cutoff": receipts_before,
                                      "protected": len(rows) - len(chosen)}
            if run_events_before is not None:
                rows = connection.execute(
                    """SELECT rowid AS id, turn_id, created_at FROM run_events
                       WHERE created_at < ?""", (run_events_before,)).fetchall()
                chosen = [row for row in rows if row["turn_id"] not in active]
                plans["run_events"] = ("run_events", "rowid", chosen)
                report["run_events"] = {"cutoff": run_events_before,
                                        "protected": len(rows) - len(chosen)}
            if inbox_before is not None:
                rows = connection.execute(
                    """SELECT occurrence_id AS id, created_at FROM (
                         SELECT occurrence_id, state, coalesce(finished_at, claimed_at)
                                AS created_at, ROW_NUMBER() OVER (PARTITION BY schedule_id
                                ORDER BY claimed_at DESC) AS rank FROM occurrences)
                       WHERE state <> 'running' AND rank > ? AND created_at < ?""",
                    (keep, inbox_before)).fetchall()
                plans["inbox"] = ("occurrences", "occurrence_id", rows)
                report["inbox"] = {"cutoff": inbox_before, "keep_per_schedule": keep}
            for name, (table, key, rows) in plans.items():
                times = [row["created_at"] for row in rows]
                report[name].update(count=len(rows), oldest=min(times, default=None),
                                    newest=max(times, default=None))
                if dry_run:
                    continue
                ids = [row[0] for row in rows]
                for start in range(0, len(ids), 500):
                    batch = ids[start:start + 500]
                    connection.execute(f"DELETE FROM {table} WHERE {key} IN "
                                       f"({', '.join('?' for _ in batch)})", batch)
                report[name]["deleted"] = len(ids)
        return report

    def operations_rows(self, *, since: float | None = None, until: float | None = None,
                        namespace: str | None = None) -> dict[str, Any]:
        """The raw rows local statistics are computed from (turns, receipts, scheduled runs
        and helpers in a time window), newest first and bounded."""
        low, high = (-math.inf if since is None else since), (math.inf if until is None else until)
        scope, parameters = ("", []) if namespace is None else (" AND c.owner = ?", [namespace])
        with self._transaction(write=False) as connection:
            turns = [dict(row) for row in connection.execute(
                f"""SELECT c.turn_id, c.state, c.idempotency_key, t.started_at, t.finished_at,
                    s.state AS journal_state, s.result_json AS journal_json
                    FROM chats c LEFT JOIN turn_log t ON t.turn_id = c.turn_id
                    LEFT JOIN turn_steps s ON s.step_id = 'journal_' || c.turn_id
                    WHERE coalesce(t.started_at, 0) >= ? AND coalesce(t.started_at, 0) < ?{scope}
                    ORDER BY c.ordinal DESC LIMIT 100000""",
                [0 if since is None else low, high, *parameters])]
            for row in turns:
                journal = _decode_json(row.pop("journal_json")) if row.get("journal_json") else None
                row["segments"] = journal.get("segments") if isinstance(journal, dict) else None
            where = "" if namespace is None else " AND namespace = ?"
            receipts = [dict(row) for row in connection.execute(
                f"""SELECT tool, state, created_at, finished_at FROM receipts
                    WHERE created_at >= ? AND created_at < ?{where}
                    ORDER BY created_at DESC LIMIT 200000""", [low, high, *parameters])]
            runs = []
            for row in connection.execute(
                    f"""SELECT schedule_id, state, reason, manual, scheduled_at, claimed_at,
                        finished_at, late_seconds, result_json FROM occurrences
                        WHERE claimed_at >= ? AND claimed_at < ?{where}
                        ORDER BY claimed_at DESC LIMIT 100000""", [low, high, *parameters]):
                item = dict(row)
                result = _decode_json(item.pop("result_json")) if row["result_json"] else None
                item["missed_count"] = result.get("missed_count") if isinstance(result, dict) \
                    else None
                runs.append(item)
            helpers = connection.execute(
                f"""SELECT count(*) FROM turn_steps WHERE kind = 'child' AND started_at >= ?
                    AND started_at < ?{where}""", [low, high, *parameters]).fetchone()[0]
        return {"turns": turns, "receipts": receipts, "runs": runs, "helpers": helpers}

    def export_turns(self, namespace: str) -> list[dict[str, Any]]:
        """Every turn of ``namespace`` in order (portable export): session, state, the owner's
        words, the answer when it succeeded, and times. Forgotten turns are left out."""
        _identifier(namespace, "Namespace")
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT c.turn_id, c.session_id, c.state, c.user_input, c.response_json,
                   t.started_at, t.finished_at FROM chats c LEFT JOIN turn_log t
                   ON t.turn_id = c.turn_id WHERE c.owner = ? AND c.user_input <> ?
                   ORDER BY c.ordinal""", (namespace, FORGOTTEN)).fetchall()
        turns = []
        for row in rows:
            response = _decode_json(row["response_json"]) if row["response_json"] else None
            turns.append({"turn_id": row["turn_id"], "session_id": row["session_id"],
                          "state": row["state"], "user_input": row["user_input"],
                          "response": response["response"] if isinstance(response, dict)
                          else None, "started_at": row["started_at"],
                          "finished_at": row["finished_at"]})
        return turns

    def table_counts(self) -> dict[str, int]:
        with self._transaction(write=False) as connection:
            return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("chats", "facts", "skills", "schedules", "occurrences",
                                  "receipts", "run_events", "turn_steps")}

    def compact(self) -> dict[str, Any]:
        """VACUUM the store (the caller holds the home exclusively), then check it."""
        with self._lock:
            connection = self._open_connection()
            before = self._path.stat().st_size
            page = connection.execute("PRAGMA page_size").fetchone()[0]
            free = connection.execute("PRAGMA freelist_count").fetchone()[0]
            started = time.monotonic()
            try:
                connection.execute("VACUUM")
                check = connection.execute("PRAGMA quick_check").fetchone()[0]
            except sqlite3.Error as error:
                raise StateError(f"The store could not be compacted: {error}") from None
            after = self._path.stat().st_size
        return {"bytes_before": before, "bytes_after": after, "free_pages_before": free,
                "page_size": page, "reclaimed_bytes": max(0, before - after),
                "quick_check": check, "seconds": round(time.monotonic() - started, 3)}
