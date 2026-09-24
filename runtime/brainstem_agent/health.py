"""One health model: liveness versus readiness, every check with a reason and a fix.

* **Live** means the process is up and its loops run: for the daemon, its control server
  answers and its schedule loop is alive. A cell that is not live needs a (re)start.
* **Ready** means a turn can run now: the pinned Grail source verifies, the worker
  interpreter is prepared, the Copilot credential is usable (present and not known to be
  rejected), the sandbox works, the store opens with a schema this version reads, disk space
  is above the floor, the warm worker is up (daemon) and the MCP servers are healthy.

``doctor`` reports the installation's checks, ``status`` and the daemon's ``GET /v1/health``
report the daemon's (liveness and readiness), and lifecycle commands (setup, restore,
upgrade, rollback, compaction) end with the same summary.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import grail, hygiene, sandbox, state
from .credential_state import CredentialState, SIGN_IN
from .credentials import CredentialUnavailable, resolve_github_credential

__all__ = ["Check", "installation_checks", "render", "summarize"]

SETUP_FIX = "Run: brainstem-agent setup"
CREDENTIAL_FIX = ("Install RAPP Brainstem and sign in with GitHub (run `brainstem`, open "
                  "http://localhost:7071), or point BRAINSTEM_AGENT_GITHUB_TOKEN_FILE at a token "
                  "file.")


@dataclass
class Check:
    id: str
    ok: bool
    reason: str
    fix: str | None = None
    kind: str = "readiness"
    required: bool = True
    detail: dict = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "ok": self.ok, "required": self.required,
                "reason": self.reason, "fix": None if self.ok else self.fix, **self.detail}

    def legacy(self) -> dict[str, Any]:
        """The ``doctor --json`` shape (``checks[<id>]``): the detail fields, ``ok`` and
        ``detail`` as before, plus ``reason``, ``fix`` and ``required``."""
        return {"ok": self.ok, **self.detail, "detail": self.reason, "reason": self.reason,
                "fix": None if self.ok else self.fix, "required": self.required}


def summarize(checks: Iterable[Check]) -> dict[str, Any]:
    checks = list(checks)
    live = all(check.ok for check in checks if check.kind == "liveness")
    ready = live and all(check.ok for check in checks if check.kind == "readiness"
                         and check.required)
    return {"live": live, "ready": ready,
            "state": "ready" if ready else "live-not-ready" if live else "down",
            "failing": [check.id for check in checks if not check.ok and check.required],
            "advisories": [check.id for check in checks if not check.ok and not check.required],
            "checks": [check.to_json() for check in checks]}


def render(report: Mapping[str, Any], subject: str = "Brainstem Agent") -> str:
    """Human lines: the verdict, then each failing check with its reason and fix."""
    verdict = ("ready" if report["ready"] else
               "live but not ready" if report["live"] else "not live")
    lines = [f"{subject}: {verdict}."]
    for check in report["checks"]:
        if not check["ok"]:
            label = "" if check["required"] else " (advisory)"
            lines.append(f"  - {check['id']}{label}: {check['reason']}")
            if check.get("fix"):
                lines.append(f"    fix: {check['fix']}")
    return "\n".join(lines)


# -- individual checks ------------------------------------------------------------------------
def grail_source_check(cache: Path) -> Check:
    try:
        source = grail.ensure_grail_source(cache, fetch=False)
    except grail.GrailSourceError as error:
        text = str(error)
        fix = SETUP_FIX if "not prepared" in text else (
            f"{SETUP_FIX} (if files were changed, remove {Path(cache) / 'grail'} first; setup "
            "downloads and verifies the pinned source again)")
        return Check("grail_source", False, text, fix, detail={
            "commit": grail.PINNED_COMMIT, "kernel_sha256": grail.KERNEL_SHA256, "files": 0})
    return Check("grail_source", True, f"all {len(source.inventory)} tracked files verified",
                 detail={"commit": source.commit, "kernel_sha256": grail.KERNEL_SHA256,
                         "version": grail.VERSION, "files": len(source.inventory)})


def worker_interpreter_check(cache: Path, environ: Mapping[str, str], *,
                             import_check: bool = True) -> Check:
    if import_check:
        result = grail.check_worker_venv(cache, environ=environ)
        detail = {key: result[key] for key in ("python", "version", "hash_locked")}
        return Check("worker_interpreter", result["ok"], result["detail"], SETUP_FIX,
                     detail=detail)
    try:
        python = grail.worker_venv_python(cache, environ=environ)
    except grail.GrailSourceError as error:
        return Check("worker_interpreter", False, str(error), SETUP_FIX,
                     detail={"python": None, "hash_locked": False})
    return Check("worker_interpreter", True, "private venv installed with --require-hashes",
                 detail={"python": str(python), "hash_locked": True})


def credential_check(home: Path, environ: Mapping[str, str]) -> Check:
    """Present and readable, and not known to be rejected (the credential state)."""
    try:
        credential = resolve_github_credential(environ=environ)
    except CredentialUnavailable as error:
        return Check("credential", False, str(error), CREDENTIAL_FIX,
                     detail={"source": None, "kind": None, "state": "missing"})
    verdict = CredentialState(home).check(credential)
    detail = {**credential.describe(), "state": verdict["state"]}
    if verdict["state"] == "ok":
        return Check("credential", True, "found (the value is never shown)", detail=detail)
    detail.update(since=verdict.get("since"), retry_after=verdict.get("retry_after"))
    if verdict.get("probe"):
        reason = ("the credential changed since it was rejected; the next turn tries it"
                  if verdict.get("changed_since_rejection") else
                  f"previously rejected ({verdict['state']}); the backoff ended, so the next turn "
                  "tries it once")
        return Check("credential", True, reason, detail=detail)
    reason = ("GitHub rejected the Copilot sign-in (revoked, expired or replaced); turns are "
              "refused until it changes" if verdict["state"] == "invalid" else
              "the signed-in GitHub account has no Copilot access; turns are refused")
    return Check("credential", False, reason, verdict.get("fix") or SIGN_IN, detail=detail)


_SANDBOX_CACHE: dict[str, tuple[float, bool]] = {}


def sandbox_check(environ: Mapping[str, str], *, max_age: float = 0.0) -> Check:
    path = sandbox.sandbox_exec_path(environ)
    cached = _SANDBOX_CACHE.get(path)
    if cached and time.monotonic() - cached[0] < max_age:
        works = cached[1]
    else:
        works = sandbox.available(environ)
        if works:
            try:
                works = subprocess.run([path, "-p", "(version 1)\n(allow default)\n",
                                        "/usr/bin/true"], capture_output=True,
                                       timeout=30).returncode == 0
            except (OSError, subprocess.SubprocessError):
                works = False
        _SANDBOX_CACHE[path] = (time.monotonic(), works)
    return Check("sandbox", works, "Seatbelt sandbox-exec works" if works else
                 f"{path} is unavailable",
                 "Brainstem Agent needs macOS's /usr/bin/sandbox-exec; it never runs work "
                 "unsandboxed", detail={"environment": sandbox.ENVIRONMENT})


def disk_check(home: Path, environ: Mapping[str, str]) -> Check:
    status = hygiene.disk_status(home, environ)
    if status["ok"]:
        return Check("disk_space", True, f"{status['free_mb']:.0f} MiB free (floor "
                     f"{status['min_free_mb']:.0f} MiB)", detail=status)
    reason = (f"only {status.get('free_mb', 0):.0f} MiB free on the disk holding the home "
              f"(floor {status['min_free_mb']:.0f} MiB); new turns are refused"
              if "error" not in status else f"free space unknown: {status['error']}")
    return Check("disk_space", False, reason,
                 "Free disk space, or run `brainstem-agent prune` then `brainstem-agent compact` "
                 "(the floor is disk.min_free_mb in operations.json)", detail=status)


def store_check(home: Path) -> Check:
    found = state.inspect_database(Path(home) / "state" / "agent.sqlite3")
    if not found["exists"]:
        return Check("store", True, "not created yet (made on first use)",
                     detail={"compatibility": None})
    detail = {key: found.get(key) for key in ("schema_version", "compatibility", "bytes")}
    kind = found.get("compatibility")
    if kind == "current":
        return Check("store", True, f"schema {found['schema_version']} (current)", detail=detail)
    if kind == "migrates":
        return Check("store", True, f"schema {found['schema_version']} (older; migrated on next "
                     "open after an automatic pre-migration backup)", detail=detail)
    if kind == "newer":
        return Check("store", False, "the store was written by a newer Brainstem Agent",
                     "Upgrade Brainstem Agent (brainstem-agent upgrade --from <release>), or "
                     "restore a backup made by this version", detail=detail)
    reason = found.get("error") or f"the store is not one this version recognizes ({kind})"
    return Check("store", False, reason, "Restore the latest backup: brainstem-agent restore "
                 "<backup directory> --replace", detail=detail)


def mcp_check(home: Path, statuses: list[dict] | None = None, *, required: bool) -> Check:
    from .organs.mcp import server_specs
    from .organs.web import read_config

    config, error = read_config(Path(home) / "reach.json")
    servers, problems = server_specs(config)
    problems = ([error] if error else []) + list(problems)
    unhealthy = [f"{item['server']}: {item['state']}" + (f" ({item['error']})"
                                                         if item.get("error") else "")
                 for item in statuses or [] if item.get("server") and
                 item.get("state") not in ("ready", "stopped", "not started")]
    problems += unhealthy
    detail = {"servers": sorted(servers), "states": {item["server"]: item.get("state")
                                                     for item in statuses or []
                                                     if item.get("server")}}
    if problems:
        return Check("mcp", False, "; ".join(problems)[:400],
                     "Check reach.json and the server (brainstem-agent mcp status <server>)",
                     required=required, detail=detail)
    return Check("mcp", True, f"{len(servers)} configured server(s) healthy" if servers else
                 "no MCP servers configured", required=required, detail=detail)


def policy_check(home: Path, environ: Mapping[str, str]) -> Check:
    _policy, problems = hygiene.load_policy(home, environ)
    return Check("operations_policy", not problems, "; ".join(problems)[:300] or
                 "operations.json absent or valid (defaults apply)",
                 "Correct operations.json (bad values keep their defaults)", required=False)


def installation_checks(home: Path, cache: Path, environ: Mapping[str, str], *,
                        import_check: bool = True) -> list[Check]:
    """What ``doctor`` checks: can a turn run on this installation?"""
    return [grail_source_check(cache),
            worker_interpreter_check(cache, environ, import_check=import_check),
            credential_check(home, environ), sandbox_check(environ), disk_check(home, environ),
            store_check(home), mcp_check(home, required=False), policy_check(home, environ)]


def not_running_checks(home: Path, cache: Path, environ: Mapping[str, str]) -> list[Check]:
    """``status`` without a daemon: not live, and what readiness would still lack."""
    return [Check("daemon", False, "the daemon is not running",
                  "Run: brainstem-agent serve --detach (or brainstem-agent service install)",
                  kind="liveness"),
            *installation_checks(home, cache, environ, import_check=False)]
