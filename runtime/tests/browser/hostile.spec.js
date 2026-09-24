// Hostile browser specs (threat model: contracts/companion.md, B tests). A real headless
// Chromium is signed in to the companion; pages on other origins then try to drive it.
import { test, expect } from '@playwright/test';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  REPO, startCell, stopCell, login, ownerAPI, workspaceFile, startHostile, stopHostile,
} from './support.mjs';

const EVIDENCE = process.env.BRAINSTEM_AGENT_BROWSER_EVIDENCE || join(REPO, '.cache', 'evidence', 'browser-companion.json');
function record(key, value) {
  const evidence = existsSync(EVIDENCE) ? JSON.parse(readFileSync(EVIDENCE, 'utf8')) : {};
  evidence[key] = value;
  mkdirSync(join(EVIDENCE, '..'), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(evidence, null, 2));
}

const ATTACK = `<!doctype html><html><head><title>hostile</title></head><body>
<form id="form" method="POST" enctype="text/plain" target="sink"></form>
<iframe name="sink" id="sink"></iframe>
<script>
(async () => {
  const params = new URL(location).searchParams;
  const T = params.get('target');
  const results = {};
  const note = (key, value) => { results[key] = value; };
  const job = (name) => JSON.stringify({ message: '[[write_file {"path": "pwned-' + name +
    '.txt", "content": "x"}]]' });
  try {
    const r = await fetch(T + '/v1/sessions', { credentials: 'include' });
    note('read', 'status:' + r.status + ':' + (await r.text()).slice(0, 80));
  } catch (e) { note('read', 'blocked:' + e.name); }
  try {
    const r = await fetch(T + '/v1/status', { credentials: 'include', mode: 'no-cors' });
    note('read_no_cors', 'type:' + r.type + ':' + (await r.text()).length);
  } catch (e) { note('read_no_cors', 'blocked:' + e.name); }
  try {
    await fetch(T + '/v1/requests', { method: 'POST', mode: 'no-cors', credentials: 'include',
      headers: { 'Content-Type': 'text/plain' }, body: job('nocors') });
    note('post_no_cors', 'sent');
  } catch (e) { note('post_no_cors', 'blocked:' + e.name); }
  try {
    const r = await fetch(T + '/v1/requests', { method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'X-Brainstem-CSRF': 'guess' }, body: job('cors') });
    note('post_cors', 'status:' + r.status);
  } catch (e) { note('post_cors', 'blocked:' + e.name); }
  note('beacon', navigator.sendBeacon(T + '/v1/requests',
    new Blob([job('beacon')], { type: 'text/plain' })));
  const form = document.getElementById('form');
  form.action = T + '/v1/requests';
  const field = document.createElement('input');
  field.name = '{"message": "[[write_file {\\"path\\": \\"pwned-form.txt\\", \\"content\\": \\"x\\"}]]", "x": "';
  field.value = '"}';
  form.appendChild(field);
  form.submit();
  note('form', 'submitted');
  await new Promise((done) => {
    const s = document.createElement('script');
    s.src = T + '/v1/sessions';
    s.onload = () => { note('script', 'loaded'); done(); };
    s.onerror = () => { note('script', 'error'); done(); };
    document.body.appendChild(s);
  });
  await new Promise((done) => {
    const img = new Image();
    img.onload = () => { note('img', 'loaded'); done(); };
    img.onerror = () => { note('img', 'error'); done(); };
    img.src = T + '/v1/status';
  });
  await new Promise((done) => {
    const source = new EventSource(T + '/v1/requests/req_x/events', { withCredentials: true });
    source.onmessage = (e) => { note('eventsource', 'message:' + e.data.slice(0, 40)); source.close(); done(); };
    source.onerror = () => { note('eventsource', 'error'); source.close(); done(); };
    setTimeout(() => { note('eventsource', results.eventsource || 'timeout'); source.close(); done(); }, 3000);
  });
  await new Promise((done) => {
    try {
      const ws = new WebSocket(T.replace('http', 'ws') + '/v1/status');
      ws.onopen = () => { note('websocket', 'open'); ws.close(); done(); };
      ws.onerror = () => { note('websocket', 'error'); done(); };
    } catch (e) { note('websocket', 'blocked:' + e.name); done(); }
    setTimeout(done, 3000);
  });
  const frame = document.createElement('iframe');
  frame.id = 'framed';
  frame.src = T + '/';
  document.body.appendChild(frame);
  await new Promise((done) => { frame.onload = done; setTimeout(done, 3000); });
  try { note('frame_read', 'read:' + frame.contentWindow.document.title); }
  catch (e) { note('frame_read', 'blocked:' + e.name); }
  const opened = window.open(T + '/', 'popup');
  await new Promise((done) => setTimeout(done, 2500));
  try { note('opened_closed', opened === null ? 'null' : String(opened.closed)); }
  catch (e) { note('opened_closed', 'blocked:' + e.name); }
  try { note('opened_read', 'read:' + opened.document.title); }
  catch (e) { note('opened_read', 'blocked:' + e.name); }
  window.results = results;
})();
</script></body></html>`;

