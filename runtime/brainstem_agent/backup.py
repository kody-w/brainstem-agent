"""Backup, restore and portable export of a cell's durable state.

A backup is a directory (0700, files 0600):

    manifest.json          format, time, version, store schema and counts, every file's
                           SHA-256 and size, and what was excluded and why
    state/agent.sqlite3    the store, copied with SQLite's online backup API (consistent while
                           the daemon runs): conversations, memory and profile, skills,
                           schedules and the inbox, receipts and turn journals
    state/mcp-pins.json    the owner's pinned MCP tool definitions (if any)
    reach.json             MCP servers and web egress policy, secrets removed by default
    operations.json        the owner's operations policy (if any)

Never included: the Copilot credential (the cell never stores it), the daemon's bearer token
(``run/``), worker trees, caches, logs and the derived search index (rebuilt from the store).
``restore`` verifies every digest before it writes anything and refuses on any mismatch.
``export`` writes skills, memory, profile and sessions in portable formats (markdown and JSON
lines) documented in ``EXPORT_FORMATS``.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import state
from .credentials import credential_kinds
from .paths import canonical

__all__ = ["BackupError", "EXPORT_FORMATS", "create_backup", "export_data", "restore_backup",
           "sanitize_reach", "verify_backup"]

FORMAT = "brainstem-agent-backup/1"
EXPORT_FORMAT = "brainstem-agent-export/1"
MANIFEST = "manifest.json"
STORE = "state/agent.sqlite3"
_OPTIONAL = ("state/mcp-pins.json", "operations.json")
ALWAYS_EXCLUDED = [
    {"item": "the Copilot credential", "reason": "never stored by Brainstem Agent; after a "
     "restore it is read again from the installed RAPP Brainstem"},
    {"item": "run/", "reason": "the daemon's bearer token and runtime records (made anew)"},
    {"item": "cache/, workers/", "reason": "the verified Grail source and worker interpreter "
     "(rebuilt by setup) and temporary worker trees"},
    {"item": "logs/", "reason": "operational logs"},
    {"item": "state/search.sqlite3", "reason": "derived session index (rebuilt from the store)"},
    {"item": "workspaces/", "reason": "your workspace files: back up project folders with your "
     "usual tools"},
]
EXPORT_FORMATS = {
    "skills/<scope>-<name>.md": "markdown with YAML-style frontmatter (name, description, "
                                "when_to_use, version, review, state, scope, author, created_at)"
                                " and a numbered '## Steps' list; importable with skills import",
    "memory.jsonl": "one JSON object per workspace fact: fact_id, scope, text, created_at, "
                    "updated_at (epoch seconds), source_turn",
    "profile.jsonl": "one JSON object per profile fact, same fields, scope 'profile'",
    "sessions/<session_id>.jsonl": "one JSON object per message in order: turn_id, role (user or "
                                   "assistant), content, state, at (epoch seconds)",
}


class BackupError(RuntimeError):
    """A backup, restore or export was refused or failed; nothing half-written is kept."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp(moment: float | None = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(moment))


def _iso(moment: float) -> str:
    return dt.datetime.fromtimestamp(moment, dt.timezone.utc).isoformat(timespec="seconds")


def _owner_only_dirs(path: Path) -> None:
    """Create ``path`` and its missing parents 0700 each (``mkdir(parents=True)`` would give
    the parents the default mode); directories that already exist are never changed."""
    path = Path(path)
    missing = []
    while not path.exists() and path != path.parent:
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)


