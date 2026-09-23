"""Seatbelt membrane: render macOS sandbox profiles and wrap commands in them.

There is no silent fallback. Callers that need isolation must refuse to run when
``available()`` is false. Paths are canonicalised (``paths.canonical``: links resolved,
then the kernel's own spelling) because Seatbelt matches resolved vnode paths: ``/var`` is
``/private/var``, and a rule spelled through a firmlink
(``/System/Volumes/Data/Users/...``) would match nothing.

Precedence (measured on macOS 27): a rule for a specific operation beats a
wildcard rule regardless of order, and among rules for the same operation the
later one wins. Every deny and its re-allow therefore name exactly the same
operation, laid out as deny-read < re-allow-read (``file-read-data``),
deny-all-writes < allow-writable < deny-protected (``file-write*``), deny <
allow for ``mach-lookup`` and ``signal``, and one network operation.

Beyond files and network, every profile is least-privilege: Mach service lookups
are denied except the directory service ordinary tools need for user and group
names, so desktop services such as LaunchServices, preferences, the keychain,
the pasteboard and Spotlight are unreachable (name resolution needs no Mach
service: it uses mDNSResponder's socket, which the network rules govern); and
signals reach only processes in the same sandbox.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .paths import canonical

__all__ = ["ENVIRONMENT", "SandboxPolicy", "SandboxUnavailable", "available", "render", "wrap"]

ENVIRONMENT = "macos-seatbelt"
DEFAULT_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_NETWORK = frozenset({"none", "outbound"})
_DEVICES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/urandom", "/dev/random")
# getpwuid/getgrgid and friends (ls -l, id, git, python, tar) go through this service.
_MACH_SERVICES = ("com.apple.system.opendirectoryd.libinfo",)


class SandboxUnavailable(RuntimeError):
    """The Seatbelt sandbox cannot be used on this host."""


@dataclass(frozen=True)
class SandboxPolicy:
    read_denied: tuple[Path, ...] = ()
    readable: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()
    write_denied: tuple[Path, ...] = ()
    network: str = "none"
    loopback_ports: tuple[int, ...] = ()


def sandbox_exec_path(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("BRAINSTEM_AGENT_SANDBOX_EXEC") or DEFAULT_SANDBOX_EXEC


def available(environ: Mapping[str, str] | None = None) -> bool:
    path = sandbox_exec_path(environ)
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _path(value: Path | str) -> str:
    raw = os.fspath(value)
    if not os.path.isabs(raw):
        raise ValueError("Sandbox paths must be absolute")
    resolved = str(canonical(raw))
    if any(ch in resolved for ch in '"\\') or any(ord(ch) < 32 for ch in resolved):
        raise ValueError("Sandbox paths must not contain quotes, backslashes or controls")
    return resolved


def _filters(kind: str, paths: Sequence[Path | str]) -> str:
    return " ".join(f'({kind} "{_path(item)}")' for item in paths)


def render(policy: SandboxPolicy) -> str:
    if policy.network not in _NETWORK:
        raise ValueError(f"network must be one of {sorted(_NETWORK)}")
    ports = []
    for port in policy.loopback_ports:
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError("loopback ports must be TCP port numbers")
        ports.append(port)
    lines = ["(version 1)", "(allow default)"]
    if policy.read_denied:
        lines.append(f"(deny file-read-data {_filters('subpath', policy.read_denied)})")
    if policy.readable:
        lines.append(f"(allow file-read-data {_filters('subpath', policy.readable)})")
    lines.append("(deny file-write*)")
    writable = _filters("subpath", policy.writable)
    devices = _filters("literal", _DEVICES)
    lines.append(f'(allow file-write* {writable} {devices} (regex #"^/dev/tty") (regex #"^/dev/fd/"))')
    for item in policy.write_denied:
        path = _path(item)
        kind = "subpath" if os.path.isdir(path) else "literal"
        lines.append(f'(deny file-write* ({kind} "{path}"))')
    if policy.network == "none":
        lines.append("(deny network*)")
    else:
        lines.append('(deny network-outbound (remote ip "localhost:*"))')
        for port in ports:
            lines.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    names = " ".join(f'"{name}"' for name in _MACH_SERVICES)
    lines.append("(deny mach-lookup)")
    lines.append(f"(allow mach-lookup (global-name {names}))")
    lines.append("(deny signal)")
    lines.append("(allow signal (target same-sandbox))")
    return "\n".join(lines) + "\n"


def wrap(argv: Sequence[str], policy: SandboxPolicy, *, profile_path: Path | None = None,
         environ: Mapping[str, str] | None = None) -> list[str]:
    """Return argv that runs ``argv`` inside the rendered profile."""
    if not available(environ):
        raise SandboxUnavailable(
            "The Seatbelt sandbox (/usr/bin/sandbox-exec) is unavailable; refusing to run "
            "without isolation.")
    profile = render(policy)
    executable = sandbox_exec_path(environ)
    if profile_path is None:
        return [executable, "-p", profile, *argv]
    descriptor = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(profile)
    return [executable, "-f", str(profile_path), *argv]
