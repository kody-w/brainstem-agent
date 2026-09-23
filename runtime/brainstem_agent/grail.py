"""The captured Grail genome: pinned source, byte-for-byte verification, worker venv.

Grail source is never vendored here; only its hash inventory ships in
``data/grail-inventory.json``. The cache comes from a verified local seed or the
public codeload tarball, and every tracked file must match before use.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

__all__ = [
    "GrailSource", "GrailSourceError", "KERNEL_SHA256", "PINNED_COMMIT", "VERSION",
    "check_worker_venv", "ensure_grail_source", "ensure_worker_venv", "extract_tarball",
    "load_inventory", "verify_tree", "worker_venv_python",
]

DATA = Path(__file__).resolve().parent / "data"
PINNED_COMMIT = "49db80c8c6b6caa7647369beaf477d374a8f293c"
KERNEL_SHA256 = "bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4"
VERSION = "0.6.16"
CODELOAD = "https://codeload.github.com/kody-w/rapp-installer/tar.gz/{commit}"
LOCK_FILE = DATA / "grail-requirements.lock"
_MAX_MEMBER = 16 * 1024 * 1024
_MARKER = ".brainstem-agent-venv.json"


class GrailSourceError(RuntimeError):
    """The Grail source or worker interpreter is missing, unverifiable or tampered."""


@dataclass(frozen=True)
class GrailSource:
    commit: str
    root: Path
    inventory: Mapping[str, str]


def load_inventory() -> dict[str, str]:
    document = json.loads((DATA / "grail-inventory.json").read_text(encoding="utf-8"))
    if document.get("commit") != PINNED_COMMIT or document["kernel"]["sha256"] != KERNEL_SHA256:
        raise GrailSourceError("The shipped Grail inventory does not match the pinned commit.")
    return dict(document["files"])


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_tree(root: Path, inventory: Mapping[str, str], *, allow_extra: bool) -> list[str]:
    """Verify every tracked file byte for byte; return sorted untracked paths."""
    root = Path(root)
    problems = []
    for relative, expected in sorted(inventory.items()):
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            problems.append(f"{relative} (missing)")
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(f"{relative} (not a regular file)")
        elif _digest(path) != expected:
            problems.append(f"{relative} (sha256 differs)")
    untracked = []
    for directory, dirs, files in os.walk(root):
        base = Path(directory)
        for name in list(dirs):
            if (base / name).is_symlink():
                untracked.append((base / name).relative_to(root).as_posix())
                dirs.remove(name)
        for name in files:
            relative = (base / name).relative_to(root).as_posix()
            if relative not in inventory:
                untracked.append(relative)
    if problems:
        raise GrailSourceError("Grail source mismatch: " + ", ".join(problems))
    if untracked and not allow_extra:
        raise GrailSourceError("Grail source has untracked files: " + ", ".join(sorted(untracked)))
    return sorted(untracked)


def _make_read_only(root: Path) -> None:
    for directory, _dirs, files in os.walk(root):
        for name in files:
            os.chmod(os.path.join(directory, name), 0o444)


def _install(staging: Path, target: Path, inventory: Mapping[str, str]) -> None:
    verify_tree(staging, inventory, allow_extra=False)
    _make_read_only(staging)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.rename(staging, target)


def ensure_grail_source(cache: Path, *, seed_dir: Path | None = None, fetch: bool = True,
                        timeout: float = 120.0) -> GrailSource:
    """Verify the cached source, else build it from a verified seed or codeload."""
    cache = Path(cache)
    inventory = load_inventory()
    target = cache / "grail" / PINNED_COMMIT / "rapp_brainstem"
    if target.exists():
        verify_tree(target, inventory, allow_extra=False)
        return GrailSource(PINNED_COMMIT, target, inventory)
    if seed_dir is None and not fetch:
        raise GrailSourceError(
            f"Grail source is not prepared in {cache}. Run: brainstem-agent setup")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_parent = cache / "grail" / f".staging-{secrets.token_hex(6)}"
    staging_parent.mkdir(parents=True, mode=0o700)
    try:
        if seed_dir is not None:
            staging = staging_parent / "rapp_brainstem"
            for relative, expected in inventory.items():
                source = Path(seed_dir) / relative
                if not source.is_file() or source.is_symlink() or _digest(source) != expected:
                    raise GrailSourceError(f"Grail seed does not match the pinned inventory: {relative}")
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(CODELOAD.format(commit=PINNED_COMMIT), timeout=timeout) as response:
                data = response.read(256 * 1024 * 1024)
            staging = extract_tarball(data, staging_parent, inventory)
        _install(staging, target, inventory)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    return GrailSource(PINNED_COMMIT, target, inventory)


def _member_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute() or name.startswith("/") or ".." in path.parts or "\\" in name:
        raise GrailSourceError(f"Unsafe archive member: {name!r}")
    return path.parts


def extract_tarball(data: bytes, destination: Path, inventory: Mapping[str, str],
                    commit: str = PINNED_COMMIT) -> Path:
    """Extract only regular inventory files under <top>/rapp_brainstem/; refuse anything unsafe."""
    destination = Path(destination)
    root = destination / "rapp_brainstem"
    seen = set()
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as error:
        raise GrailSourceError("The Grail archive is not a readable tarball.") from error
    with archive:
        for member in archive:
            parts = _member_parts(member.name)
            if len(parts) < 2 or parts[1] != "rapp_brainstem":
                continue
            relative = "/".join(parts[2:])
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise GrailSourceError(f"Refusing link or device in the Grail archive: {member.name}")
            if member.isdir() or not relative:
                continue
            if not member.isfile():
                raise GrailSourceError(f"Refusing unusual archive member: {member.name}")
            if relative in seen:
                raise GrailSourceError(f"Duplicate archive member: {member.name}")
            seen.add(relative)
            if relative not in inventory:
                continue
            if member.size > _MAX_MEMBER:
                raise GrailSourceError(f"Archive member is too large: {member.name}")
            handle = archive.extractfile(member)
            payload = handle.read() if handle else b""
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    verify_tree(root, inventory, allow_extra=False)
    return root


def _python_version(python: str) -> tuple[int, int]:
    result = subprocess.run([python, "-c", "import sys; print(*sys.version_info[:2])"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise GrailSourceError(f"Worker interpreter {python} cannot run.")
    major, minor = map(int, result.stdout.split())
    return major, minor


def _venv_dir(cache: Path, python: str) -> tuple[Path, tuple[int, int]]:
    version = _python_version(python)
    if version < (3, 11):
        raise GrailSourceError("The Grail worker interpreter needs Python 3.11 or newer.")
    lock = hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest()
    return Path(cache) / "venvs" / f"{lock[:16]}-py{version[0]}{version[1]}", version


def _choose_python(python: str | None, environ: Mapping[str, str] | None) -> str:
    environ = os.environ if environ is None else environ
    return python or environ.get("BRAINSTEM_AGENT_PYTHON") or sys.executable


_IMPORT_CHECK = "import flask, flask_cors, requests, dotenv, pyzipper"


def worker_venv_python(cache: Path, *, python: str | None = None,
                       environ: Mapping[str, str] | None = None) -> Path:
    """Return the prepared worker interpreter; never builds or installs anything."""
    venv, _ = _venv_dir(cache, _choose_python(python, environ))
    marker = venv / _MARKER
    try:
        document = json.loads(marker.read_text())
    except (OSError, ValueError):
        raise GrailSourceError(
            f"The worker interpreter is not prepared in {venv}. "
            "Run: brainstem-agent setup") from None
    if document.get("lock_sha256") != hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest():
        raise GrailSourceError("The worker interpreter was built from a different lock.")
    return venv / "bin" / "python"


def check_worker_venv(cache: Path, *, python: str | None = None,
                      environ: Mapping[str, str] | None = None) -> dict:
    try:
        executable = worker_venv_python(cache, python=python, environ=environ)
        result = subprocess.run([str(executable), "-I", "-c", _IMPORT_CHECK + "; import sys; "
                                 "print('.'.join(map(str, sys.version_info[:3])))"],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise GrailSourceError("The worker interpreter cannot import Grail's requirements.")
    except GrailSourceError as error:
        return {"ok": False, "python": None, "version": None, "hash_locked": False,
                "detail": str(error)}
    return {"ok": True, "python": str(executable), "version": result.stdout.strip(),
            "hash_locked": True, "detail": "private venv installed with --require-hashes"}


def ensure_worker_venv(cache: Path, *, python: str | None = None,
                       environ: Mapping[str, str] | None = None) -> Path:
    """Build the private worker venv once from the hash lock (wheels only, no deps resolution)."""
    chosen = _choose_python(python, environ)
    venv, version = _venv_dir(cache, chosen)
    venv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(venv.parent / ".build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return worker_venv_python(cache, python=chosen, environ=environ)
        except GrailSourceError:
            pass
        shutil.rmtree(venv, ignore_errors=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "LANG": "en_US.UTF-8",
               "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
        steps = [
            [chosen, "-m", "venv", str(venv)],
            [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", "--require-hashes",
             "--no-deps", "--only-binary=:all:", "-r", str(LOCK_FILE)],
            [str(venv / "bin" / "python"), "-I", "-c", _IMPORT_CHECK],
        ]
        for step in steps:
            result = subprocess.run(step, capture_output=True, text=True, env=env, timeout=900)
            if result.returncode != 0:
                shutil.rmtree(venv, ignore_errors=True)
                tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
                raise GrailSourceError(f"Building the worker interpreter failed: {tail[0][:300]}")
        (venv / _MARKER).write_text(json.dumps({
            "lock_sha256": hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest(),
            "python": ".".join(map(str, version)),
        }))
    return venv / "bin" / "python"
