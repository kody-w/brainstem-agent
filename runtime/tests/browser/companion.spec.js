// Companion browser specs (G2, G3, G5, G6, G7) against a real daemon with scripted
// workers; Playwright's own headless Chromium. Measurements land in the evidence file.
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { REPO, PYTHON, RUNTIME, startCell, restartCell, stopCell, login, ownerAPI } from './support.mjs';

const EVIDENCE = process.env.BRAINSTEM_AGENT_BROWSER_EVIDENCE || join(REPO, '.cache', 'evidence', 'browser-companion.json');
const evidence = {};
function record(key, value) {
  Object.assign(evidence, existsSync(EVIDENCE) ? JSON.parse(readFileSync(EVIDENCE, 'utf8')) : {});
  evidence[key] = value;
  mkdirSync(join(EVIDENCE, '..'), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(evidence, null, 2));
}

let cell;
test.beforeAll(async () => { cell = await startCell(); });
test.afterAll(async () => { await stopCell(cell); });

const turnState = (page, name) => page.locator('#transcript > li').last().locator(`:scope > .row .state-${name}`);
const liveState = (page, name) => page.locator('#transcript > li[data-live="true"]').locator(`:scope > .row .state-${name}`);
const nav = (page, name) => page.getByRole('navigation').getByRole('button', { name, exact: true }).click();
const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
};

async function send(page, text) {
  await page.getByLabel('Message').fill(text);
  await page.getByRole('button', { name: 'Send' }).click();
}

test('G2 chat: streamed answer, receipt, session list and resume, stop', async ({ page }) => {
  const requests = [];
  page.on('request', (request) => requests.push({ method: request.method(), url: request.url() }));
  await login(page, cell);
  const before = await page.evaluate(() => window.__bsa.states.length);
  await send(page, '[[say "one two three four five six seven eight nine ten"]]');
  await expect(page.locator('#transcript')).toContainText('one two three four five six seven eight nine ten');
  await expect(page.locator('#transcript .state-succeeded').first()).toHaveText('succeeded');
  // The page records every state it showed, so a short stream is never missed under load.
  const seen = await page.evaluate((from) => window.__bsa.states.slice(from).map((item) => item[1]), before);
  expect(seen).toContain('streaming');
  expect(seen[seen.length - 1]).toBe('succeeded');
  expect(await page.evaluate(() => window.__bsa.deltaLags.length)).toBeGreaterThan(0);
  await send(page, '[[write_file {"path": "notes/ui.txt", "content": "from the companion"}]] [[answer "Created notes/ui.txt."]]');
  await expect(page.locator('#transcript')).toContainText('Created notes/ui.txt.');
  await page.locator('#transcript > li').last().getByText(/^Receipts/).click();
  await expect(page.locator('#transcript > li').last()).toContainText('write_file');
  await expect(page.locator('#transcript > li').last().locator('details .state-succeeded').first()).toBeAttached();
  expect(readFileSync(join(cell.workspace, 'notes', 'ui.txt'), 'utf8')).toBe('from the companion');
  const session = (await page.locator('#session-line').textContent()).replace('Session ', '');
  // Stop a long-running turn; the session stays.
  await send(page, '[[slow 30]]');
  await expect(page.locator('#transcript > li[data-live="true"] .state-streaming')).toBeVisible();
  const stopped = Date.now();
  await page.getByRole('button', { name: 'Stop' }).click();
  await expect(turnState(page, 'cancelled')).toHaveText('cancelled');
  record('g2_stop_to_cancelled_ms', Date.now() - stopped);
  await expect(page.locator('#session-line')).toHaveText(`Session ${session}`);
  // Sessions: a new one, then resume the first.
  await page.getByRole('button', { name: 'New session' }).click();
  await send(page, '[[say "another session"]]');
  await expect(page.locator('#transcript')).toContainText('another session');
  await nav(page, 'Sessions');
  await expect(page.locator('#view-sessions .cards > li')).toHaveCount(2);
  await page.getByRole('button', { name: `Resume session ${session.replace(/^session_/, '').slice(0, 10)}` }).click();
  await expect(page.locator('#session-line')).toHaveText(`Session ${session}`);
  await expect(page.locator('#transcript > li')).toHaveCount(3);
  await send(page, '[[say "continued"]]');
  await expect(page.locator('#transcript > li')).toHaveCount(4);
  await expect(page.locator('#transcript > li[data-live="true"]')).toHaveCount(0);
  await expect(turnState(page, 'succeeded')).toBeVisible();
  const owner = await ownerAPI(cell, 'GET', `/v1/sessions/${session}`);
  expect(owner.body.turns.map((turn) => turn.label)).toEqual(['succeeded', 'succeeded', 'cancelled', 'succeeded']);
  // G3/G7: every request the page made was same-origin and a documented companion route.
  const routes = (await ownerAPI(cell, 'GET', '/v1/api')).body.routes;
  for (const { method, url } of requests) {
    expect(url.startsWith(cell.origin), url).toBe(true);
    const path = new URL(url).pathname;
    if (['/', '/login', '/ui/app.js', '/ui/login.js', '/ui/app.css', '/ui/icon.svg'].includes(path)) continue;
    const match = routes.find((route) => route.method === method && new RegExp(
      `^${route.path.replace(/\{[^}]+\}/g, '[^/]+')}$`).test(path));
    expect(match, `${method} ${path}`).toBeTruthy();
    // The login page's token exchange is the one public route; everything else is a
    // companion-session route.
    if (!(method === 'POST' && path === '/v1/companion/session')) {
      expect(match.companion, `${method} ${path}`).toBe(true);
    }
  }
  record('g3_browser_requests', requests.length);
});

