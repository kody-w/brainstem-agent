"""The companion's browser specs (runtime/tests/browser: hostile.spec.js, companion.spec.js)
run from the unit tier, in Playwright's own headless Chromium, against real daemons with
scripted workers on this interpreter. Skips when the dev-only Playwright is not installed.
Records the axe report, page size, first-render time, streaming latency and the hostile
pages' attack outcomes as evidence (G2, G4-G7, G12)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from acceptance_support import REPO, criteria, private_dir, record_metric


class BrowserSpecs(unittest.TestCase):
    @criteria("G2", "G3", "G4", "G5", "G6", "G7", "G12")
    def test_g4_g6_hostile_pages_axe_layout_speed_in_headless_chromium(self):
        npx = os.environ.get("BRAINSTEM_AGENT_NPX") or shutil.which("npx") or "npx"
        node = Path(npx).parent / "node"  # npx runs under `env node`: keep node on PATH
        if not (Path(npx).exists() and node.exists()
                and (REPO / "node_modules" / "@axe-core" / "playwright").exists()):
            self.skipTest("Node, Playwright and axe (dev only) are not installed here")
        evidence = private_dir(self) / "browser.json"
        env = {**os.environ, "BRAINSTEM_AGENT_BROWSER_EVIDENCE": str(evidence),
               "BRAINSTEM_AGENT_TEST_PYTHON": sys.executable,
               "PATH": f"{node.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
        env.pop("BRAINSTEM_AGENT_LIVE", None)
        result = subprocess.run([npx, "playwright", "test", "-c", "playwright.companion.config.js",
                                 "hostile.spec.js", "companion.spec.js", "--reporter=line"],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=1200)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-3000:])
        document = json.loads(evidence.read_text())
        axe = document["g6_axe"]
        serious = [(view, item["id"]) for view, found in axe["views"].items()
                   for item in found["violations"] if item["impact"] in ("serious", "critical")]
        self.assertEqual(serious, [])
        record_metric("g6_axe_report", axe)
        for key in ("g7_first_render_ms", "g7_login_link_to_first_render_ms", "g7_page_bytes_loaded",
                    "g7_stream_latency_ms", "g5_live_state_sequence", "g2_stop_to_cancelled_ms",
                    "g4_attacks_same_site", "g4_attacks_cross_site"):
            self.assertIn(key, document)
            record_metric(key, document[key])


if __name__ == "__main__":
    unittest.main()
