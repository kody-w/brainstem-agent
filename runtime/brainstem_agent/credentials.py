"""Read-only discovery of the brainstem's existing GitHub Copilot connection, and the
shapes of credentials in general.

The value is never printed, logged, persisted or returned by ``repr``/``str``;
callers only ever see a source label and a token kind. ``credential_kinds`` names
credential-shaped text (GitHub tokens, bearer tokens, private-key blocks, cloud and API
keys, JWTs, URL passwords, ``password = ...`` assignments) so the cell can refuse to keep
it as knowledge, and ``redact_credentials`` removes it from what the cell records.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

__all__ = ["CredentialUnavailable", "GitHubCredential", "credential_kinds",
           "installed_credential_path", "redact_credentials", "resolve_github_credential"]

_MAX_BYTES = 4096
_TOKEN = re.compile(r"[A-Za-z0-9_.\-]{20,512}")
_KINDS = (("github_pat_", "github_pat"), ("ghu_", "ghu"), ("gho_", "gho"), ("ghp_", "ghp"))
# Text shaped like a credential (never stored as knowledge, redacted from receipts). A kind
# names the shape only; the matched text is never repeated anywhere.
_SECRET_NAMES = (r"password|passwd|passphrase|secret|client[_ -]?secret|api[_ -]?key|"
                 r"access[_ -]?key|private[_ -]?key|access[_ -]?token|auth[_ -]?token|"
                 r"refresh[_ -]?token|token")
_SHAPES = tuple((kind, re.compile(pattern, flags)) for kind, pattern, flags in (
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", 0),
    ("private-key", r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
                    r"(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.DOTALL),
    ("bearer-token", r"\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.IGNORECASE),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 0),
    ("api-key", r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|[sr]k_live_[0-9A-Za-z]{16,}"
                r"|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,})", 0),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0),
    ("url-credentials", r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@", re.IGNORECASE),
    # NAME = value / NAME is value, with a value that has both letters and digits and is no
    # placeholder ($VAR, <value>, {value}, ****) or code (a call's name stops at "(").
    ("secret-assignment", rf"(?<![A-Za-z0-9])(?:{_SECRET_NAMES})(?![A-Za-z0-9])[\"']?\s*"
                          r"(?:[:=]|\bis\b)\s*[\"']?(?=[^\s\"'`$<>{}()%*,;]*\d)"
                          r"(?=[^\s\"'`$<>{}()%*,;]*[A-Za-z])[^\s\"'`$<>{}()%*,;]{8,}",
     re.IGNORECASE),
))


def credential_kinds(text: str) -> list[str]:
    """The kinds of credential-shaped text in ``text`` (empty when there is none)."""
    if not isinstance(text, str) or not text:
        return []
    return [kind for kind, pattern in _SHAPES if pattern.search(text)]


def redact_credentials(value):
    """``value`` (text, or JSON-like lists and mappings of it) with every credential-shaped
    span replaced by ``[REDACTED:<kind>]``."""
    if isinstance(value, str):
        for kind, pattern in _SHAPES:
            value = pattern.sub(f"[REDACTED:{kind}]", value)
        return value
    if isinstance(value, list):
        return [redact_credentials(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_credentials(item) for key, item in value.items()}
    return value


class CredentialUnavailable(RuntimeError):
    """No usable Copilot credential; the message never contains a credential value."""


@dataclass(frozen=True)
class GitHubCredential:
    value: str = field(repr=False)
    source: str
    kind: str
    path: str = field(default="", repr=False)

    def __str__(self) -> str:
        return f"GitHubCredential(source={self.source!r}, kind={self.kind!r})"

    def describe(self) -> dict:
        return {"source": self.source, "kind": self.kind}


def installed_credential_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    home = environ.get("BRAINSTEM_HOME") or os.path.join(
        environ.get("HOME") or os.path.expanduser("~"), ".brainstem")
    return Path(home) / "src" / "rapp_brainstem" / ".copilot_token"


def _read(path: Path, source: str) -> GitHubCredential:
    label = "the explicit token file" if source == "file" else "the installed brainstem credential"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise CredentialUnavailable(f"No Copilot credential: {label} ({path}) does not exist.") from None
    except OSError:
        raise CredentialUnavailable(
            f"No Copilot credential: {label} ({path}) cannot be opened safely "
            "(symlinks are refused).") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialUnavailable(f"No Copilot credential: {label} is not a regular file.")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CredentialUnavailable(
                f"No Copilot credential: {label} must be owned by you and not group/world-readable.")
        if not 0 < info.st_size <= _MAX_BYTES:
            raise CredentialUnavailable(f"No Copilot credential: {label} is empty or too large.")
        raw = os.read(descriptor, _MAX_BYTES + 1).decode("utf-8", "replace").strip()
    finally:
        os.close(descriptor)
    value = raw
    if raw.startswith("{"):
        try:
            document = json.loads(raw)
        except ValueError:
            raise CredentialUnavailable(f"No Copilot credential: {label} is not valid JSON.") from None
        value = document.get("access_token") if isinstance(document, dict) else None
        if not isinstance(value, str):
            raise CredentialUnavailable(f"No Copilot credential: {label} has no access_token.")
    if not _TOKEN.fullmatch(value or ""):
        raise CredentialUnavailable(f"No Copilot credential: {label} does not hold a token.")
    kind = next((name for prefix, name in _KINDS if value.startswith(prefix)), "other")
    return GitHubCredential(value=value, source=source, kind=kind, path=str(path))


def resolve_github_credential(*, environ: Mapping[str, str] | None = None) -> GitHubCredential:
    """Explicit BRAINSTEM_AGENT_GITHUB_TOKEN_FILE first (no fallback), else the installed brainstem."""
    environ = os.environ if environ is None else environ
    explicit = environ.get("BRAINSTEM_AGENT_GITHUB_TOKEN_FILE")
    if explicit:
        return _read(Path(explicit), "file")
    return _read(installed_credential_path(environ), "brainstem")