test('G5 honest states: queued, running, streaming, failed, uncertain, partial, cancelled', async ({ page }) => {
  await login(page, cell);
  // The owner's own turn holds the one worker until the owner cancels it, so the
  // companion's turn is queued for as long as the check needs (no timing window).
  const blocker = await ownerAPI(cell, 'POST', '/v1/requests', { message: '[[slow 60]]' });
  await send(page, '[[say "after the owner"]]');
  const live = page.locator('#transcript > li[data-live="true"]');
  await expect(live.locator('.state-queued')).toHaveText('queued');
  await ownerAPI(cell, 'POST', '/v1/cancel', { request_id: blocker.body.request_id });
  await expect(turnState(page, 'succeeded')).toBeVisible({ timeout: 30_000 });
  const seen = await page.evaluate(() => window.__bsa.states.map((item) => item[1]));
  expect(seen.slice(-4)).toEqual(['queued', 'running', 'streaming', 'succeeded']);
  record('g5_live_state_sequence', seen.slice(-4));
  expect(blocker.status).toBe(202);
  await send(page, '[[say "streamed but"]] [[nodone]]');
  await expect(turnState(page, 'failed')).toHaveText('failed');
  await expect(page.locator('#transcript > li').last()).toContainText('Streamed text (not recorded as an answer)');
  await expect(turnState(page, 'succeeded')).toHaveCount(0);
  await send(page, '[[write_file {"path": "u.txt", "content": "x"}]] [[say "half"]] [[crash]]');
  await expect(turnState(page, 'uncertain')).toHaveText('uncertain');
  const session = (await page.locator('#session-line').textContent()).replace('Session ', '');
  // A limit ends a turn partial (three tool rounds with a one-step budget): owner API, same session.
  const partial = await ownerAPI(cell, 'POST', '/v1/requests', {
    session_id: session, budget: { max_segments: 1 },
    message: '[[list_files {"path": "."}]] [[list_files {"path": "."}]] [[list_files {"path": "."}]]',
  });
  for (let index = 0; index < 50; index += 1) {
    const state = (await ownerAPI(cell, 'GET', `/v1/requests/${partial.body.request_id}`)).body.state;
    if (state === 'partial') break;
    await page.waitForTimeout(200);
  }
  await nav(page, 'Sessions');
  // The sessions list labels that session by its last turn, as the transcript does.
  const card = page.locator('#view-sessions .cards > li', {
    has: page.getByRole('button', { name: `Resume session ${session.replace(/^session_/, '').slice(0, 10)}` }),
  });
  await expect(card.locator(':scope > .row .state-partial')).toHaveText('partial');
  await nav(page, 'Chat');
  await expect(turnState(page, 'partial')).toHaveText('partial');
  for (const name of ['succeeded', 'failed', 'uncertain', 'partial']) {
    const shape = await page.locator(`#transcript .state-${name}`).first().evaluate((node) => {
      const style = getComputedStyle(node);
      return `${style.borderTopStyle}|${style.color}|${getComputedStyle(node, '::before').content}`;
    });
    record(`g5_shape_${name}`, shape);
  }
  const shapes = ['succeeded', 'failed', 'uncertain', 'partial'].map((name) => evidence[`g5_shape_${name}`]);
  expect(new Set(shapes).size).toBe(shapes.length); // each state looks different
});

