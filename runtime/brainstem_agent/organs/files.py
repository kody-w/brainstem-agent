"""Files organ: workspace-relative file tools that never follow links or leave the root.

Every path is resolved component by component with ``O_NOFOLLOW`` directory
descriptors, so absolute paths, ``..``, symlinked directories, symlinked leaves
and hard links to other files are all refused. Reads refuse FIFOs and other
special files without opening or blocking on them, and listings never report the
metadata of a hard-linked file (it may be another file's inode).
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from typing import Any, Mapping

from .base import BindContext, InvocationContext, OrganError, ToolResult, ToolSpec, clip

_MAX_READ = 256 * 1024
_MAX_LIST = 500
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _parts(path: Any, *, allow_root: bool = False) -> list[str]:
    if not isinstance(path, str) or not path:
        raise OrganError("A workspace-relative path is required.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or "\\" in path:
        raise OrganError("Paths must not contain control characters or backslashes.")
    if path.startswith("/") or path.startswith("~"):
        raise OrganError("Absolute paths are refused; use a path relative to the workspace.")
    if allow_root and path in (".", "./"):
        return []
    parts = path.split("/")
    if parts and parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    if any(part in ("", ".", "..") for part in parts):
        raise OrganError("Paths must not contain '..', '.' or empty components.")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise OrganError("A path component is too long.")
    return parts


def _open_directory(root: os.PathLike, parts: list[str], *, create: bool = False) -> int:
    try:
        descriptor = os.open(root, _DIR_FLAGS)
    except OSError as error:
        raise OrganError("The workspace directory is unavailable.") from error
    for name in parts:
        try:
            child = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
        except FileNotFoundError:
            if not create:
                os.close(descriptor)
                raise OrganError("No such directory in the workspace.") from None
            try:
                os.mkdir(name, 0o755, dir_fd=descriptor)
                child = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
            except OSError as error:
                os.close(descriptor)
                raise OrganError("Cannot create that directory in the workspace.") from error
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                raise OrganError("Refusing to follow a symlink or non-directory in the path.") from None
            raise OrganError("Cannot open that directory in the workspace.") from None
        os.close(descriptor)
        descriptor = child
    return descriptor


def _leaf_info(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _check_readable(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise OrganError("Refusing to read through a symlink.")
    if not stat.S_ISREG(info.st_mode):
        raise OrganError("That path is not a regular file.")
    if info.st_nlink > 1:
        raise OrganError("Refusing to read a hard-linked file.")


class FilesOrgan:
    name = "files"

    def tools(self) -> list[ToolSpec]:
        path = {"type": "string", "minLength": 1, "maxLength": 1024,
                "description": "Path relative to the workspace root, e.g. notes/todo.txt"}
        return [
            ToolSpec("write_file", "Create or replace a text file in the workspace. The content is "
                     "written exactly as given (UTF-8); no newline is added.",
                     {"type": "object", "properties": {
                         "path": path,
                         "content": {"type": "string", "maxLength": 1_000_000,
                                     "description": "Exact file content."}},
                      "required": ["path", "content"]}, "files.write", "write"),
            ToolSpec("read_file", "Read a text file from the workspace.",
                     {"type": "object", "properties": {"path": path}, "required": ["path"]},
                     "files.read", "read"),
            # Every tool requires an argument: unchanged Grail refuses the empty argument
            # string models stream for a tool that requires none. '.' lists the root.
            ToolSpec("list_files", "List a directory in the workspace ('.' is the root).",
                     {"type": "object", "properties": {"path": {
                         **path, "default": ".",
                         "description": "Directory relative to the workspace root; '.' for "
                                        "the root."}},
                      "required": ["path"]},
                     "files.read", "read"),
        ]

    def context(self, context: BindContext) -> str | None:
        return None

    def invoke(self, context: InvocationContext, tool: str,
               arguments: Mapping[str, Any]) -> ToolResult:
        context.check()
        if tool == "write_file":
            return self._write(context, arguments["path"], arguments["content"])
        if tool == "read_file":
            return self._read(context, arguments["path"])
        if tool == "list_files":
            return self._list(context, arguments.get("path", "."))
        raise OrganError(f"Unknown files tool {tool!r}.")

    def _write(self, context: InvocationContext, path: str, content: str) -> ToolResult:
        parts = _parts(path)
        payload = content.encode("utf-8")
        directory = _open_directory(context.workspace_root, parts[:-1], create=True)
        leaf = parts[-1]
        temporary = f".{leaf[:40]}.{secrets.token_hex(6)}.tmp"
        try:
            info = _leaf_info(directory, leaf)
            if info is not None and not stat.S_ISREG(info.st_mode):
                raise OrganError("Refusing to write through a symlink or over a non-file.")
            if info is not None and info.st_nlink > 1:
                raise OrganError("Refusing to write a hard-linked file.")
            context.check()
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                 | os.O_CLOEXEC, 0o644, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
                raise
        finally:
            os.close(directory)
        digest = hashlib.sha256(payload).hexdigest()
        return ToolResult(f"Wrote {len(payload)} bytes to {path}.",
                          evidence={"path": path, "bytes": len(payload), "sha256": digest})

    def _read(self, context: InvocationContext, path: str) -> ToolResult:
        parts = _parts(path)
        directory = _open_directory(context.workspace_root, parts[:-1])
        try:
            info = _leaf_info(directory, parts[-1])
            if info is None:
                raise OrganError(f"No such file in the workspace: {path}")
            _check_readable(info)
            # O_NONBLOCK: a leaf swapped for a FIFO after the check must not block the open.
            try:
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                     | os.O_CLOEXEC, dir_fd=directory)
            except FileNotFoundError:
                raise OrganError(f"No such file in the workspace: {path}") from None
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise OrganError("Refusing to read through a symlink.") from None
                raise OrganError("Cannot open that file in the workspace.") from None
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            _check_readable(info)
            data = handle.read(_MAX_READ + 1)
        truncated = len(data) > _MAX_READ
        text = data[:_MAX_READ].decode("utf-8", "replace")
        note = f" (first {_MAX_READ} bytes)" if truncated else ""
        full = f"{path} ({info.st_size} bytes){note}:\n{text}"
        shown = clip(full)
        try:
            data.decode("utf-8")
            lossless = True
        except UnicodeDecodeError:
            lossless = False
        # ``exact``: the result holds the whole file, byte for byte (scripts rely on it).
        return ToolResult(shown, evidence={
            "path": path, "bytes": info.st_size, "truncated": truncated,
            "exact": not truncated and lossless and shown == full})

    def _list(self, context: InvocationContext, path: str) -> ToolResult:
        parts = _parts(path, allow_root=True)
        directory = _open_directory(context.workspace_root, parts)
        try:
            names = sorted(os.listdir(directory))
            lines = []
            for name in names[:_MAX_LIST]:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    lines.append(name + "/")
                elif stat.S_ISLNK(info.st_mode):
                    lines.append(name + " (symlink, not followed)")
                elif not stat.S_ISREG(info.st_mode):
                    lines.append(name + " (not a regular file)")
                elif info.st_nlink > 1:
                    lines.append(name + " (hard link, refused)")
                else:
                    lines.append(f"{name} ({info.st_size} bytes)")
        finally:
            os.close(directory)
        if len(names) > _MAX_LIST:
            lines.append(f"... {len(names) - _MAX_LIST} more entries")
        shown = path if parts else "."
        return ToolResult(f"{shown}:\n" + ("\n".join(lines) if lines else "(empty)"),
                          evidence={"path": shown, "entries": len(names)})
