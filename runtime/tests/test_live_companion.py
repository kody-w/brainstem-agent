"""Live companion acceptance (G8, G9): runs the live browser spec (runtime/tests/browser/
live.spec.js) against a real daemon with unchanged Grail and real Copilot inference, and
records its transcript summaries. Gated by BRAINSTEM_AGENT_LIVE=1 (about 10 Grail requests).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from acceptance_support import (LIVE, REPO, criteria, leaks, prepared_cache, private_dir,
                                real_credential_needles, record_metric)


@unittest.skipUnless(LIVE, "live: set BRAINSTEM_AGENT_LIVE=1 (spends real Copilot turns)")
class LiveCompanion(unittest.TestCase):
    @criteria("G8", "G9")
    def test_g8_g9_companion_and_terminal_with_real_copilot(self):
        npx = os.environ.get("BRAINSTEM_AGENT_NPX") or shutil.which("npx") or "npx"
        node = Path(npx).parent / "node"  # npx runs under `env node`: keep node on PATH
        if not (Path(npx).exists() and node.exists()
                and (REPO / "node_modules" / "@playwright" / "test").exists()):
            self.skipTest("Node and Playwright (dev only) are not installed here")
        scratch = private_dir(self)
        evidence = scratch / "browser-live.json"
        env = {**os.environ, "BRAINSTEM_AGENT_LIVE": "1",
               "BRAINSTEM_AGENT_TEST_CACHE": str(prepared_cache()),
               "BRAINSTEM_AGENT_BROWSER_EVIDENCE": str(evidence),
               "BRAINSTEM_AGENT_TEST_PYTHON": __import__("sys").executable,
               "PATH": f"{node.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
        result = subprocess.run([npx, "playwright", "test", "-c", "playwright.companion.config.js",
                                 "live.spec.js", "--reporter=line"], cwd=REPO, env=env,
                                capture_output=True, text=True, timeout=1800)
        tail = (result.stdout + result.stderr)[-3000:]
        self.assertEqual(result.returncode, 0, tail)
        document = json.loads(evidence.read_text())
        self.assertEqual(leaks(real_credential_needles(), texts=[evidence.read_text(), tail]), [])
        for key in ("g9_create", "g9_cancel", "g9_skill", "g8_parity", "g8_schedule",
                    "live_grail_requests", "daemon_stop"):
            self.assertIn(key, document)
        self.assertTrue(document["daemon_stop"]["ok"])
        record_metric("g9_transcript", {key: document[key] for key in ("g9_create", "g9_cancel",
                                                                          "g9_skill")})
        record_metric("g8_transcript", {key: document[key] for key in ("g8_parity",
                                                                          "g8_schedule")})
        record_metric("g_live_grail_requests", document["live_grail_requests"])


if __name__ == "__main__":
    unittest.main()