test('readiness: the Status view shows live versus ready from GET /v1/health, as the owner sees it', async ({ page }) => {
  await login(page, cell);
  await nav(page, 'Status');
  const line = page.locator('#readiness');
  await expect(line).toContainText('Live: yes');
  const owner = (await ownerAPI(cell, 'GET', '/v1/health')).body;
  expect(await line.getAttribute('data-live')).toBe(String(owner.live));
  await expect(line).toContainText(`Ready: ${owner.ready ? 'yes' : 'no'}`);
  const shown = (await page.locator('#status-body ul.checks > li > strong').allTextContents()).map((text) => text.trim());
  expect(shown).toEqual(owner.checks.map((check) => check.id));
  for (const check of owner.checks.filter((item) => !item.ok && item.fix)) {
    await expect(page.locator('#status-body ul.checks > li', { hasText: check.id }).first()).toContainText('Fix:');
  }
  await expect(page.locator('#connection')).toContainText(owner.ready ? 'ready' : 'not ready');
  record('readiness_view', { live: owner.live, ready: owner.ready, checks: shown.length, failing: owner.failing });
});

test('G6 accessibility: axe on every view, keyboard only, focus, live region, 320 px, 200% zoom, reduced motion', async ({ browser }) => {
  const context = await browser.newContext({ reducedMotion: 'reduce' });
  const page = await context.newPage();
  await login(page, cell);
  // Keyboard only: skip link, navigation, composer, send with Control+Enter.
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to main content' })).toBeFocused();
  const outline = await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
  expect(outline).toBe('solid'); // visible focus
  await page.getByLabel('Message').focus();
  await page.keyboard.type('[[say "typed with the keyboard only"]]');
  await page.keyboard.press('Control+Enter');
  await expect(page.locator('#transcript')).toContainText('typed with the keyboard only');
  await expect(page.locator('#announcer')).toContainText('Answer finished: succeeded');
  await page.getByRole('navigation').getByRole('button', { name: 'Skills', exact: true }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#view-skills')).toBeVisible();
  await page.getByRole('navigation').getByRole('button', { name: 'Chat', exact: true }).focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#view-chat')).toBeVisible();
  expect(await page.getByRole('banner').count()).toBe(1);
  expect(await page.getByRole('navigation').count()).toBe(1);
  expect(await page.getByRole('main').count()).toBe(1);
  // Reduced motion: no animation on the running/streaming symbols.
  const animation = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.className = 'state state-running';
    document.body.append(probe);
    const name = getComputedStyle(probe, '::before').animationName;
    probe.remove();
    return name;
  });
  expect(animation).toBe('none');
  const views = ['Chat', 'Sessions', 'Schedules and inbox', 'Skills', 'Memory and profile', 'Tools and MCP', 'Egress log', 'Status'];
  const report = { views: {}, viewports: {} };
  for (const [label, viewport] of [['1280', { width: 1280, height: 900 }], ['320', { width: 320, height: 800 }], ['200% zoom', { width: 640, height: 450 }]]) {
    await page.setViewportSize(viewport);
    const overflow = [];
    for (const name of views) {
      await nav(page, name);
      await page.waitForTimeout(150);
      const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']).analyze();
      const serious = results.violations.filter((item) => ['serious', 'critical'].includes(item.impact));
      report.views[`${label} ${name}`] = {
        violations: results.violations.map((item) => ({ id: item.id, impact: item.impact, nodes: item.nodes.length })),
        passes: results.passes.length,
      };
      expect(serious, `${label} ${name}: ${JSON.stringify(serious.map((item) => item.id))}`).toEqual([]);
      const width = await page.evaluate(() => document.documentElement.scrollWidth);
      if (width > viewport.width) overflow.push(`${name}:${width}`);
    }
    report.viewports[label] = { overflow };
    expect(overflow, label).toEqual([]);
  }
  record('g6_axe', report);
  await context.close();
});

