"""One place per path: how the kernel names a directory, so aliases never split or bypass.

macOS reaches one directory through many spellings: a symlinked parent (``/var`` is
``/private/var``), letter case on a case-insensitive volume, and firmlinks
(``/System/Volumes/Data/Users/...`` is ``/Users/...``). ``os.path.realpath`` resolves
only the first. ``canonical`` also asks the kernel for the name it reports for the open
directory (``F_GETPATH``), which Seatbelt rules and workspace namespaces then share; and
``holds`` compares places by device and inode, so a guard never depends on how a path
was spelled.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

__all__ = ["canonical", "holds", "identity", "lineage"]

_GETPATH = getattr(fcntl, "F_GETPATH", None)


def identity(path: os.PathLike | str) -> tuple[int, int] | None:
    """(device, inode) of what ``path`` names (links followed), or None if it is absent."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return info.st_dev, info.st_ino


def _reported(directory: str) -> str | None:
    """The kernel's own name for an existing directory (on-disk case, no firmlink)."""
    if _GETPATH is None:
        return None
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(descriptor, _GETPATH, bytes(1024))
    except OSError:
        return None
    finally:
        os.close(descriptor)
    name = os.fsdecode(raw.split(b"\0", 1)[0])
    return name if name.startswith("/") and identity(name) == identity(directory) else None


def canonical(path: os.PathLike | str) -> Path:
    """``path`` with links resolved and its deepest existing directory spelled as the kernel
    reports it; a missing tail (or a file name) is kept as given."""
    head, tail = os.path.realpath(os.fspath(path)), []
    while head != os.path.dirname(head) and not os.path.isdir(head):
        head, leaf = os.path.split(head)
        tail.append(leaf)
    return Path(_reported(head) or head, *reversed(tail))


def lineage(path: os.PathLike | str) -> set[tuple[int, int]]:
    """Identities of ``path`` and every existing directory above it."""
    found, spelled = set(), canonical(path)
    for place in (spelled, *spelled.parents):
        mark = identity(place)
        if mark is not None:
            found.add(mark)
    return found


def holds(outer: os.PathLike | str, inner: os.PathLike | str) -> bool:
    """True when ``outer`` is ``inner`` or one of the directories above it, whatever either
    is called (``inner`` need not exist yet)."""
    mark = identity(outer)
    return mark is not None and mark in lineage(inner)