let cell;
let hostile;

test.beforeAll(async () => {
  cell = await startCell({ seed: 'hostile' });
  hostile = await startHostile({ '/attack.html': ATTACK });
});

test.afterAll(async () => {
  stopHostile(hostile);
  await stopCell(cell);
});

async function attackFrom(page, origin) {
  const before = (await ownerAPI(cell, 'GET', '/v1/sessions')).body.sessions.length;
  const refusedBefore = (await ownerAPI(cell, 'GET', '/v1/status')).body.companion.refused;
  await page.goto(`${origin}/attack.html?target=${encodeURIComponent(cell.origin)}`);
  await page.waitForFunction(() => window.results !== undefined, null, { timeout: 60_000 });
  const results = await page.evaluate(() => window.results);
  await page.waitForTimeout(500);
  const after = (await ownerAPI(cell, 'GET', '/v1/sessions')).body.sessions.length;
  const refused = (await ownerAPI(cell, 'GET', '/v1/status')).body.companion.refused;
  return { results, before, after, refusedBefore, refused };
}

function expectNothingHappened(outcome) {
  const { results } = outcome;
  // Reads: blocked by CORS, opaque, or refused with 403 (never data).
  expect(results.read).toMatch(/^blocked:TypeError$/);
  expect(results.read_no_cors).toMatch(/^(type:opaque:0|blocked:TypeError)$/); // CORP blocks or opaque
  expect(results.post_cors).toMatch(/^blocked:TypeError$/); // the preflight is never granted
  expect(results.script).toBe('error');
  expect(results.img).toBe('error');
  expect(results.eventsource).not.toMatch(/^message/);
  expect(results.websocket).not.toBe('open');
  expect(results.frame_read).toMatch(/^blocked:/);
  expect(results.opened_read).toMatch(/^blocked:|^read:undefined/);
  expect(outcome.after).toBe(outcome.before); // no turn started
  for (const name of ['nocors', 'cors', 'beacon', 'form']) {
    expect(workspaceFile(cell, `pwned-${name}.txt`)).toBe(false);
  }
}

test('login: the one-time link becomes an HttpOnly Strict cookie and leaves the URL', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const url = await login(page, cell);
  const token = url.split('#')[1];
  expect(page.url()).toBe(`${cell.origin}/`);
  expect(page.url()).not.toContain(token);
  expect(await page.evaluate(() => document.cookie)).toBe('');
  expect(await page.evaluate(() => document.referrer)).toBe('');
  const cookies = await context.cookies(cell.origin);
  expect(cookies).toHaveLength(1);
  expect(cookies[0].name).toBe(`bsa_session_${cell.port}`);
  expect(cookies[0].httpOnly).toBe(true);
  expect(cookies[0].sameSite).toBe('Strict');
  expect(cookies[0].expires).toBe(-1); // a session cookie
  expect(cookies[0].value).not.toBe(token);
  const back = await page.goBack().catch(() => null);
  expect(page.url()).not.toContain(token);
  if (back) expect(back.url()).not.toContain(token);
  // The same link a second time (another tab, another context): refused, no cookie.
  const other = await browser.newContext();
  const second = await other.newPage();
  await second.goto(url);
  await expect(second.getByRole('alert')).toContainText(/already used|expired/i);
  expect(await other.cookies(cell.origin)).toHaveLength(0);
  expect(second.url()).not.toContain(token);
  await other.close();
  await context.close();
});