test('G7 speed and size: first render, assets, no external requests, streaming latency versus the terminal', async ({ page }) => {
  const external = [];
  page.on('request', (request) => { if (!request.url().startsWith(cell.origin)) external.push(request.url()); });
  const cold = Date.now();
  await login(page, cell);
  await page.waitForFunction(() => window.__bsa && window.__bsa.firstRender !== null);
  record('g7_login_link_to_first_render_ms', Date.now() - cold);
  const renders = [];
  for (let index = 0; index < 5; index += 1) {
    await page.reload();
    await page.waitForFunction(() => window.__bsa && window.__bsa.firstRender !== null);
    renders.push(await page.evaluate(() => window.__bsa.firstRender));
  }
  const bytes = await page.evaluate(() => performance.getEntriesByType('resource')
    .filter((entry) => entry.name.includes('/ui/')).reduce((sum, entry) => sum + entry.decodedBodySize, 0)
    + performance.getEntriesByType('navigation')[0].decodedBodySize);
  expect(bytes).toBeLessThanOrEqual(150 * 1024);
  expect(external).toEqual([]);
  record('g7_first_render_ms', { median: median(renders), runs: renders });
  record('g7_page_bytes_loaded', bytes);
  // Streaming latency: server event time to arrival, browser versus the terminal (REPL
  // --json through the same daemon), the same scripted stream (a word every 10 ms).
  const words = Array.from({ length: 60 }, (_, index) => `w${index}`).join(' ');
  await send(page, `[[say "${words}"]]`);
  await expect(turnState(page, 'succeeded')).toBeVisible();
  const browserLags = await page.evaluate(() => window.__bsa.deltaLags.map((lag) => lag * 1000));
  const terminalLags = await new Promise((resolvePromise, reject) => {
    const env = { ...cell.cellEnv, BRAINSTEM_AGENT_WORKSPACE: cell.workspace };
    const child = spawn(PYTHON, ['-m', 'brainstem_agent', 'repl', '--json'], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    const lags = [];
    createInterface({ input: child.stdout }).on('line', (line) => {
      const document = JSON.parse(line);
      if (document.type === 'ready') child.stdin.write(`${JSON.stringify({ op: 'chat', text: `[[say "${words}"]]` })}\n`);
      if (document.type === 'delta' && typeof document.t === 'number') lags.push(performance.timeOrigin + performance.now() - document.t * 1000);
      if (document.type === 'result') { child.stdin.end(); }
    });
    child.on('exit', () => resolvePromise(lags));
    child.on('error', reject);
  });
  expect(browserLags.length).toBeGreaterThan(30);
  expect(terminalLags.length).toBeGreaterThan(30);
  const added = median(browserLags) - median(terminalLags);
  record('g7_stream_latency_ms', {
    browser_median: median(browserLags), terminal_median: median(terminalLags), added_median: added,
    browser_samples: browserLags.length, terminal_samples: terminalLags.length,
  });
  expect(added).toBeLessThan(250);
});

test('G5 daemon unreachable: shown, panels marked stale, the running turn stale', async ({ page }) => {
  const own = await startCell();
  try {
    await login(page, own);
    await send(page, '[[slow 30]]');
    await expect(page.locator('#transcript > li[data-live="true"] .state-streaming')).toBeVisible();
    own.child.kill('SIGKILL');
    await expect(page.locator('#connection')).toContainText('Daemon unreachable', { timeout: 15_000 });
    await expect(page.locator('#transcript > li[data-live="true"] .state-stale')).toContainText('stale');
    await expect(page.locator('#view-chat')).toHaveAttribute('data-stale', 'true');
    await expect(page.locator('#transcript .state-succeeded')).toHaveCount(0);
  } finally {
    await stopCell(own);
  }
});

test('G5 daemon restarted: an open page says so, apart from unreachable and signed out', async ({ page }) => {
  const own = await startCell();
  try {
    await login(page, own);
    await expect(page.locator('#connection')).toContainText('Connected', { timeout: 15_000 });
    const port = own.port;
    own.child.kill('SIGKILL');
    await expect(page.locator('#connection')).toContainText('Daemon unreachable', { timeout: 30_000 });
    await expect(page.locator('#connection')).toHaveAttribute('data-state', 'unreachable');
    await restartCell(own); // the same home: it comes back on its previous port
    expect(own.port).toBe(port);
    await expect(page.locator('#connection')).toContainText('Daemon restarted', { timeout: 30_000 });
    await expect(page.locator('#connection')).toHaveAttribute('data-state', 'restarted');
    await expect(page.locator('#signed-out')).toBeVisible();
    await expect(page.locator('#signed-out-title')).toHaveText('Daemon restarted');
    await expect(page.locator('#signed-out-reason')).toContainText('brainstem-agent open');
    // Signing out in a tab is a different state (and says so).
    const other = await page.context().newPage();
    await login(other, own);
    await other.getByRole('button', { name: 'Sign out' }).click();
    await expect(other.locator('#connection')).toHaveAttribute('data-state', 'signed-out');
    await expect(other.locator('#signed-out-title')).toHaveText('Signed out');
    await other.close();
  } finally {
    await stopCell(own);
  }
});