def _private_file(path: Path, data: bytes) -> None:
    _owner_only_dirs(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _new_directory(path: Path) -> Path:
    path = Path(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise BackupError(f"{path} already exists and is not an empty directory.")
    _owner_only_dirs(path)
    os.chmod(path, 0o700)
    return path


def _workspace_rooms(home: Path) -> list[str]:
    """The in-home workspaces (``workspaces/<name>``) whose store rows name the home's path."""
    rooms = Path(home) / "workspaces"
    try:
        return sorted(item.name for item in rooms.iterdir()
                      if item.is_dir() and not item.is_symlink())
    except OSError:
        return []


def _remap(database: Path, pairs: list[tuple[str, str]]) -> int:
    """Rewrite, in one transaction, every value naming an old workspace path or namespace (a
    restore into a home at another path): plain columns equal to it, and ``$.workspace`` or
    ``$.namespace`` inside JSON columns. Returns the rows changed."""
    connection = sqlite3.connect(database, isolation_level=None)
    changed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND substr(name, 1, 7) <> 'sqlite_'")]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                for old, new in pairs:
                    changed += connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{column}" = ?',
                        (new, old)).rowcount
                    if column.endswith("_json"):
                        for key in ("$.workspace", "$.namespace"):
                            changed += connection.execute(
                                f'UPDATE "{table}" SET "{column}" = json_set("{column}", ?, ?) '
                                f'WHERE json_valid("{column}") AND '
                                f'json_extract("{column}", ?) = ?', (key, new, key, old)).rowcount
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("Re-mapping the workspaces broke the store's references; "
                              "nothing was restored.")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return changed


def sanitize_reach(config: Mapping[str, Any]) -> tuple[dict, list[dict]]:
    """``reach.json`` without secrets: every MCP server environment value, URL credentials
    and query strings, and any string shaped like a credential; each removal is reported."""
    clean, excluded = copy.deepcopy(dict(config)), []

    def strip(value: Any, where: str) -> Any:
        if isinstance(value, dict):
            return {key: strip(item, f"{where}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [strip(item, f"{where}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, str) and credential_kinds(value):
            excluded.append({"item": f"reach.json: {where}",
                             "reason": f"credential-shaped text ({', '.join(credential_kinds(value))})"})
            return "[excluded from the backup]"
        return value

    servers = clean.get("mcpServers")
    if isinstance(servers, dict):
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            env = spec.get("env")
            if isinstance(env, dict) and env:
                for key in env:
                    excluded.append({"item": f"reach.json: mcpServers.{name}.env.{key}",
                                     "reason": "an MCP server's environment value (re-enter it "
                                               "after a restore)"})
                spec["env"] = {key: "" for key in env}
            url = spec.get("url")
            if isinstance(url, str):
                parts = urllib.parse.urlsplit(url)
                if parts.query or parts.username or parts.password:
                    excluded.append({"item": f"reach.json: mcpServers.{name}.url",
                                     "reason": "URL credentials or query string (re-enter them)"})
                    host = parts.hostname or ""
                    port = f":{parts.port}" if parts.port else ""
                    spec["url"] = urllib.parse.urlunsplit((parts.scheme, host + port,
                                                           parts.path, "", ""))
    return strip(clean, "$"), excluded


def create_backup(home: Path | str, output: Path | str | None = None, *,
                  include_secrets: bool = False, events=None,
                  lock_timeout: float = 60.0) -> dict[str, Any]:
    """Write a verified backup directory of ``home`` (default ``home/backups/<UTC time>``).
    A store kept locked for ``lock_timeout`` seconds fails the backup (nothing is left)."""
    from . import hygiene, release

    home = Path(os.path.realpath(home))
    store = home / STORE
    if not store.is_file():
        raise BackupError(f"There is nothing to back up: {home} has no store yet.")
    if output is None:
        (home / "backups").mkdir(mode=0o700, exist_ok=True)
        os.chmod(home / "backups", 0o700)
    target = Path(output) if output else home / "backups" / _stamp()
    size_mb = store.stat().st_size / (1 << 20)
    disk = hygiene.disk_status(target)
    if "error" not in disk and disk["free_mb"] < 2 * size_mb + 16:
        raise BackupError(f"Not enough free disk space for a backup (about {2 * size_mb + 16:.0f}"
                          f" MiB needed, {disk['free_mb']:.0f} MiB free).")
    started = time.monotonic()
    target = _new_directory(target)
    try:
        files: list[dict] = []
        excluded = list(ALWAYS_EXCLUDED)
        (target / "state").mkdir(mode=0o700)
        store_info = state.backup_database(store, target / STORE, timeout=lock_timeout)
        files.append({"path": STORE, "sha256": _sha256(target / STORE),
                      "bytes": (target / STORE).stat().st_size})
        for relative in _OPTIONAL:
            source = home / relative
            if source.is_file() and not source.is_symlink():
                _private_file(target / relative, source.read_bytes())
                files.append({"path": relative, "sha256": _sha256(target / relative),
                              "bytes": (target / relative).stat().st_size})
        reach = home / "reach.json"
        if reach.is_file() and not reach.is_symlink():
            raw = reach.read_bytes()
            if include_secrets:
                data = raw
            else:
                try:
                    config = json.loads(raw)
                    if not isinstance(config, dict):
                        raise ValueError
                    clean, removed = sanitize_reach(config)
                    excluded += removed
                    data = (json.dumps(clean, indent=2) + "\n").encode("utf-8")
                except ValueError:
                    data = None
                    excluded.append({"item": "reach.json", "reason": "not valid JSON, so its "
                                     "secrets could not be separated; not backed up"})
            if data is not None:
                _private_file(target / "reach.json", data)
                files.append({"path": "reach.json", "sha256": _sha256(target / "reach.json"),
                              "bytes": len(data)})
        now = time.time()
        manifest = {"format": FORMAT, "created_at": _iso(now), "created_ts": round(now, 3),
                    "product": "Brainstem Agent", "version": release.identity()["id"],
                    "store": {key: store_info[key] for key in (
                        "method", "integrity_check", "schema_version", "schema_sha256",
                        "compatibility", "counts")},
                    "secrets_included": include_secrets, "files": files, "excluded": excluded,
                    "home": str(canonical(home)), "workspaces": _workspace_rooms(home)}
        _private_file(target / MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    result = {"ok": True, "path": str(target), "seconds": round(time.monotonic() - started, 3),
              "files": files, "excluded": excluded, "secrets_included": include_secrets,
              "store": manifest["store"]}
    if events is not None:
        events.write("backup.created", path=target.name, seconds=result["seconds"],
                     files=len(files), secrets_included=include_secrets)
    return result


def verify_backup(path: Path | str) -> dict[str, Any]:
    """Check a backup directory: its manifest, and every listed file's size and SHA-256;
    anything unlisted, linked or missing is a problem."""
    path = Path(path)
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"ok": False, "problems": [f"{MANIFEST} is missing or unreadable ({error})"]}
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or \
            not isinstance(manifest.get("files"), list):
        return {"ok": False, "problems": [f"{MANIFEST} is not a {FORMAT} manifest"]}
    problems, listed = [], set()
    for item in manifest["files"]:
        relative = str(item.get("path", ""))
        parts = PurePosixPath(relative).parts
        if not relative or relative.startswith("/") or ".." in parts:
            problems.append(f"unsafe path in the manifest: {relative!r}")
            continue
        listed.add(relative)
        file = path.joinpath(*parts)
        if file.is_symlink() or not file.is_file():
            problems.append(f"{relative}: missing or not a regular file")
        elif file.stat().st_size != item.get("bytes"):
            problems.append(f"{relative}: size differs from the manifest")
        elif _sha256(file) != item.get("sha256"):
            problems.append(f"{relative}: SHA-256 differs from the manifest")
    if STORE not in listed:
        problems.append(f"the manifest lists no {STORE}")
    for directory, dirs, names in os.walk(path):
        for name in names:
            relative = (Path(directory) / name).relative_to(path).as_posix()
            if relative != MANIFEST and relative not in listed:
                problems.append(f"{relative}: not listed in the manifest")
    return {"ok": not problems, "problems": problems[:50], "manifest": manifest}


def restore_backup(backup: Path | str, home: Path | str, *, replace: bool = False,
                   events=None) -> dict[str, Any]:
    """Restore a verified backup into ``home`` (a new home, or ``replace`` an existing store
    after a safety backup of it). Refuses, writing nothing, on any digest mismatch, a newer
    store, or a running daemon."""
    from . import daemon

    started = time.monotonic()
    check = verify_backup(backup)
    if not check["ok"]:
        raise BackupError("The backup failed verification, so nothing was restored: "
                          + "; ".join(check["problems"][:8]))
    manifest, backup = check["manifest"], Path(backup)
    found = state.inspect_database(backup / STORE)
    if found.get("compatibility") not in ("current", "migrates"):
        raise BackupError(f"The backup's store is {found.get('compatibility') or 'unreadable'} "
                          "for this version (a newer Brainstem Agent made it?); nothing was "
                          "restored. Upgrade first, then restore.")
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home = Path(os.path.realpath(home))
    if daemon.read_record(home) is not None:
        raise BackupError("The daemon is running for this home; stop it first "
                          "(brainstem-agent stop), then restore.")
    target_store = home / STORE
    safety = None
    if target_store.exists():
        if not replace:
            raise BackupError(f"{home} already has a store; restore into a new home, or add "
                              "--replace (a safety backup of the current store is taken first).")
        (home / "backups").mkdir(mode=0o700, exist_ok=True)
        safety = create_backup(home, home / "backups" / f"pre-restore-{_stamp()}",
                               include_secrets=True, events=events)["path"]
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    staged = state_dir / f".restore-{secrets.token_hex(4)}.sqlite3"
    restored, kept = [], []
    try:
        shutil.copyfile(backup / STORE, staged)
        os.chmod(staged, 0o600)
        expected = next(item["sha256"] for item in manifest["files"] if item["path"] == STORE)
        if _sha256(staged) != expected:
            raise BackupError("The store changed while it was being restored; nothing was "
                              "restored.")
        remapped = _rehome(staged, manifest, home)
        for sidecar in ("-journal", "-wal", "-shm"):
            Path(str(target_store) + sidecar).unlink(missing_ok=True)
        os.replace(staged, target_store)
        restored.append(STORE)
        (state_dir / "search.sqlite3").unlink(missing_ok=True)
    finally:
        staged.unlink(missing_ok=True)
    for item in manifest["files"]:
        relative = item["path"]
        if relative == STORE:
            continue
        destination = home.joinpath(*PurePosixPath(relative).parts)
        sanitized = relative == "reach.json" and not manifest.get("secrets_included")
        if destination.exists() and (sanitized or not replace):
            kept.append({"item": relative, "reason": "kept the existing file" + (
                " (the backup's copy has its secrets removed)" if sanitized else "")})
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
        shutil.copyfile(backup.joinpath(*PurePosixPath(relative).parts), temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        restored.append(relative)
    with state.Store(target_store) as opened:  # validates, and migrates an older layout
        migrated = opened.pre_migration_backup
        counts = opened.table_counts()
    attention = [item for item in manifest["excluded"] if item["item"].startswith("reach.json")]
    result = {"ok": True, "home": str(home), "backup": str(backup), "restored": restored,
              "kept": kept, "counts": counts, "safety_backup": safety,
              "migrated": migrated is not None, "needs_attention": attention,
              "remapped": remapped,
              "excluded": manifest["excluded"], "backup_version": manifest.get("version"),
              "seconds": round(time.monotonic() - started, 3)}
    if events is not None:
        events.write("restore.completed", backup=backup.name, restored=len(restored),
                     migrated=result["migrated"], seconds=result["seconds"])
    return result


def _rehome(database: Path, manifest: Mapping[str, Any], home: Path) -> dict[str, Any]:
    """A backup restored into a home at another path: the in-home workspaces' paths, and the
    namespaces derived from them, are re-mapped in the staged store copy, so their memory,
    skills, sessions, schedules and inbox stay visible (and schedules run in the new home)."""
    from .host import OWNER, cell_namespace

    old = manifest.get("home")
    if not isinstance(old, str) or not old:
        return {"rows_changed": 0, "reason": "the backup does not record its home"}
    home = canonical(home)
    if old == str(home) or (Path(old).exists() and canonical(old) == home):
        return {"rows_changed": 0, "reason": "same home"}
    rooms = [name for name in manifest.get("workspaces") or ["default"]
             if isinstance(name, str) and name and "/" not in name and name not in (".", "..")]
    pairs: list[tuple[str, str]] = []
    for name in rooms:
        room = home / "workspaces" / name
        _owner_only_dirs(room)
        before, after = str(Path(old) / "workspaces" / name), str(canonical(room))
        pairs += [(before, after), (cell_namespace(OWNER, before), cell_namespace(OWNER, after))]
    return {"from_home": old, "to_home": str(home), "workspaces": rooms,
            "rows_changed": _remap(database, pairs)}


def _jsonl(path: Path, records: list[dict]) -> dict:
    data = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    _private_file(path, data.encode("utf-8"))
    return {"records": len(records)}


def export_data(host, output: Path | str) -> dict[str, Any]:
    """Portable export of the host's workspace and the owner's profile (``EXPORT_FORMATS``)."""
    from .organs.skills import render_skill

    target = _new_directory(Path(output))
    store, files = host.store, []
    try:
        for skill in store.list_skills([host.namespace, host.profile_namespace]):
            scope = "profile" if skill["scope"] == host.profile_namespace else "workspace"
            text = render_skill(skill, scope_label=scope)
            relative = f"skills/{scope}-{skill['name']}.md"
            _private_file(target / relative, text.encode("utf-8"))
            files.append({"path": relative, "records": 1})
        for name, scope, namespace in (("memory.jsonl", "workspace", host.namespace),
                                       ("profile.jsonl", "profile", host.profile_namespace)):
            facts = [{"fact_id": fact["fact_id"], "scope": scope, "text": fact["text"],
                      "created_at": fact["created_at"], "updated_at": fact["updated_at"],
                      "source_turn": fact["source_turn"]}
                     for fact in reversed(store.list_facts(namespace, limit=5000))]
            files.append({"path": name, **_jsonl(target / name, facts)})
        sessions: dict[str, list[dict]] = {}
        for turn in store.export_turns(host.namespace):
            messages = sessions.setdefault(turn["session_id"], [])
            messages.append({"turn_id": turn["turn_id"], "role": "user",
                             "content": turn["user_input"], "state": turn["state"],
                             "at": turn["started_at"]})
            if turn["response"] is not None:
                messages.append({"turn_id": turn["turn_id"], "role": "assistant",
                                 "content": turn["response"], "state": turn["state"],
                                 "at": turn["finished_at"]})
        for session_id, messages in sessions.items():
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:120]
            files.append({"path": f"sessions/{safe}.jsonl",
                          **_jsonl(target / "sessions" / f"{safe}.jsonl", messages)})
        for item in files:
            item["sha256"] = _sha256(target.joinpath(*PurePosixPath(item["path"]).parts))
        manifest = {"format": EXPORT_FORMAT, "created_at": _iso(time.time()),
                    "workspace": str(host.workspace), "formats": EXPORT_FORMATS, "files": files}
        _private_file(target / "export-manifest.json",
                      (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"ok": True, "path": str(target), "files": files,
            "counts": {"skills": sum(1 for item in files if item["path"].startswith("skills/")),
                       "sessions": len(sessions),
                       "memory": next(item["records"] for item in files
                                      if item["path"] == "memory.jsonl"),
                       "profile": next(item["records"] for item in files
                                       if item["path"] == "profile.jsonl")}}
