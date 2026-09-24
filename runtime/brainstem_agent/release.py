"""Release identity, side-by-side versions and the single-file artifact.

* The release manifest (``data/release-manifest.json``) is produced from the tree with
  ``python -m brainstem_agent.release --write`` and names every file a release ships (with
  its SHA-256), the Grail pin, the store schema this version reads and migrates, the bridge
  and worker lock digests and the capability set. ``verify_tree`` checks a copy against it.
* A home keeps runtime versions side by side in ``versions/<version>-<tree12>/`` and names
  the active one in ``versions/active.json``. A command started from a version the home has
  moved away from continues in the active one (``redirect_target``), so the CLI, the daemon
  and the LaunchAgent always run the same code.
* ``build_zipapp`` writes the single-file artifact: a zipapp that verifies every file against
  its manifest and unpacks itself into the home's versions on first run (an offline install
  for any Python 3.11 or newer).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import __version__

__all__ = ["ReleaseError", "build_manifest", "build_zipapp", "identity", "install_tree",
           "installed_versions", "load_manifest", "read_active", "redirect_target",
           "stage_candidate", "verify_tree", "version_info", "write_active"]

PACKAGE = Path(__file__).resolve().parent
PACKAGE_NAME = "brainstem_agent"
MANIFEST_NAME = "release-manifest.json"
MANIFEST_PATH = PACKAGE / "data" / MANIFEST_NAME
MANIFEST_FORMAT = 1
MARKER = ".verified.json"
ACTIVE = "active.json"
_MAX_MEMBER = 8 * 1024 * 1024
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ReleaseError(RuntimeError):
    """A release artifact, manifest or version is missing, inconsistent or incompatible."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(files: Mapping[str, str]) -> str:
    return hashlib.sha256("".join(f"{name}\0{digest}\n" for name, digest in
                                  sorted(files.items())).encode("utf-8")).hexdigest()


def package_files(root: Path = PACKAGE) -> dict[str, str]:
    """Every file a release ships, as ``brainstem_agent/<path>`` -> SHA-256: the Python modules,
    the ``data/`` files and the companion's ``ui/`` assets (the manifest itself excluded; caches
    and dotfiles ignored)."""
    root, files = Path(root), {}
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(item for item in dirs if item != "__pycache__" and
                         not item.startswith("."))
        relative = Path(directory).relative_to(root)
        for name in sorted(names):
            path, posix = Path(directory) / name, (relative / name).as_posix()
            if name.startswith(".") or posix == f"data/{MANIFEST_NAME}":
                continue
            if not (name.endswith(".py") or posix.startswith(("data/", "ui/"))):
                continue
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ReleaseError(f"{posix} is not a regular file")
            files[f"{PACKAGE_NAME}/{posix}"] = _sha256_file(path)
    return files


def build_manifest(root: Path = PACKAGE) -> dict[str, Any]:
    """The release manifest of the package at ``root`` (this interpreter's package)."""
    from . import grail, state
    from .host import ALL_CAPABILITIES

    root = Path(root)
    files = package_files(root)
    data = root / "data"
    return {
        "format": MANIFEST_FORMAT,
        "product": "Brainstem Agent",
        "package": "brainstem-agent",
        "version": __version__,
        "requires_python": ">=3.11",
        "grail": {"repository": "kody-w/rapp-installer", "commit": grail.PINNED_COMMIT,
                  "version": grail.VERSION, "kernel_sha256": grail.KERNEL_SHA256,
                  "inventory_sha256": _sha256_file(data / "grail-inventory.json"),
                  "inventory_files": len(grail.load_inventory())},
        "worker_lock_sha256": _sha256_file(data / "grail-requirements.lock"),
        "bridge_sha256": _sha256_file(root / "bridge" / "rapp_bridge_agent.py"),
        "store": {"schema_version": state.SCHEMA_VERSION,
                  "reads": [{"schema_version": state.SCHEMA_VERSION,
                             "schema_sha256": state.SCHEMA_DIGEST}],
                  "migrates_from": [dict(item) for item in state.MIGRATES_FROM]},
        "capabilities": sorted(ALL_CAPABILITIES),
        "dynamic_capabilities": ["mcp.<server> for each server in reach.json"],
        "files": files,
        "tree_sha256": tree_sha256(files),
    }