test('same-site other port: the cookie is sent, and still nothing can be read or changed', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await login(page, cell);
  const outcome = await attackFrom(await context.newPage(), `http://127.0.0.1:${hostile.port}`);
  record('g4_attacks_same_site', { ...outcome.results, turns_before: outcome.before, turns_after: outcome.after,
    refused_with_cookie: outcome.refused.with_cookie - outcome.refusedBefore.with_cookie });
  expectNothingHappened(outcome);
  expect(outcome.results.opened_closed).toBe('true'); // COOP severed the window reference
  // The daemon saw the cookie on refused requests: SameSite did not stop them, Origin, CSRF
  // and fetch metadata did.
  expect(outcome.refused.with_cookie).toBeGreaterThan(outcome.refusedBefore.with_cookie);
  await context.close();
});

test('cross-site origin: the same attacks from another site fail', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await login(page, cell);
  const outcome = await attackFrom(await context.newPage(), `http://evil.test:${hostile.port}`);
  record('g4_attacks_cross_site', { ...outcome.results, turns_before: outcome.before, turns_after: outcome.after });
  expectNothingHappened(outcome);
  await context.close();
});

test('DNS rebinding: a foreign name that resolves to 127.0.0.1 gets nothing', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  for (const path of ['/', '/login', '/ui/app.js', '/v1/status', '/v1/api']) {
    const response = await page.goto(`http://rebind.test:${cell.port}${path}`);
    expect(response.status(), path).toBe(403);
    expect(response.headers()['content-type']).toBe('application/json; charset=utf-8');
  }
  await context.close();
});

test('framing is refused (frame-ancestors none, X-Frame-Options DENY)', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await login(page, cell);
  const attacker = await context.newPage();
  await attacker.goto(`http://127.0.0.1:${hostile.port}/attack.html?target=${encodeURIComponent(cell.origin)}`);
  await attacker.waitForFunction(() => window.results !== undefined, null, { timeout: 60_000 });
  for (const frame of attacker.frames()) {
    if (frame === attacker.mainFrame()) continue;
    const loaded = await frame.locator('main#main').count().catch(() => 0);
    expect(loaded, frame.url()).toBe(0);
  }
  await context.close();
});

async function visitEveryView(page) {
  const views = ['Chat', 'Sessions', 'Schedules and inbox', 'Skills', 'Memory and profile',
    'Tools and MCP', 'Egress log', 'Status'];
  for (const name of views) {
    await page.getByRole('navigation').getByRole('button', { name, exact: true }).click();
    await page.waitForTimeout(250);
  }
}

async function expectInert(page) {
  expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
  const active = await page.evaluate(() => {
    const main = document.getElementById('main');
    const tags = main.querySelectorAll('a,img,script,iframe,svg,object,embed,style,link,form input[type=image]');
    const handlers = [...document.querySelectorAll('*')].filter((el) =>
      [...el.attributes].some((attribute) => attribute.name.startsWith('on')));
    return { tags: tags.length, handlers: handlers.length };
  });
  expect(active).toEqual({ tags: 0, handlers: 0 });
}

