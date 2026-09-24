import { existsSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { defineConfig } from '@playwright/test';

// Reuse the headless shell already in Playwright's cache (no download). Only Playwright's
// own browser builds are considered, never an installed browser or a user profile.
function cachedHeadlessShell() {
  if (process.env.BRAINSTEM_AGENT_CHROMIUM) return process.env.BRAINSTEM_AGENT_CHROMIUM;
  const cache = join(homedir(), 'Library', 'Caches', 'ms-playwright');
  if (!existsSync(cache)) return undefined;
  const builds = readdirSync(cache).filter((name) => name.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const build of builds) {
    const binary = join(cache, build, 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell');
    if (existsSync(binary)) return binary;
  }
  return undefined;
}
const executablePath = cachedHeadlessShell();

// The companion's browser specs (runtime/tests/browser). Separate from the public site's
// playwright.config.js. Always Playwright's own headless Chromium (never an installed
// browser or a profile); foreign test names resolve to loopback so DNS rebinding and
// cross-site pages can be exercised without the network.
export default defineConfig({
  testDir: './runtime/tests/browser',
  testMatch: '**/*.spec.js',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  reporter: process.env.CI ? 'github' : 'list',
  outputDir: '.cache/browser-results',
  use: {
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1280, height: 900 },
    launchOptions: {
      ...(executablePath ? { executablePath } : {}),
      args: ['--host-resolver-rules=MAP evil.test 127.0.0.1, MAP rebind.test 127.0.0.1'],
    },
    trace: 'off',
    screenshot: 'off',
  },
});
