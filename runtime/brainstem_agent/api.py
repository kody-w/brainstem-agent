"""The daemon's documented API: every route, who may call it, and its CLI equivalent.

One table serves the CLI (bearer token), agents and the companion (session + CSRF).
``companion`` marks the routes a browser session may call (least privilege: never direct
tool calls, the RAPP/1 endpoint, stop/wake, or minting sign-in links). ``GET /v1/api``
returns this table; ``brainstem-agent api METHOD PATH`` reaches any route from the CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ROUTES", "Route", "describe", "markdown_table", "match", "methods_for"]

_PARAM = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


@dataclass(frozen=True)
class Route:
    """One route. ``cli`` is a command line that reaches it (paste-safe: placeholders are
    ``<name>``, which a shell refuses rather than runs); ``note`` says what else does."""

    name: str
    method: str
    pattern: str
    companion: bool
    summary: str
    cli: str
    note: str = ""

    def parts(self) -> list[str]:
        return self.pattern.strip("/").split("/")


ROUTES = (
    Route("api", "GET", "/v1/api", True, "This route table.", "api GET /v1/api"),
    Route("status", "GET", "/v1/status", True,
          "Health, workers, active turn, schedules, MCP servers, last errors, companion.",
          "status --json"),
    Route("health", "GET", "/v1/health", True,
          "Liveness versus readiness: every check with its reason and fix.",
          "api GET /v1/health", "status --json carries the same report as readiness"),
    Route("version", "GET", "/v1/version", False,
          "The running version, Grail pin, store schema, digests and capabilities.",
          "api GET /v1/version", "version --json shows it next to the installed release"),
    Route("turn", "POST", "/v1/turn", False, "Run one turn and wait for its full result.",
          "chat <message> --json"),
    Route("chat", "POST", "/chat", False,
          "RAPP/1: exactly response, agent_logs, session_id.", "api POST /chat --body <json>"),
    Route("tool", "POST", "/v1/tool", False, "Invoke one cell tool directly as the owner.",
          "tool <name> --arguments <json>"),
    Route("cancel", "POST", "/v1/cancel", True,
          "Cancel a request ({request_id}) or the active turn ({active: true}).",
          "cancel", "the active turn; Ctrl-C cancels a running chat or terminal turn"),
    Route("progress", "POST", "/v1/progress", False, "Progress events of a /v1/turn request.",
          "chat <message>", "progress lines on stderr"),
    Route("wake", "POST", "/v1/wake", False, "Wake the schedule loop.", "api POST /v1/wake",
          "every schedules command that changes a schedule wakes it too"),
    Route("drain", "POST", "/v1/drain", False,
          "Finish the running work and start nothing new ({timeout}); {resume: true} undoes it.",
          "stop --drain", "upgrade and rollback drain the daemon first"),
    Route("stop", "POST", "/v1/stop", False, "Stop the daemon and its workers.", "stop"),
    Route("requests.start", "POST", "/v1/requests", True,
          "Start a turn ({message, session_id?}); 202 with request_id, state queued.",
          "api POST /v1/requests --body <json>", "the terminal sends each message this way"),
    Route("requests.show", "GET", "/v1/requests/{request_id}", True,
          "A started turn's state and, once finished, its result.",
          "api GET /v1/requests/<request_id>"),
    Route("requests.events", "GET", "/v1/requests/{request_id}/events", True,
          "text/event-stream of a started turn (?after=SEQ): queued, running, progress, "
          "answer.delta, request.finished.", "api GET /v1/requests/<request_id>/events",
          "the terminal streams it; one JSON line per event"),
    Route("sessions.list", "GET", "/v1/sessions", True,
          "Sessions of the daemon's workspace, each with its last turn's label.",
          "sessions list --json"),
    Route("sessions.show", "GET", "/v1/sessions/{session_id}", True,
          "Every turn of a session with its state label, answer and receipts.",
          "sessions show <session_id> --json"),
    Route("journal.show", "GET", "/v1/journal/{turn_id}", True,
          "A turn's journal: segments, helpers, receipts.", "turns show <turn_id> --json"),
    Route("receipts.list", "GET", "/v1/receipts", True,
          "Receipts (?turn=TURN_ID: that turn and its helpers).", "receipts --json"),
    Route("schedules.list", "GET", "/v1/schedules", True, "Schedules of the workspace.",
          "schedules list --json"),
    Route("schedules.change", "POST", "/v1/schedules/{schedule_id}/{action}", True,
          "pause, resume or remove a schedule.", "schedules pause <schedule_id>",
          "also resume and remove"),
    Route("inbox.list", "GET", "/v1/inbox", True, "Results of scheduled runs, newest first.",
          "inbox --json"),
    Route("skills.list", "GET", "/v1/skills", True, "Skills of the workspace and profile.",
          "skills list --json"),
    Route("skills.show", "GET", "/v1/skills/{name}", True,
          "One skill (?version=N) with its steps and history.", "skills show <name> --json",
          "--version picks an older version"),
    Route("skills.review", "POST", "/v1/skills/{name}/{action}", True,
          "approve ({version}), reject, disable or enable a skill.", "skills approve <name>",
          "also reject, disable and enable; --version approves that version"),
    Route("memory.list", "GET", "/v1/memory", True,
          "Workspace and profile facts (?scope=all, workspace or profile).", "memory --json"),
    Route("memory.edit", "POST", "/v1/memory/edit", True,
          "Edit a fact ({scope, fact_id, text}).",
          "memory edit <fact_id> --scope workspace --text <text>", "--scope profile for a "
          "profile fact"),
    Route("memory.forget", "POST", "/v1/memory/forget", True,
          "Forget a fact ({scope, fact_id}).", "memory forget <fact_id> --scope workspace",
          "--scope profile for a profile fact"),
    Route("tools.list", "GET", "/v1/tools", True,
          "The cell's tools: capability, effect, whether a chat turn holds it.",
          "api GET /v1/tools"),
    Route("mcp.list", "GET", "/v1/mcp", True,
          "Configured MCP servers and their state.", "api GET /v1/mcp",
          "mcp list and mcp status show the same servers"),
    Route("egress.list", "GET", "/v1/egress", True,
          "The outbound request log (?limit=N, newest last).", "api GET /v1/egress",
          "egress log prints the same log"),
    Route("companion.login", "POST", "/v1/companion/login", False,
          "Mint a one-time companion sign-in link (120 s).", "open"),
    Route("companion.revoke", "POST", "/v1/companion/revoke", False,
          "End every companion session and pending link.", "open --sign-out-all"),
    Route("companion.session", "POST", "/v1/companion/session", False,
          "Exchange a one-time token for a session (the login page; no other credential).",
          "open", "prints the link whose token the login page exchanges"),
    Route("companion.whoami", "GET", "/v1/companion/session", True,
          "Whether this companion session is signed in.", "api GET /v1/companion/session"),
    Route("companion.logout", "POST", "/v1/companion/logout", True,
          "End this companion session.", "open --sign-out-all",
          "ends every companion session, this one included"),
)


def match(method: str, path: str) -> tuple[Route | None, dict, bool]:
    """(route, params, path_known): the route for ``method`` and ``path``; ``path_known``
    is True when some route has this path (so a wrong method is 405, not 404)."""
    parts = path.strip("/").split("/") if path.startswith("/") else None
    known = False
    if parts is None:
        return None, {}, False
    for route in ROUTES:
        pattern = route.parts()
        if len(pattern) != len(parts):
            continue
        params = {}
        for want, got in zip(pattern, parts):
            if want.startswith("{"):
                if not _PARAM.fullmatch(got):
                    break
                params[want[1:-1]] = got
            elif want != got:
                break
        else:
            known = True
            if route.method == method:
                return route, params, True
    return None, {}, known


def methods_for(path: str) -> list[str]:
    return sorted({route.method for route in ROUTES if match(route.method, path)[0]})


def describe() -> dict:
    return {"routes": [{"name": route.name, "method": route.method, "path": route.pattern,
                        "companion": route.companion, "summary": route.summary,
                        "cli": "brainstem-agent " + route.cli, "note": route.note}
                       for route in ROUTES]}


def markdown_table() -> str:
    """The route table as runtime/README.md lists it (a spec keeps the two identical)."""
    rows = ["| Route | Companion | What | CLI |", "|---|---|---|---|"]
    for route in ROUTES:
        cli = f"`brainstem-agent {route.cli}`" + (f" ({route.note})" if route.note else "")
        rows.append(f"| `{route.method} {route.pattern}` | {'yes' if route.companion else 'no'} "
                    f"| {route.summary} | {cli} |")
    return "\n".join(rows)
