"""Runs the real interactive terminal (``brainstem_agent.repl.Repl``) in-process with the
scripted fake Grail of ``companion_support`` (no daemon), on this process's real stdin,
stdout and signals: ``python repl_support.py [--json]``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_support import factory  # noqa: E402
from brainstem_agent.repl import Repl  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(Repl(dict(os.environ), json_mode="--json" in sys.argv,
                          workspace=os.environ.get("BRAINSTEM_AGENT_WORKSPACE"),
                          worker_factory=factory).run())
