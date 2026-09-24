"""Lifecycle: upgrade and roll back between side-by-side versions, and uninstall.

``upgrade`` takes a local release only (a repository checkout, ``runtime/``, a zipapp or a
wheel; nothing is fetched), verifies it against its release manifest, checks that it can open
this home's store, takes a pre-upgrade backup, drains the daemon (the running turn and
scheduled run finish; nothing new starts), keeps the running version installed, switches the
home's active version, points the LaunchAgent at it and restarts the daemon. If the new version
does not come up, the previous one is restored automatically. ``rollback`` switches back to
the previous version when it can read the store; when the new version has already migrated
the store beyond what the previous one reads, rollback refuses with guidance, or, with
``--restore <pre-upgrade backup>``, puts that store back first (the current one is kept in a
safety backup). ``uninstall`` removes exactly what its dry run lists and never touches the
installed RAPP Brainstem.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import backup, daemon, release, state
from .observe import EventLog
from .paths import canonical, holds

__all__ = ["LifecycleError", "rollback", "store_compatibility", "uninstall", "upgrade"]

KNOWN_ENTRIES = frozenset({"state", "cache", "workers", "run", "logs", "workspaces", "versions",
                           "backups", "reach.json", "operations.json"})


class LifecycleError(RuntimeError):
    """An upgrade, rollback or uninstall was refused; nothing was changed."""


def store_compatibility(home: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Can the release described by ``manifest`` open this home's store?"""
    found = state.inspect_database(Path(home) / "state" / "agent.sqlite3")
    if not found["exists"]:
        return {"verdict": "no-store"}
    if not found.get("readable"):
        return {"verdict": "unreadable", "error": found.get("error")}
    key = (found["schema_version"], found["schema_sha256"])
    store = manifest.get("store") or {}
    reads = {(item["schema_version"], item["schema_sha256"]) for item in store.get("reads", [])}
    migrates = {(item["schema_version"], item["schema_sha256"])
                for item in store.get("migrates_from", [])}
    verdict = "reads" if key in reads else "migrates" if key in migrates else "incompatible"
    return {"verdict": verdict, "store_schema_version": found["schema_version"],
            "store_schema_sha256": found["schema_sha256"]}


def _du(path: Path) -> int:
    total = 0
    for directory, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += (Path(directory) / name).lstat().st_size
            except OSError:
                pass
    return total


def _drain_and_stop(home: Path, *, timeout: float, force: bool) -> dict[str, Any]:
    """Let the daemon finish its running work, then stop it (nothing new starts meanwhile)."""
    client = daemon.connect(home)
    if client is None:
        return {"running": False}
    record = daemon.read_record(home) or {}
    try:
        drained = client.call("POST", "/v1/drain", {"timeout": timeout}, timeout=timeout + 30)
    except daemon.DaemonUnavailable:
        return {"running": False}
    except daemon.DaemonError as error:
        drained = {"drained": False, "error": str(error)}
    if not drained.get("drained") and not force:
        try:
            client.call("POST", "/v1/drain", {"resume": True}, timeout=10)
        except daemon.DaemonError:
            pass
        raise LifecycleError(
            f"The daemon is still busy after {timeout:g}s (turn "
            f"{(drained.get('active_turn') or {}).get('turn_id')}, scheduled run "
            f"{drained.get('running_occurrence')}); nothing was changed and it keeps running. "
            "Try again later, or add --force to cancel the running work.")
    stopped = daemon.stop(home, timeout=60)
    if not stopped.get("ok"):
        raise LifecycleError("The daemon did not stop cleanly; nothing else was changed "
                             f"({stopped}).")
    return {"running": True, "workspace": record.get("workspace"), "drain": drained,
            "stop": {key: stopped.get(key) for key in ("seconds", "workers_gone")}}


def _version_health(root: Path, home: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """The given version's own view of the cell (``status`` when a daemon runs, else
    ``doctor``), through the same health model."""
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), *[item for item in env.get(
        "PYTHONPATH", "").split(os.pathsep) if item]])
    env["BRAINSTEM_AGENT_HOME"] = str(home)
    env["BRAINSTEM_AGENT_REDIRECTED"] = root.name
    command = "status" if daemon.read_record(home) else "doctor"
    try:
        result = subprocess.run([sys.executable, "-m", "brainstem_agent", command, "--json"],
                                capture_output=True, text=True, env=env, timeout=120)
        document = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"command": command, "ok": False, "error": type(error).__name__}
    summary = next((item for item in (document.get("readiness"), document.get("health"))
                    if isinstance(item, dict)), {})
    return {"command": command, "exit": result.returncode,
            "live": summary.get("live", document.get("running")),
            "ready": summary.get("ready", document.get("ready")),
            "failing": summary.get("failing", [item.split(":")[0] for item in
                                               document.get("problems", [])])}