def render_manifest(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def load_manifest(root: Path = PACKAGE) -> dict[str, Any]:
    path = Path(root) / "data" / MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError(f"No release manifest ({path.name}) in {root}.") from None
    except (OSError, ValueError) as error:
        raise ReleaseError(f"The release manifest in {root} is unreadable: {error}") from None
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT or not \
            isinstance(manifest.get("files"), dict):
        raise ReleaseError(f"The release manifest in {root} has an unknown format.")
    return manifest


def _version_id(manifest: Mapping[str, Any]) -> str:
    return f"{manifest['version']}-{str(manifest['tree_sha256'])[:12]}"


def verify_tree(root: Path = PACKAGE) -> dict[str, Any]:
    """Check a package copy against its own manifest: every listed file present and
    identical, nothing unlisted, and the manifest's tree digest consistent."""
    manifest = load_manifest(root)
    try:
        actual = package_files(root)
    except (OSError, ReleaseError) as error:
        return {"ok": False, "version": manifest.get("version"), "problems": [str(error)]}
    listed = manifest["files"]
    missing = sorted(set(listed) - set(actual))
    changed = sorted(name for name in listed if name in actual and actual[name] != listed[name])
    extra = sorted(set(actual) - set(listed))
    consistent = tree_sha256(listed) == manifest.get("tree_sha256")
    problems = ([f"{name} (missing)" for name in missing] + [f"{name} (sha256 differs)"
                                                             for name in changed]
                + [f"{name} (not in the manifest)" for name in extra]
                + ([] if consistent else ["the manifest's tree digest does not match its files"]))
    return {"ok": not problems, "version": manifest.get("version"), "id": _version_id(manifest),
            "tree_sha256": manifest.get("tree_sha256"), "files": len(listed),
            "problems": problems[:20]}


def identity(root: Path = PACKAGE) -> dict[str, Any]:
    """The running (or given) package's version identity, read from its manifest."""
    try:
        manifest = load_manifest(root)
    except ReleaseError:
        return {"version": __version__, "tree_sha256": None, "id": f"{__version__}-unreleased"}
    return {"version": manifest["version"], "tree_sha256": manifest["tree_sha256"],
            "id": _version_id(manifest)}


# -- side-by-side versions in a home --------------------------------------------------------
def versions_dir(home: Path | str) -> Path:
    return Path(home) / "versions"


def _write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(dict(document), handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        try:
            document = json.loads(handle.read(1 << 20))
        except ValueError:
            return None
    return document if isinstance(document, dict) else None


def read_active(home: Path | str) -> dict | None:
    document = _read_json(versions_dir(home) / ACTIVE)
    return document if document and isinstance(document.get("active"), str) else None


def write_active(home: Path | str, document: Mapping[str, Any]) -> None:
    folder = versions_dir(home)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private_json(folder / ACTIVE, document)


def installed_versions(home: Path | str) -> list[dict]:
    folder, found = versions_dir(home), []
    if not folder.is_dir():
        return found
    for entry in sorted(folder.iterdir()):
        marker = _read_json(entry / MARKER) if entry.is_dir() else None
        if marker and marker.get("id") == entry.name:
            found.append({**marker, "path": str(entry)})
    return found


def _make_private_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def install_tree(home: Path | str, source_root: Path, *, origin: str) -> dict[str, Any]:
    """Copy the verified package at ``source_root/brainstem_agent`` into
    ``versions/<version>-<tree12>/`` (idempotent). The copy is verified again before it is
    renamed into place, so a file changed during the copy never becomes a version."""
    source = Path(source_root) / PACKAGE_NAME
    check = verify_tree(source)
    if not check["ok"]:
        raise ReleaseError("The release's files do not match its manifest: "
                           + "; ".join(check["problems"][:5]))
    folder = versions_dir(home)
    _make_private_dirs(folder)
    target = folder / check["id"]
    marker = _read_json(target / MARKER)
    if marker and marker.get("id") == check["id"] and verify_tree(target / PACKAGE_NAME)["ok"]:
        return {**marker, "path": str(target), "already_installed": True}
    staging = folder / f".staging-{secrets.token_hex(6)}"
    try:
        manifest = load_manifest(source)
        for name in [*manifest["files"], f"{PACKAGE_NAME}/data/{MANIFEST_NAME}"]:
            relative = PurePosixPath(name)
            destination = staging.joinpath(*relative.parts)
            _make_private_dirs(destination.parent)
            shutil.copyfile(Path(source_root).joinpath(*relative.parts), destination)
            os.chmod(destination, 0o444)
        again = verify_tree(staging / PACKAGE_NAME)
        if not again["ok"] or again["id"] != check["id"]:
            raise ReleaseError("The release changed while it was being copied: "
                               + "; ".join(again["problems"][:5]))
        marker = {"id": check["id"], "version": check["version"],
                  "tree_sha256": check["tree_sha256"], "files": check["files"],
                  "origin": origin, "installed_at": time.time()}
        _write_private_json(staging / MARKER, marker)
        if target.exists():
            _remove(target)
        os.rename(staging, target)
    finally:
        if staging.exists():
            _remove(staging)
    return {**marker, "path": str(target), "already_installed": False}


def _remove(path: Path) -> None:
    for directory, dirs, _files in os.walk(path):
        for name in dirs:
            try:
                os.chmod(os.path.join(directory, name), 0o700, follow_symlinks=False)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def stage_candidate(source: Path | str, work: Path) -> Path:
    """The root (a directory holding ``brainstem_agent/``) of a local candidate release: a
    repository checkout, its ``runtime/`` directory, the package directory itself, or a
    zipapp or wheel file (unpacked into ``work`` safely). Nothing is fetched."""
    source = Path(source).expanduser()
    if source.is_dir():
        for root in (source / "runtime", source, source.parent):
            if (root / PACKAGE_NAME / "__init__.py").is_file() and \
                    (root != source.parent or source.name == PACKAGE_NAME):
                return root
        raise ReleaseError(f"{source} holds no {PACKAGE_NAME} package.")
    if not source.is_file():
        raise ReleaseError(f"{source} does not exist.")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile):
        raise ReleaseError(f"{source} is neither a directory nor a zipapp or wheel.") from None
    with archive:
        try:
            manifest = json.loads(archive.read(f"{PACKAGE_NAME}/data/{MANIFEST_NAME}"))
        except (KeyError, ValueError):
            raise ReleaseError(f"{source} carries no readable release manifest.") from None
        wanted = set(manifest.get("files") or {}) | {f"{PACKAGE_NAME}/data/{MANIFEST_NAME}"}
        for info in archive.infolist():
            name = info.filename
            if name not in wanted:
                continue
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or info.is_dir() or \
                    info.file_size > _MAX_MEMBER or (info.external_attr >> 16) & 0o170000 \
                    not in (0, stat.S_IFREG):
                raise ReleaseError(f"{source} holds an unsafe member: {name}")
            destination = Path(work).joinpath(*parts)
            _make_private_dirs(destination.parent)
            destination.write_bytes(archive.read(info))
    return Path(work)


def redirect_target(home: Path | str, environ: Mapping[str, str]) -> tuple[Path | None, str | None]:
    """Where a command should run instead of this code: the home's active version, when this
    code is a version the home has moved away from. Returns (version root or None, note)."""
    if environ.get("BRAINSTEM_AGENT_NO_REDIRECT"):
        return None, None
    active = read_active(home)
    if active is None:
        return None, None
    running = identity()
    wanted = active["active"]
    if wanted == running["id"]:
        return None, None
    root = versions_dir(home) / wanted
    if _read_json(root / MARKER) is None:
        return None, (f"this home's active version {wanted} is missing; this command runs "
                      f"{running['id']} (fix: brainstem-agent rollback, or upgrade again)")
    if not (versions_dir(home) / running["id"]).is_dir():
        return None, (f"this home's active version is {wanted}, but this command runs "
                      f"{running['id']}, which the home has not installed; it runs as is "
                      "(to make it active: brainstem-agent upgrade --from <this release>)")
    if environ.get("BRAINSTEM_AGENT_REDIRECTED") == wanted:
        return None, f"the active version {wanted} did not take over; running {running['id']}"
    return root, None


def install_kind(package: Path = PACKAGE, home: Path | str | None = None) -> str:
    if home is not None and versions_dir(home) in package.parents:
        return "home-version"
    if "site-packages" in package.parts or "dist-packages" in package.parts:
        return "pip"
    return "source" if (package.parent / "pyproject.toml").is_file() else "unknown"


def version_info(home: Path | str | None = None, *, verify: bool = True) -> dict[str, Any]:
    """Everything that identifies this installation (``version --json``)."""
    from . import grail, state
    from .host import ALL_CAPABILITIES

    data = PACKAGE / "data"
    running = identity()
    document: dict[str, Any] = {
        "product": "Brainstem Agent", "version": __version__, "version_id": running["id"],
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.mac_ver()[0] or platform.release()} "
                    f"{platform.machine()}",
        "install": {"kind": install_kind(PACKAGE, home), "package": str(PACKAGE),
                    "interpreter": sys.executable},
        "grail": {"repository": "kody-w/rapp-installer", "commit": grail.PINNED_COMMIT,
                  "version": grail.VERSION, "kernel_sha256": grail.KERNEL_SHA256,
                  "inventory_sha256": _sha256_file(data / "grail-inventory.json"),
                  "inventory_files": len(grail.load_inventory())},
        "store": {"schema_version": state.SCHEMA_VERSION, "schema_sha256": state.SCHEMA_DIGEST,
                  "migrates_from": [dict(item) for item in state.MIGRATES_FROM]},
        "search_index": {"engine": "sqlite-fts5 (derived, rebuilt from the store)"},
        "bridge_sha256": _sha256_file(PACKAGE / "bridge" / "rapp_bridge_agent.py"),
        "worker_lock_sha256": _sha256_file(data / "grail-requirements.lock"),
        "capabilities": sorted(ALL_CAPABILITIES),
    }
    try:
        manifest = load_manifest()
        document["release_manifest"] = {"format": manifest["format"],
                                        "tree_sha256": manifest["tree_sha256"],
                                        "files": len(manifest["files"])}
        if verify:
            check = verify_tree()
            document["release_manifest"].update(verified=check["ok"], problems=check["problems"])
    except ReleaseError as error:
        document["release_manifest"] = {"verified": False, "problems": [str(error)]}
    if home is not None:
        store = state.inspect_database(Path(home) / "state" / "agent.sqlite3")
        document["store"]["home_store"] = store
        active = read_active(home)
        document["versions"] = {"active": active["active"] if active else None,
                                "previous": active.get("previous") if active else None,
                                "installed": [item["id"] for item in installed_versions(home)]}
    return document