for (const bypassCSP of [false, true]) {
  test(`hostile payloads in every field render as text (CSP ${bypassCSP ? 'bypassed' : 'enforced'})`, async ({ browser }) => {
    const context = await browser.newContext({ bypassCSP });
    const page = await context.newPage();
    const dialogs = [];
    page.on('dialog', (dialog) => { dialogs.push(dialog.message()); dialog.dismiss(); });
    const errors = [];
    page.on('pageerror', (error) => errors.push(String(error)));
    await login(page, cell);
    await page.getByRole('navigation').getByRole('button', { name: 'Sessions', exact: true }).click();
    const sessions = page.locator('#view-sessions');
    await expect(sessions).toContainText('<img src=x onerror="window.__pwned=1">');
    // Resume every session: inputs, answers, receipts (file names, tool output) as text.
    const resumes = sessions.getByRole('button', { name: /^Resume/ });
    const count = await resumes.count();
    expect(count).toBeGreaterThanOrEqual(4);
    for (let index = 0; index < count; index += 1) {
      await page.getByRole('navigation').getByRole('button', { name: 'Sessions', exact: true }).click();
      await sessions.getByRole('button', { name: /^Resume/ }).nth(index).click();
      await expect(page.locator('#transcript')).toContainText('window.__pwned');
      await expectInert(page);
    }
    await expect(page.locator('#transcript')).toBeVisible();
    await visitEveryView(page);
    for (const [view, text] of [
      ['#view-schedules', 'sched <img src=x onerror="window.__pwned=9">'],
      ['#view-schedules', '<script>window.__pwned=2</script>'],
      ['#view-skills', '<svg onload="window.__pwned=3"></svg>'],
      ['#view-memory', 'fact <img src=x'],
      ['#view-memory', 'profile <img src=x'],
      ['#view-egress', '/<img src=x'],
    ]) {
      await page.getByRole('navigation').getByRole('button', {
        name: { '#view-schedules': 'Schedules and inbox', '#view-skills': 'Skills',
          '#view-memory': 'Memory and profile', '#view-egress': 'Egress log' }[view], exact: true }).click();
      await expect(page.locator(view)).toContainText(text);
    }
    await page.getByRole('navigation').getByRole('button', { name: 'Skills', exact: true }).click();
    await page.locator('#view-skills').getByRole('button', { name: /^Show hostile-skill/ }).click();
    await expect(page.locator('#view-skills')).toContainText('second <img src=x');
    await expectInert(page);
    expect(dialogs).toEqual([]);
    expect(errors).toEqual([]);
    await context.close();
  });
}

// Unicode's Bidi_Control characters: ALM, LRM, RLM, LRE, RLE, PDF, LRO, RLO, LRI, RLI, FSI, PDI.
const BIDI = ['\u061c', '\u200e', '\u200f', '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
  '\u2066', '\u2067', '\u2068', '\u2069'];
const VISIBLE = BIDI.map((char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`).join('');

test('bidi controls in untrusted text are shown, never applied (stored and streamed)', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const spoof = BIDI.join('');
  const stored = await ownerAPI(cell, 'POST', '/v1/turn', {
    timeout: 60,
    message: `asked ${spoof}here [[say ${JSON.stringify(`said ${spoof}`)}]] [[remember ${JSON.stringify(
      { text: `bidi fact ${spoof}tail`, scope: 'workspace' })}]]`,
  });
  expect(stored.body.ok, JSON.stringify(stored.body).slice(0, 300)).toBe(true);
  const raw = (text) => BIDI.filter((char) => text.includes(char)).map((char) => char.charCodeAt(0).toString(16));
  await login(page, cell);
  await page.getByLabel('Message').fill(`[[say ${JSON.stringify(`live ${spoof}`)}]]`);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('#transcript')).toContainText(`live ${VISIBLE}`);
  await expect(page.locator('#transcript > li').last().locator(':scope > .row .state-succeeded')).toBeVisible();
  expect(raw(await page.locator('#transcript').innerText())).toEqual([]);
  expect(raw(await page.locator('#announcer').innerText())).toEqual([]);
  const short = stored.body.session_id.replace(/^session_/, '').slice(0, 10);
  await page.getByRole('navigation').getByRole('button', { name: 'Sessions', exact: true }).click();
  await expect(page.locator('#view-sessions')).toContainText(`asked ${VISIBLE}here`);
  await page.locator('#view-sessions').getByRole('button', { name: `Resume session ${short}` }).click();
  await expect(page.locator('#transcript')).toContainText(`said ${VISIBLE}`);
  await expect(page.locator('#transcript')).toContainText(`asked ${VISIBLE}here`);
  await page.getByRole('navigation').getByRole('button', { name: 'Memory and profile', exact: true }).click();
  await expect(page.locator('#view-memory')).toContainText(`bidi fact ${VISIBLE}tail`);
  expect(raw(await page.locator('body').innerText())).toEqual([]);
  await context.close();
});
