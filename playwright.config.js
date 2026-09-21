import { accessSync, constants } from 'node:fs';
import { defineConfig } from '@playwright/test';

function isExecutable(path) {
  try {
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// Local development can reuse an installed browser; CI always uses bundled Chromium.
const executablePath = process.env.CI ? undefined : (
  process.env.PLAYWRIGHT_EXECUTABLE_PATH || [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  ].find(isExecutable)
);

if (executablePath && !isExecutable(executablePath)) {
  throw new Error('PLAYWRIGHT_EXECUTABLE_PATH must identify an executable browser.');
}

const port = Number(process.env.PORT || 4173);
const baseURL = `http://127.0.0.1:${port}/brainstem-agent/`;

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.js',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 2,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL,
    browserName: 'chromium',
    viewport: { width: 1440, height: 1000 },
    launchOptions: executablePath ? { executablePath } : {},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'node tests/server.mjs',
    url: baseURL,
    env: { PORT: String(port) },
    reuseExistingServer: false,
    timeout: 15_000,
  },
});