# -- the zipapp ------------------------------------------------------------------------------
LAUNCHER = r'''"""Brainstem Agent single-file release: verify, unpack into the home's versions, run."""
import hashlib
import json
import os
import secrets
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

PACKAGE = "brainstem_agent"
MANIFEST = PACKAGE + "/data/release-manifest.json"


def main():
    if sys.version_info < (3, 11):
        sys.exit("Brainstem Agent needs Python 3.11 or newer.")
    archive = Path(__file__).resolve().parent
    home = Path(os.environ.get("BRAINSTEM_AGENT_HOME") or os.path.expanduser("~/.brainstem-agent"))
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read(MANIFEST))
        version_id = "%s-%s" % (manifest["version"], manifest["tree_sha256"][:12])
        versions = home / "versions"
        target = versions / version_id
        if not (target / ".verified.json").is_file():
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            versions.mkdir(exist_ok=True, mode=0o700)
            staging = versions / (".staging-" + secrets.token_hex(6))
            try:
                for name in [*manifest["files"], MANIFEST]:
                    data = bundle.read(name)
                    expected = manifest["files"].get(name)
                    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
                        sys.exit("Brainstem Agent: %s does not match the release manifest; "
                                 "this file is damaged." % name)
                    destination = staging.joinpath(*PurePosixPath(name).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.write_bytes(data)
                    os.chmod(destination, 0o444)
                marker = {"id": version_id, "version": manifest["version"],
                          "tree_sha256": manifest["tree_sha256"], "files": len(manifest["files"]),
                          "origin": "zipapp"}
                (staging / ".verified.json").write_text(json.dumps(marker))
                os.chmod(staging / ".verified.json", 0o600)
                try:
                    os.rename(staging, target)
                except OSError:
                    if not (target / ".verified.json").is_file():
                        raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    sys.path.insert(0, str(target))
    from brainstem_agent.cli import main as cell_main
    return cell_main(sys.argv[1:], redirect=True)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def build_zipapp(output: Path | str, *, root: Path = PACKAGE) -> dict[str, Any]:
    """Write the zipapp (deterministic bytes) for the verified package at ``root``."""
    check = verify_tree(root)
    if not check["ok"]:
        raise ReleaseError("The package does not match its release manifest (regenerate it "
                           "with python -m brainstem_agent.release --write): "
                           + "; ".join(check["problems"][:5]))
    output = Path(output)
    if output.exists():
        raise ReleaseError(f"{output} already exists.")
    manifest = load_manifest(root)
    names = sorted([*manifest["files"], f"{PACKAGE_NAME}/data/{MANIFEST_NAME}"])
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                def add(name: str, data: bytes) -> None:
                    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    bundle.writestr(info, data)
                add("__main__.py", LAUNCHER.encode("utf-8"))
                for name in names:
                    add(name, Path(root).parent.joinpath(*PurePosixPath(name).parts).read_bytes())
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "path": str(output), "version": manifest["version"],
            "version_id": _version_id(manifest), "files": len(names),
            "sha256": _sha256_file(output), "bytes": output.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brainstem_agent.release",
                                     description="Produce or check the release manifest; build "
                                                 "the zipapp.")
    parser.add_argument("--write", action="store_true",
                        help="write data/release-manifest.json from the tree")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 unless the committed manifest matches the tree")
    parser.add_argument("--zipapp", type=Path, help="build the zipapp at this new path")
    arguments = parser.parse_args(argv)
    if arguments.write:
        MANIFEST_PATH.write_text(render_manifest(build_manifest()), encoding="utf-8")
        print(f"wrote {MANIFEST_PATH.name} ({identity()['id']})")
    if arguments.check or not (arguments.write or arguments.zipapp):
        expected = render_manifest(build_manifest())
        actual = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else ""
        if actual != expected:
            print("the release manifest does not match the tree; run: "
                  "python -m brainstem_agent.release --write", file=sys.stderr)
            return 1
        print(f"release manifest matches the tree ({identity()['id']})")
    if arguments.zipapp:
        print(json.dumps(build_zipapp(arguments.zipapp), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