def _come_up(home: Path, root: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """Wait until a daemon for ``home`` answers as the version installed at ``root`` (launchd
    starts it under a LaunchAgent); raise LifecycleError when none does in time."""
    try:
        timeout = float(environ.get("BRAINSTEM_AGENT_SERVICE_START_TIMEOUT") or 60)
    except ValueError:
        timeout = 60.0
    deadline, seen = time.monotonic() + timeout, None
    while True:
        client = daemon.connect(home)
        if client is not None:
            try:
                seen = client.call("GET", "/v1/version", timeout=5).get("version_id")
            except daemon.DaemonError:
                seen = None
            if seen == root.name:
                record = daemon.read_record(home) or {}
                return {"pid": record.get("pid"), "version": seen, "live": True}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    raise LifecycleError(f"The daemon did not come up on {root.name} within {timeout:g}s"
                         + (f" (a daemon answered as {seen})" if seen else
                            " (the LaunchAgent started nothing that answers)"))


def _restart(home: Path, environ: Mapping[str, str], root: Path, stop: Mapping[str, Any],
             service_installed: bool) -> dict[str, Any]:
    """Point the LaunchAgent at ``root`` (when installed) and start the daemon again if it
    ran before. Returns what happened; raises LifecycleError when it does not come up (as
    that version, also when launchd starts it)."""
    done: dict[str, Any] = {}
    if service_installed:
        daemon.service("uninstall", home, environ, dry_run=False)
        done["service"] = daemon.service("install", home, environ, dry_run=False,
                                         package_parent=str(root))
        done["service"].pop("plist", None)
        if stop.get("running") and done["service"].get("ok"):
            done["daemon"] = _come_up(home, root, environ)
    if stop.get("running") and not (service_installed and done["service"].get("ok")):
        extra = ["--workspace", stop["workspace"]] if stop.get("workspace") else []
        try:
            status = daemon.spawn(home, environ, extra, package_parent=str(root))
        except daemon.DaemonError as error:
            raise LifecycleError(f"The daemon did not start: {error}") from None
        done["daemon"] = {"pid": status.get("pid"), "version": (status.get("readiness") or {})
                          .get("version"), "live": (status.get("readiness") or {}).get("live",
                                                                                     True)}
        if done["daemon"]["version"] not in (None, root.name):
            raise LifecycleError(f"The daemon did not come up on {root.name} (it answered as "
                                 f"{done['daemon']['version']})")
    return done


def _switch(home: Path, active: str, previous: str | None, action: str) -> None:
    record = release.read_active(home) or {}
    history = list(record.get("history") or [])[-19:]
    history.append({"at": time.time(), "action": action, "from": record.get("active"),
                    "to": active})
    release.write_active(home, {"active": active, "previous": previous,
                                "switched_at": time.time(), "history": history})


def _current_version(home: Path) -> str:
    active = release.read_active(home)
    return active["active"] if active else release.identity()["id"]


def _ensure_current_installed(home: Path) -> dict[str, Any]:
    """Keep the version in use available for rollback (the running one when the home has no
    active version yet)."""
    active = release.read_active(home)
    if active:
        root = release.versions_dir(home) / active["active"]
        if release.verify_tree(root / release.PACKAGE_NAME)["ok"]:
            return {"id": active["active"], "path": str(root)}
    check = release.verify_tree(release.PACKAGE)
    if not check["ok"]:
        raise LifecycleError("The running installation does not match its release manifest, so "
                             "it cannot be kept for a rollback: "
                             + "; ".join(check["problems"][:4]))
    return release.install_tree(home, release.PACKAGE.parent, origin="kept at upgrade")


def upgrade(home: Path | str, environ: Mapping[str, str], source: Path | str, *,
            dry_run: bool = False, drain_timeout: float = 300.0, force: bool = False
            ) -> dict[str, Any]:
    from . import grail

    home = canonical(home)
    started = time.monotonic()
    work = release.versions_dir(home) / f".candidate-{secrets.token_hex(4)}"
    try:
        root = release.stage_candidate(source, work)
        check = release.verify_tree(root / release.PACKAGE_NAME)
        if not check["ok"]:
            raise LifecycleError("The release does not match its manifest, so it was not "
                                 "installed: " + "; ".join(check["problems"][:5]))
        manifest = release.load_manifest(root / release.PACKAGE_NAME)
        current = _current_version(home)
        plan: dict[str, Any] = {"ok": True, "dry_run": dry_run, "from": current,
                                "to": check["id"], "source": str(source),
                                "manifest": {"verified": True, "files": check["files"],
                                             "tree_sha256": check["tree_sha256"]}}
        if check["id"] == current:
            return {**plan, "changed": False, "reason": f"{current} is already active"}
        compatibility = store_compatibility(home, manifest)
        plan["store"] = compatibility
        if compatibility["verdict"] not in ("reads", "migrates", "no-store"):
            raise LifecycleError(f"The new version cannot open this home's store "
                                 f"({compatibility['verdict']}); nothing was changed.")
        cache = Path(environ.get("BRAINSTEM_AGENT_CACHE") or home / "cache")
        needs = []
        if manifest["grail"]["commit"] != grail.PINNED_COMMIT and not \
                (cache / "grail" / manifest["grail"]["commit"]).is_dir():
            needs.append("its pinned Grail source")
        lock = release._sha256_file(release.PACKAGE / "data" / "grail-requirements.lock")
        if manifest.get("worker_lock_sha256") != lock:
            needs.append("a worker interpreter built from its lock")
        if needs:
            raise LifecycleError(
                f"The new version needs {' and '.join(needs)}, which this home does not have, "
                "and upgrade never downloads anything. Prepare it first with the new version's "
                f"setup (PYTHONPATH={root} python3 -m brainstem_agent setup), then upgrade.")
        service_installed = daemon.service_plist_path(home, environ).exists()
        plan.update(daemon_running=daemon.read_record(home) is not None,
                    service_installed=service_installed)
        if dry_run:
            return plan
        events = EventLog(home)
        pre = None
        (home / "backups").mkdir(mode=0o700, exist_ok=True)
        if compatibility["verdict"] != "no-store":
            pre = backup.create_backup(home, home / "backups" /
                                       f"pre-upgrade-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}",
                                       include_secrets=True, events=events)["path"]
        stop = _drain_and_stop(home, timeout=drain_timeout, force=force)
        previous = _ensure_current_installed(home)
        installed = release.install_tree(home, root, origin="upgrade")
        _switch(home, installed["id"], previous["id"], "upgrade")
        target = release.versions_dir(home) / installed["id"]
        try:
            restarted = _restart(home, environ, target, stop, service_installed)
        except LifecycleError as error:
            _switch(home, previous["id"], None, "automatic rollback")
            again = ""
            try:
                back = _restart(home, environ, Path(previous["path"]), stop, service_installed)
            except LifecycleError as failure:  # still switched back; say so and how to start
                back = {"ok": False, "error": str(failure)}
                again = (f" (its daemon did not come back either: {failure}; start it with "
                         "brainstem-agent serve --detach)")
            events.write("upgrade.rolled_back", level="error", to=installed["id"],
                         back=previous["id"], error=str(error))
            return {**plan, "ok": False, "error": f"{error}; the previous version "
                    f"{previous['id']} is active again{again}", "rolled_back": True,
                    "restarted": back, "pre_upgrade_backup": pre}
        result = {**plan, "changed": True, "previous": previous["id"],
                  "pre_upgrade_backup": pre, "drain": stop, "restarted": restarted,
                  "health": _version_health(target, home, environ),
                  "seconds": round(time.monotonic() - started, 3)}
        events.write("upgrade.completed", level="info", to=installed["id"],
                     previous=previous["id"], seconds=result["seconds"])
        return result
    except release.ReleaseError as error:
        raise LifecycleError(str(error)) from None
    finally:
        if work.exists():
            release._remove(work)


def _pre_upgrade_backups(home: Path, version: str) -> list[str]:
    found = []
    for manifest in sorted((home / "backups").glob("pre-upgrade-*/manifest.json"), reverse=True):
        try:
            if json.loads(manifest.read_text()).get("version") == version:
                found.append(str(manifest.parent))
        except (OSError, ValueError):
            continue
    return found


def rollback(home: Path | str, environ: Mapping[str, str], *, dry_run: bool = False,
             drain_timeout: float = 300.0, force: bool = False,
             restore: Path | str | None = None) -> dict[str, Any]:
    home = canonical(home)
    active = release.read_active(home)
    if not active or not active.get("previous"):
        raise LifecycleError("There is no previous version to roll back to (no upgrade is "
                             "recorded for this home).")
    previous, current = active["previous"], active["active"]
    root = release.versions_dir(home) / previous
    try:
        check = release.verify_tree(root / release.PACKAGE_NAME)
        manifest = release.load_manifest(root / release.PACKAGE_NAME)
    except release.ReleaseError as error:
        raise LifecycleError(f"The previous version {previous} is not installed intact ({error});"
                             " install it again with upgrade --from <that release>.") from None
    if not check["ok"]:
        raise LifecycleError(f"The previous version {previous} is damaged: "
                             + "; ".join(check["problems"][:4]))
    compatibility = store_compatibility(home, manifest)
    plan: dict[str, Any] = {"ok": True, "dry_run": dry_run, "from": current, "to": previous,
                            "store": compatibility}
    restoring = None
    if restore is not None:
        verified = backup.verify_backup(restore)
        if not verified["ok"]:
            raise LifecycleError("The backup failed verification; nothing was changed: "
                                 + "; ".join(verified["problems"][:5]))
        found = state.inspect_database(Path(restore) / backup.STORE)
        key = (found.get("schema_version"), found.get("schema_sha256"))
        readable = {(item["schema_version"], item["schema_sha256"]) for item in
                    manifest["store"]["reads"] + manifest["store"]["migrates_from"]}
        if key not in readable:
            raise LifecycleError(f"That backup's store is not one {previous} can read either; "
                                 "nothing was changed.")
        restoring = str(restore)
        plan["restore"] = {"backup": restoring, "store_made_by": verified["manifest"].get(
            "version")}
    elif compatibility["verdict"] not in ("reads", "migrates", "no-store"):
        candidates = _pre_upgrade_backups(home, previous)
        hint = (f" To go back with the store as it was before the upgrade (later changes are "
                f"kept in a safety backup, not in the store), run: brainstem-agent rollback "
                f"--restore {candidates[0]}" if candidates else
                " No pre-upgrade backup made by that version was found in the home's backups.")
        raise LifecycleError(
            f"Rollback refused: this home's store now uses a schema that {previous} cannot read "
            f"(the store was migrated by {current}); nothing was changed.{hint}")
    service_installed = daemon.service_plist_path(home, environ).exists()
    plan.update(daemon_running=daemon.read_record(home) is not None,
                service_installed=service_installed)
    if dry_run:
        return plan
    events = EventLog(home)
    stop = _drain_and_stop(home, timeout=drain_timeout, force=force)
    if restoring is not None:
        (home / "backups").mkdir(mode=0o700, exist_ok=True)
        safety = backup.create_backup(home, home / "backups" /
                                      f"pre-rollback-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}",
                                      include_secrets=True, events=events)["path"]
        store = home / backup.STORE
        staged = store.with_name(f".rollback-{secrets.token_hex(4)}.sqlite3")
        shutil.copyfile(Path(restoring) / backup.STORE, staged)
        os.chmod(staged, 0o600)
        for sidecar in ("-journal", "-wal", "-shm"):
            Path(str(store) + sidecar).unlink(missing_ok=True)
        os.replace(staged, store)
        (home / "state" / "search.sqlite3").unlink(missing_ok=True)
        plan["restore"]["safety_backup"] = safety
    _switch(home, previous, current, "rollback")
    restarted = _restart(home, environ, root, stop, service_installed)
    result = {**plan, "changed": True, "drain": stop, "restarted": restarted,
              "health": _version_health(root, home, environ)}
    events.write("rollback.completed", to=previous, previous=current,
                 restored_store=restoring is not None)
    return result


# -- uninstall -------------------------------------------------------------------------------
def _brainstem_home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("BRAINSTEM_HOME") or os.path.join(
        environ.get("HOME") or os.path.expanduser("~"), ".brainstem"))


