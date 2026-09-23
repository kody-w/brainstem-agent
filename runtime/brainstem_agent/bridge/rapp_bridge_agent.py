"""Brainstem Agent bridge: the cell's only code inside a captured Grail worker.

Grail executes this file fresh for every request (load_agents inside Flask's
request context). When the request carries the cell's grant header, this module
binds the grant with the cell's broker and defines one tool class per granted
cell tool; perform() forwards only the model's arguments to the broker and
returns its text. Without a request context or grant it defines nothing. The
bridge holds no authority and never raises from perform().
"""

import json
import os
import urllib.error
import urllib.request
import uuid

try:
    from agents.basic_agent import BasicAgent
except ImportError:  # flat layout fallback supported by Grail
    from basic_agent import BasicAgent

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_UNAVAILABLE = "Brainstem Agent tools are unavailable for this request"


def _grant_header():
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return None
        value = request.headers.get("X-Brainstem-Agent-Grant")
    except Exception:
        return None
    if isinstance(value, str) and 0 < len(value) <= 512:
        return value
    return None


def _post(path, body, timeout):
    environ = os.environ
    request = urllib.request.Request(
        environ.get("BRAINSTEM_AGENT_BROKER_URL", "") + path,
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Brainstem-Agent-Worker": environ.get("BRAINSTEM_AGENT_WORKER_ID", ""),
            "X-Brainstem-Agent-Generation": environ.get("BRAINSTEM_AGENT_WORKER_GENERATION", ""),
            "Authorization": "Bearer " + environ.get("BRAINSTEM_AGENT_WORKER_KEY", ""),
        })
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(1 << 22) or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read(1 << 16) or b"{}")
        except ValueError:
            return error.code, {}


def _tool_class(spec, bind_id, grant, context):
    name = str(spec.get("name", ""))
    metadata = {"name": name, "description": str(spec.get("description", "")),
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}}}
    timeout = float(spec.get("timeout_seconds", 30)) + 15

    def __init__(self):
        BasicAgent.__init__(self, name=name, metadata=metadata)

    def perform(*_bound, **arguments):
        try:
            status, body = _post("/v1/invoke", {
                "grant": grant, "bind_id": bind_id, "call_id": "call_" + uuid.uuid4().hex,
                "tool": name, "arguments": arguments}, timeout)
        except Exception:
            return "Brainstem Agent tool call failed: the cell did not answer."
        if status == 200 and isinstance(body, dict) and isinstance(body.get("content"), str):
            return body["content"]
        detail = body.get("error") if isinstance(body, dict) else None
        return "Brainstem Agent tool call failed: " + str(detail or f"HTTP {status}")[:500]

    def system_context(self):
        return context

    return type("BrainstemTool_" + name, (BasicAgent,), {
        "__module__": __name__, "__doc__": metadata["description"], "__init__": __init__,
        "perform": perform, "system_context": system_context})


def _status_class(reason):
    # A required argument: Grail refuses the empty arguments models send otherwise.
    metadata = {"name": "brainstem_agent_status",
                "description": "Explains why Brainstem Agent tools are unavailable.",
                "parameters": {"type": "object", "properties": {"intent": {
                    "type": "string", "description": "What you wanted to do with the tools."}},
                    "required": ["intent"]}}

    def __init__(self):
        BasicAgent.__init__(self, name="brainstem_agent_status", metadata=metadata)

    def perform(*_bound, **_arguments):
        return f"{_UNAVAILABLE}: {reason}"

    def system_context(self):
        return (f"<brainstem_agent>{_UNAVAILABLE}: {reason}. Do not claim to have used any "
                "Brainstem Agent tool.</brainstem_agent>")

    return type("BrainstemAgentStatus", (BasicAgent,), {
        "__module__": __name__, "__init__": __init__, "perform": perform,
        "system_context": system_context})


_GRANT = _grant_header()
if _GRANT is not None:
    try:
        _STATUS, _BOUND = _post("/v1/bind", {"grant": _GRANT}, 20)
    except Exception:
        _STATUS, _BOUND = 0, {}
    _TOOLS = _BOUND.get("tools") if isinstance(_BOUND, dict) else None
    if _STATUS == 200 and isinstance(_TOOLS, list) and _TOOLS:
        _CONTEXT = _BOUND.get("context") if isinstance(_BOUND.get("context"), str) else None
        for _INDEX, _SPEC in enumerate(_TOOLS):
            _CLASS = _tool_class(_SPEC, str(_BOUND.get("bind_id", "")), _GRANT,
                                 _CONTEXT if _INDEX == 0 else None)
            globals()[_CLASS.__name__] = _CLASS
    else:
        _REASON = ("the cell refused this worker's grant" if _STATUS in (401, 403, 404)
                   else "the cell could not be reached")
        BrainstemAgentStatus = _status_class(_REASON)
