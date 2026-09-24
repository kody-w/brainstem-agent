"""The Copilot credential's lifecycle as the cell sees it: an explicit invalid state.

Grail exchanges the installed brainstem's GitHub token for Copilot access. When GitHub
rejects it (revoked, expired or replaced elsewhere) or the account has no Copilot access,
the cell records that in ``state/credential.json`` (0600): the kind, a reason, when, and the
credential file's identity (device, inode, size and modification time) - never the
credential or anything derived from its value. While the state holds, new turns are refused
at once with guidance (no Grail request, no worker start) and the daemon stops warming
workers, so nothing retries in a loop. The state clears by itself when the credential file
changes (the owner signed in again): the next turn, or the daemon's idle loop, picks up the
new token without a restart. After a backoff (10 minutes, doubling to 6 hours) one probe is
allowed, in case the rejection was temporary.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

__all__ = ["CredentialState", "classify", "guidance"]

STATE_FILE = "credential.json"
KINDS = ("invalid", "no_access")
BACKOFF_SECONDS = (600.0, 21600.0)
_NO_ACCESS = ("no copilot access", "no_copilot_access")
_INVALID = ("credential was rejected", "copilot auth failed", "invalid_credentials",
            "not authenticated", "bad credentials", "reports unauthenticated")
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
SIGN_IN = ("Sign in again in RAPP Brainstem (run `brainstem`, open http://localhost:7071 and "
           "sign in with GitHub); Brainstem Agent notices the new sign-in by itself, no restart "
           "needed.")


def classify(text: str) -> str | None:
    """``invalid`` or ``no_access`` when a Grail or worker error says the credential itself
    is the problem, else None (network trouble and other failures are not the credential's)."""
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _NO_ACCESS):
        return "no_access"
    if any(marker in lowered for marker in _INVALID):
        return "invalid"
    return None


def _local(moment: float | None) -> str:
    if not moment:
        return "unknown"
    return dt.datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def guidance(record: dict) -> str:
    """The refusal text and fix for a recorded credential problem."""
    if record.get("kind") == "no_access":
        return (f"The signed-in GitHub account has no Copilot access (seen {_local(record.get('since'))}"
                "). Nothing was run. Enable Copilot for that account, or sign in to RAPP Brainstem "
                "with an account that has it; Brainstem Agent checks again after "
                f"{_local(record.get('retry_after'))} or as soon as the sign-in changes.")
    return (f"The Copilot sign-in was rejected by GitHub (revoked, expired or replaced; seen "
            f"{_local(record.get('since'))}). Nothing was run. {SIGN_IN}")


class CredentialState:
    """The recorded credential problem of one home (thread- and process-safe enough: every
    change is an atomic replace of a small file)."""

    def __init__(self, home: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / "state" / STATE_FILE
        self.clock = clock

    @staticmethod
    def identity(credential) -> list | None:
        try:
            info = os.stat(credential.path)
        except (OSError, TypeError, ValueError):
            return None
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def read(self) -> dict | None:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            try:
                record = json.loads(handle.read(65536))
            except ValueError:
                return None
        return record if isinstance(record, dict) and record.get("kind") in KINDS else None

    def _write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(temporary, self.path)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def check(self, credential) -> dict[str, Any]:
        """The verdict for a new turn with ``credential``: ``state`` (``ok``, ``invalid`` or
        ``no_access``), ``refuse`` and its ``message``; ``changed`` when a recorded problem
        was cleared because the credential file changed; ``probe`` when the backoff ended and
        one attempt may go ahead."""
        record = self.read()
        if record is None:
            return {"state": "ok", "refuse": False}
        identity = self.identity(credential)
        if identity != record.get("identity") or credential.source != record.get("source"):
            # A new sign-in: the next attempt goes ahead at once and a failure starts a fresh
            # backoff; success clears the record (``record_success``).
            record.update(identity=identity, source=credential.source, attempts=0,
                          retry_after=0, changed_at=self.clock())
            self._write(record)
            return {"state": "ok", "refuse": False, "changed": True, "was": record["kind"]}
        verdict = {"state": record["kind"], "reason": record.get("reason"),
                   "since": record.get("since"), "retry_after": record.get("retry_after"),
                   "attempts": record.get("attempts"), "fix": guidance(record),
                   "changed_since_rejection": float(record.get("changed_at") or 0) >
                   float(record.get("last_failure") or 0)}
        if self.clock() >= float(record.get("retry_after") or 0):
            return {**verdict, "refuse": False, "probe": True}
        return {**verdict, "refuse": True, "message": guidance(record)}

    def record_failure(self, credential, kind: str, reason: str) -> dict:
        """Record (or extend) a credential problem; the backoff doubles for the same file."""
        now, previous = self.clock(), self.read()
        identity = self.identity(credential)
        same = previous is not None and previous.get("identity") == identity and \
            previous.get("kind") == kind
        attempts = int(previous.get("attempts") or 0) + 1 if same else 1
        backoff = min(BACKOFF_SECONDS[1], BACKOFF_SECONDS[0] * 2 ** (attempts - 1))
        record = {"kind": kind, "reason": _TOKEN_SHAPES.sub(r"\1[REDACTED]", str(reason))[:300],
                  "since": previous["since"] if same and previous.get("attempts") else now,
                  "last_failure": now, "attempts": attempts, "retry_after": now + backoff,
                  "source": getattr(credential, "source", None), "identity": identity}
        self._write(record)
        return record

    def record_success(self) -> bool:
        """A turn or warm start succeeded with the current credential: clear any record."""
        return self.clear()

    def describe(self) -> dict[str, Any]:
        record = self.read()
        if record is None:
            return {"state": "ok"}
        return {"state": record["kind"], "reason": record.get("reason"),
                "since": record.get("since"), "retry_after": record.get("retry_after"),
                "attempts": record.get("attempts"), "source": record.get("source"),
                "fix": guidance(record)}