def uninstall_plan(home: Path | str, environ: Mapping[str, str], *,
                   remove_home: bool = False) -> dict[str, Any]:
    """Exactly what ``uninstall`` would remove (and what it keeps), without changing anything."""
    home = canonical(home)
    owner_home = canonical(environ.get("HOME") or os.path.expanduser("~"))
    brainstem = _brainstem_home(environ)
    refusals = []
    if holds(home, brainstem) or holds(brainstem, home):
        refusals.append(f"the home {home} overlaps the installed RAPP Brainstem ({brainstem}), "
                        "which is never touched")
    if holds(home, owner_home) or str(home) == "/":
        refusals.append(f"the home {home} is or contains your home folder")
    items: list[dict] = []
    record = daemon.read_record(home)
    if record is not None:
        items.append({"kind": "daemon", "action": "stop", "pid": record["pid"]})
    plist = daemon.service_plist_path(home, environ)
    if plist.exists():
        items.append({"kind": "service", "action": "bootout and remove", "path": str(plist)})
    for name in ("workers", "run", "logs", "versions"):
        path = home / name
        if path.exists():
            items.append({"kind": name, "action": "remove", "path": str(path),
                          "bytes": _du(path)})
    cache = canonical(environ.get("BRAINSTEM_AGENT_CACHE") or home / "cache")
    kept: list[dict] = []
    if cache.exists():
        if holds(home, cache):
            items.append({"kind": "cache", "action": "remove", "path": str(cache),
                          "bytes": _du(cache)})
        else:
            kept.append({"path": str(cache), "reason": "the cache is outside the home "
                         "(BRAINSTEM_AGENT_CACHE); remove it yourself if nothing else uses it"})
    unknown = sorted(name for name in (os.listdir(home) if home.is_dir() else [])
                     if name not in KNOWN_ENTRIES and not name.startswith("."))
    if remove_home:
        if unknown:
            refusals.append(f"the home holds entries Brainstem Agent did not create "
                            f"({', '.join(unknown[:8])}); move them out first")
        if home.exists():
            counts = {}
            store = home / "state" / "agent.sqlite3"
            if store.exists():
                try:
                    counts = state.read_counts(store)
                except Exception as error:  # a damaged store is still removed on request
                    counts = {"error": str(error)[:200]}
            items.append({"kind": "home", "action": "remove", "path": str(home),
                          "bytes": _du(home), "store_counts": counts,
                          "includes": [name for name in ("state", "workspaces", "backups",
                                                         "reach.json", "operations.json")
                                       if (home / name).exists()]})
    else:
        for name in ("state", "workspaces", "backups", "reach.json", "operations.json"):
            if (home / name).exists():
                kept.append({"path": str(home / name), "reason": "your data and settings (add "
                             "--remove-home with --confirm to remove the whole home)"})
    kept.append({"path": str(brainstem), "reason": "the installed RAPP Brainstem is never "
                 "touched"})
    kept.append({"path": str(release.PACKAGE.parent), "reason": "the runtime itself: remove it "
                 "with pip uninstall brainstem-agent (or delete its virtual environment or the "
                 "zipapp file)"})
    return {"ok": not refusals, "home": str(home), "remove": items, "keep": kept,
            "refused": refusals}


