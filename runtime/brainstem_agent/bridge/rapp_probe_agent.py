"""Brainstem Agent membrane probe, installed only in `doctor --deep` workers.

It defines no tools. When a request carries the probe header matching this
worker's nonce, it tries forbidden reads, writes and connections from inside the
Grail process and records only "allowed"/"denied"/"absent" in $HOME/probe.json.
"""

import json
import os
import socket


def _requested():
    try:
        from flask import has_request_context, request
        nonce = os.environ.get("BRAINSTEM_AGENT_PROBE_NONCE", "")
        return bool(nonce) and has_request_context() and \
            request.headers.get("X-Brainstem-Agent-Probe") == nonce
    except Exception:
        return False


def _read(path):
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            with open(path, "rb") as handle:
                handle.read(1)
        return "allowed"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "denied"


def _write_new(path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return "denied"
    os.close(descriptor)
    return "allowed"


def _write_existing(path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError:
        return "denied"
    os.close(descriptor)
    return "allowed"


def _connect(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
            return "allowed"
    except OSError:
        return "denied"


def _probe():
    targets = json.loads(os.environ.get("BRAINSTEM_AGENT_PROBE", "{}"))
    tree = os.path.dirname(os.environ.get("AGENTS_PATH", ""))
    broker_port = os.environ.get("BRAINSTEM_AGENT_BROKER_URL", "").rsplit(":", 1)[-1]
    results = {name: _read(path) for name, path in targets.get("read", {}).items()}
    results.update({name: _write_new(path) for name, path in targets.get("write_new", {}).items()})
    results["write_agents_dir"] = _write_new(os.path.join(tree, "agents", "probe_escape_agent.py"))
    results["write_tracked_grail_file"] = _write_existing(
        os.path.join(tree, "rapp_brainstem", "brainstem.py"))
    results["connect_other_loopback"] = _connect(targets.get("other_port", 9))
    results["connect_broker"] = _connect(broker_port)
    with open(os.path.join(os.environ["HOME"], "probe.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle)


if _requested():
    try:
        _probe()
    except Exception as error:  # report failure without details that could hold paths
        with open(os.path.join(os.environ.get("HOME", "."), "probe.json"), "w") as handle:
            json.dump({"probe_error": type(error).__name__}, handle)
