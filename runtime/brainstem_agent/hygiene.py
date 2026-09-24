"""Resource hygiene: the owner's operations policy, the disk-space floor, bounded logs,
retention with a dry run, and store compaction.

``$BRAINSTEM_AGENT_HOME/operations.json`` (optional, owner-editable; every key has a default):

    {"retention": {"receipts_days": 90, "run_events_days": 30, "egress_days": 30,
                   "inbox_days": 90, "inbox_keep_per_schedule": 20, "pre_migration_days": 30},
     "logs": {"max_bytes": 1048576, "keep": 3, "worker_logs_keep": 20,
              "worker_log_max_bytes": 4194304},
     "disk": {"min_free_mb": 512}}

``BRAINSTEM_AGENT_MIN_FREE_MB`` overrides ``disk.min_free_mb``.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from .observe import rotate_file

__all__ = ["DEFAULTS", "compact", "disk_status", "load_policy", "log_hygiene",
           "low_disk_refusal", "prune"]

POLICY_FILE = "operations.json"
DEFAULTS: dict[str, dict[str, float]] = {
    "retention": {"receipts_days": 90, "run_events_days": 30, "egress_days": 30,
                  "inbox_days": 90, "inbox_keep_per_schedule": 20, "pre_migration_days": 30},
    "logs": {"max_bytes": 1 << 20, "keep": 3, "worker_logs_keep": 20,
             "worker_log_max_bytes": 4 << 20},
    "disk": {"min_free_mb": 512},
}
_DAY = 86400.0


def load_policy(home: Path | str, environ: Mapping[str, str] | None = None
                ) -> tuple[dict[str, dict[str, float]], list[str]]:
    """The effective policy and the problems found in ``operations.json`` (a bad value keeps
    its default; the file never stops the cell)."""
    environ = os.environ if environ is None else environ
    policy, problems = copy.deepcopy(DEFAULTS), []
    path = Path(home) / POLICY_FILE
    if path.exists():
        try:
            given = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(given, dict):
                raise ValueError("the file must hold a JSON object")
        except (OSError, ValueError) as error:
            given = {}
            problems.append(f"{POLICY_FILE}: {error}")
        for section, values in given.items():
            if section not in policy or not isinstance(values, dict):
                problems.append(f"{POLICY_FILE}: unknown section {section!r}")
                continue
            for key, value in values.items():
                if key not in policy[section]:
                    problems.append(f"{POLICY_FILE}: unknown key {section}.{key}")
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    problems.append(f"{POLICY_FILE}: {section}.{key} must be a number >= 0")
                else:
                    policy[section][key] = value
    override = environ.get("BRAINSTEM_AGENT_MIN_FREE_MB")
    if override:
        try:
            policy["disk"]["min_free_mb"] = max(0.0, float(override))
        except ValueError:
            problems.append("BRAINSTEM_AGENT_MIN_FREE_MB must be a number")
    return policy, problems


def _existing(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def disk_status(home: Path | str, environ: Mapping[str, str] | None = None,
                policy: Mapping | None = None) -> dict[str, Any]:
    """Free space on the volume holding the home against the policy's floor."""
    policy = policy or load_policy(home, environ)[0]
    floor = float(policy["disk"]["min_free_mb"])
    try:
        usage = shutil.disk_usage(_existing(Path(home)))
    except OSError as error:
        return {"ok": False, "error": str(error), "min_free_mb": floor}
    free_mb = usage.free / (1 << 20)
    return {"ok": free_mb >= floor, "free_mb": round(free_mb, 1), "min_free_mb": floor,
            "total_mb": round(usage.total / (1 << 20), 1)}


def low_disk_refusal(home: Path | str, environ: Mapping[str, str] | None = None) -> str | None:
    """The message a new turn is refused with while disk space is below the floor."""
    status = disk_status(home, environ)
    if status["ok"]:
        return None
    if "error" in status:
        return f"Brainstem Agent could not check free disk space ({status['error']})."
    return (f"Low disk space: only {status['free_mb']:.0f} MiB are free on the disk that holds "
            f"Brainstem Agent's home; a turn needs at least {status['min_free_mb']:.0f} MiB. "
            "Nothing was run. Free some space (or run `brainstem-agent prune` and "
            "`brainstem-agent compact`), then try again.")