def uninstall(home: Path | str, environ: Mapping[str, str], *, dry_run: bool = False,
              remove_home: bool = False, confirm: str | None = None) -> dict[str, Any]:
    plan = uninstall_plan(home, environ, remove_home=remove_home)
    plan["dry_run"] = dry_run
    if not plan["ok"] or dry_run:
        return plan
    if remove_home and (confirm is None or canonical(confirm) != Path(plan["home"])):
        return {**plan, "ok": False, "needs_confirmation": True,
                "refused": [f"removing the home deletes every conversation, memory, skill, "
                            f"schedule, backup and workspace file in it; confirm with --confirm "
                            f"{plan['home']}"]}
    home = Path(plan["home"])
    done = []
    for item in plan["remove"]:
        if item["kind"] == "daemon":
            stopped = daemon.stop(home, timeout=60)
            done.append({**item, "ok": bool(stopped.get("ok"))})
        elif item["kind"] == "service":
            result = daemon.service("uninstall", home, environ, dry_run=False)
            done.append({**item, "ok": not Path(item["path"]).exists(),
                         "commands": result.get("commands")})
        else:
            release._remove(Path(item["path"]))
            done.append({**item, "ok": not Path(item["path"]).exists()})
    return {**plan, "ok": all(item["ok"] for item in done), "removed": done}