def worker_logs(home: Path | str) -> list[Path]:
    """Worker log files, newest first."""
    folder = Path(home) / "logs" / "workers"
    try:
        files = [item for item in folder.iterdir() if item.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)


def log_hygiene(home: Path | str, policy: Mapping | None = None) -> dict[str, Any]:
    """Keep logs bounded: rotate the daemon's output in place, keep the newest worker logs."""
    policy = policy or load_policy(home)[0]
    logs = policy["logs"]
    rotated = []
    daemon_log = Path(home) / "logs" / "daemon.log"
    if rotate_file(daemon_log, max_bytes=int(logs["max_bytes"]), keep=int(logs["keep"]),
                   truncate=True):
        rotated.append(daemon_log.name)
    removed = 0
    keep = max(1, int(logs["worker_logs_keep"]))
    for extra in worker_logs(home)[keep:]:
        try:
            extra.unlink()
            removed += 1
        except OSError:
            pass
    return {"rotated": rotated, "worker_logs_removed": removed}


def _egress_plan(home: Path, before: float) -> tuple[list[tuple[Path, list[str], int]], int]:
    from .observe import parse_when

    plans, total = [], 0
    for path in (home / "state" / "egress.jsonl.1", home / "state" / "egress.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        kept, old = [], 0
        for line in lines:
            try:
                moment = parse_when(json.loads(line).get("at"))
            except (ValueError, AttributeError, TypeError):
                moment = None
            if moment is not None and moment < before:
                old += 1
            else:
                kept.append(line)
        plans.append((path, kept, old))
        total += old
    return plans, total


def prune(home: Path | str, store, *, dry_run: bool = True, now: float | None = None,
          policy: Mapping | None = None) -> dict[str, Any]:
    """Apply the retention policy: receipts, run events, the inbox, the egress log and old
    pre-migration copies. ``dry_run`` reports exactly what would go and changes nothing."""
    home, now = Path(home), time.time() if now is None else now
    policy = policy or load_policy(home)[0]
    keep = policy["retention"]

    def cutoff(key: str) -> float:
        return now - float(keep[key]) * _DAY

    categories = store.retention(receipts_before=cutoff("receipts_days"),
                                 run_events_before=cutoff("run_events_days"),
                                 inbox_before=cutoff("inbox_days"),
                                 inbox_keep=int(keep["inbox_keep_per_schedule"]), dry_run=dry_run)
    plans, old = _egress_plan(home, cutoff("egress_days"))
    categories["egress_log"] = {"cutoff": cutoff("egress_days"), "count": old}
    if not dry_run and old:
        for path, kept, removed in plans:
            if not removed:
                continue
            if not kept:
                path.unlink(missing_ok=True)
                continue
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("\n".join(kept) + "\n")
            os.replace(temporary, path)
        categories["egress_log"]["deleted"] = old
    copies = sorted((home / "state" / "pre-migration").glob("*.sqlite3"),
                    key=lambda item: item.stat().st_mtime, reverse=True) \
        if (home / "state" / "pre-migration").is_dir() else []
    stale = [item for item in copies[1:] if item.stat().st_mtime < cutoff("pre_migration_days")]
    categories["pre_migration_copies"] = {"cutoff": cutoff("pre_migration_days"),
                                          "count": len(stale),
                                          "bytes": sum(item.stat().st_size for item in stale)}
    if not dry_run:
        for item in stale:
            item.unlink(missing_ok=True)
        categories["pre_migration_copies"]["deleted"] = len(stale)
    total = sum(entry.get("count", 0) for entry in categories.values())
    return {"ok": True, "dry_run": dry_run, "policy": dict(keep), "categories": categories,
            "total": total}


def compact(home: Path | str, store, *, environ: Mapping[str, str] | None = None) -> dict:
    """VACUUM the store once there is room for a full copy of it (plus the disk floor)."""
    path = Path(home) / "state" / "agent.sqlite3"
    size_mb = path.stat().st_size / (1 << 20)
    status = disk_status(home, environ)
    if "error" in status or status["free_mb"] < size_mb + status["min_free_mb"]:
        return {"ok": False, "error": (
            f"Compaction needs about {size_mb:.0f} MiB free for a temporary copy of the store, "
            f"on top of the {status.get('min_free_mb', 0):.0f} MiB floor; only "
            f"{status.get('free_mb', 0):.0f} MiB are free."), "disk": status}
    return {"ok": True, **store.compact()}
