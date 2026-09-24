// Live companion specs (G8, G9): a real daemon with unchanged Grail and real Copilot
// inference (the installed brainstem's connection). Gated: BRAINSTEM_AGENT_LIVE=1.
// About 10 Grail requests. Transcript summaries land in the evidence file.
import { test, expect } from '@playwright/test';
import { spawn, spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { REPO, RUNTIME, PYTHON, scratchDir } from './support.mjs';

const LIVE = process.env.BRAINSTEM_AGENT_LIVE === '1';
const EVIDENCE = process.env.BRAINSTEM_AGENT_BROWSER_EVIDENCE || join(REPO, '.cache', 'evidence', 'browser-live.json');
const evidence = {};
function record(key, value) {
  evidence[key] = value;
  mkdirSync(join(EVIDENCE, '..'), { recursive: true });
  writeFileSync(EVIDENCE, JSON.stringify(evidence, null, 2));
}

test.skip(!LIVE, 'live: set BRAINSTEM_AGENT_LIVE=1 (spends real Copilot turns)');
test.describe.configure({ mode: 'serial' });

let cell;

function cli(args, { json = true } = {}) {
  const result = spawnSync(PYTHON, ['-m', 'brainstem_agent', ...args, ...(json ? ['--json'] : [])], {
    env: cell.env, encoding: 'utf8', timeout: 600_000,
  });
  if (result.status !== 0) throw new Error(`cli ${args.join(' ')} exit ${result.status}: ${result.stderr.slice(-600)}`);
  return json ? JSON.parse(result.stdout) : result.stdout;
}

async function owner(method, path, body) {
  const record_ = JSON.parse(readFileSync(join(cell.home, 'run', 'daemon.json'), 'utf8'));
  const response = await fetch(`http://127.0.0.1:${record_.port}${path}`, {
    method, headers: { Authorization: `Bearer ${record_.token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return response.json();
}

test.beforeAll(async () => {
  if (!LIVE) return;
  const dir = scratchDir('live-');
  const home = join(dir, 'home');
  const workspace = join(dir, 'workspace');
  mkdirSync(home, { mode: 0o700 });
  mkdirSync(workspace, { mode: 0o700 });
  const cache = process.env.BRAINSTEM_AGENT_TEST_CACHE || join(REPO, '.cache', 'grail-cache');
  const env = { PATH: '/usr/bin:/bin', HOME: process.env.HOME, LANG: 'en_US.UTF-8', PYTHONPATH: RUNTIME,
    BRAINSTEM_AGENT_HOME: home, BRAINSTEM_AGENT_CACHE: cache, BRAINSTEM_AGENT_WORKSPACE: workspace };
  cell = { dir, home, workspace, env };
  const status = cli(['serve', '--detach', '--workspace', workspace]);
  cell.pid = status.pid;
  cell.origin = `http://127.0.0.1:${JSON.parse(readFileSync(join(home, 'run', 'daemon.json'), 'utf8')).port}`;
});

test.afterAll(async () => {
  if (!cell) return;
  try {
    const stopped = cli(['stop']);
    record('daemon_stop', { ok: stopped.ok, workers_gone: stopped.workers_gone, seconds: stopped.seconds });
    const sessions = (() => { try { return cli(['sessions']).sessions; } catch { return []; } })();
    record('sessions_after', sessions.length);
  } finally {
    rmSync(cell.dir, { recursive: true, force: true });
  }
});

const turnState = (page, name) => page.locator('#transcript > li').last().locator(`:scope > .row .state-${name}`);
const nav = (page, name) => page.getByRole('navigation').getByRole('button', { name, exact: true }).click();

async function login(page) {
  await page.goto(cli(['open']).url);
  await page.waitForURL(`${cell.origin}/`);
  await page.getByRole('heading', { level: 1, name: 'Brainstem Agent' }).waitFor();
}

async function send(page, text, { finished = true } = {}) {
  await page.getByLabel('Message').fill(text);
  await page.getByRole('button', { name: 'Send' }).click();
  if (finished) {
    await expect(page.locator('#transcript > li[data-live="true"]')).toHaveCount(0, { timeout: 300_000 });
  }
}

async function lastTurn(page) {
  const session = (await page.locator('#session-line').textContent()).replace('Session ', '');
  const view = await owner('GET', `/v1/sessions/${session}`);
  return { session, turn: view.turns[view.turns.length - 1], view };
}

function terminal() {
  const child = spawn(PYTHON, ['-m', 'brainstem_agent', 'repl', '--json'], { env: cell.env, stdio: ['pipe', 'pipe', 'pipe'] });
  const waiting = [];
  const lines = [];
  createInterface({ input: child.stdout }).on('line', (line) => {
    const document = JSON.parse(line);
    lines.push(document);
    for (const item of [...waiting]) {
      if (item.predicate(document)) { waiting.splice(waiting.indexOf(item), 1); item.resolve(document); }
    }
  });
  return {
    child, lines,
    next(predicate, timeout = 300_000) {
      const found = lines.find(predicate);
      if (found) { lines.splice(lines.indexOf(found), 1); return Promise.resolve(found); }
      return new Promise((resolve, reject) => {
        const entry = { predicate, resolve: (doc) => { lines.splice(lines.indexOf(doc), 1); resolve(doc); } };
        waiting.push(entry);
        setTimeout(() => reject(new Error('terminal: timed out')), timeout);
      });
    },
    send(document) { child.stdin.write(`${JSON.stringify(document)}\n`); },
    async close() { this.send({ op: 'exit' }); await new Promise((r) => child.on('exit', r)); },
  };
}

test('G9 live: log in, create notes/ui.txt, stream and receipt, cancel a long turn, approve a skill', async ({ page }) => {
  test.setTimeout(900_000);
  await login(page);
  const started = Date.now();
  await send(page, 'Create notes/ui.txt containing exactly: from the companion');
  const { session, turn } = await lastTurn(page);
  expect(turn.label).toBe('succeeded');
  await expect(turnState(page, 'succeeded')).toBeVisible();
  const content = readFileSync(join(cell.workspace, 'notes', 'ui.txt'), 'utf8');
  expect(content.replace(/\n$/, '')).toBe('from the companion');
  const writes = turn.receipts.filter((receipt) => receipt.tool === 'write_file');
  expect(writes.map((receipt) => receipt.state)).toContain('succeeded');
  await page.locator('#transcript > li').last().getByText(/^Receipts/).click();
  await expect(page.locator('#transcript > li').last()).toContainText('write_file');
  const states = await page.evaluate(() => window.__bsa.states.map((item) => item[1]));
  const deltas = await page.evaluate(() => window.__bsa.deltaLags.length);
  record('g9_create', { seconds: (Date.now() - started) / 1000, answer: turn.response.slice(0, 300),
    file_bytes: Buffer.byteLength(content), trailing_newline: content.endsWith('\n'),
    receipts: turn.receipts.map((receipt) => `${receipt.tool}:${receipt.state}`), states, deltas,
    segments: turn.segments.length });
  expect(deltas).toBeGreaterThan(0); // the answer streamed into the page
  // Cancel a long-running turn once its answer is streaming.
  await send(page, 'Write a detailed 1500-word essay about the history of mitochondria research. Do not use any tools.', { finished: false });
  await expect(page.locator('#transcript > li[data-live="true"]').locator(':scope > .row .state-streaming')).toBeVisible({ timeout: 180_000 });
  const stopping = Date.now();
  await page.getByRole('button', { name: 'Stop' }).click();
  await expect(page.locator('#transcript > li[data-live="true"]')).toHaveCount(0, { timeout: 30_000 });
  await expect(turnState(page, 'cancelled')).toHaveText('cancelled');
  const cancelled = (await lastTurn(page)).turn;
  expect(cancelled.label).toBe('cancelled');
  record('g9_cancel', { stop_to_cancelled_seconds: (Date.now() - stopping) / 1000, label: cancelled.label });
  // Save a skill from the conversation, then approve it in the companion.
  await send(page, 'Save how you created notes/ui.txt as a skill called ui-note.');
  const saved = (await lastTurn(page)).turn;
  expect(saved.label).toBe('succeeded');
  const before = await owner('GET', '/v1/skills/ui-note');
  expect(before.skill.review).toBe('unreviewed');
  await nav(page, 'Skills');
  await page.getByRole('button', { name: `Approve ui-note v${before.skill.version}` }).click();
  await expect(page.locator('#view-skills li', { hasText: 'ui-note' }).locator('.state-approved')).toBeVisible();
  const after = await owner('GET', '/v1/skills/ui-note');
  expect(after.skill.review).toBe('approved');
  record('g9_skill', { saved_answer: saved.response.slice(0, 200), review_before: before.skill.review,
    review_after: after.skill.review, version: after.skill.version, session });
});

test('G8 live: companion to terminal and back, same history, receipts and skills; a chat schedule reaches the inbox', async ({ page }) => {
  test.setTimeout(900_000);
  await login(page);
  await page.getByRole('button', { name: 'New session' }).click();
  await send(page, 'My code word is PAPAYA. Reply with just OK.');
  const first = await lastTurn(page);
  expect(first.turn.label).toBe('succeeded');
  const repl = terminal();
  const ready = await repl.next((doc) => doc.type === 'ready');
  expect(ready.mode).toBe('daemon');
  repl.send({ op: 'command', text: `/resume ${first.session}` });
  const resumed = await repl.next((doc) => doc.type === 'command');
  expect(resumed.result.turns.map((turn) => turn.turn_id)).toEqual(first.view.turns.map((turn) => turn.turn_id));
  repl.send({ op: 'chat', text: 'What code word did I tell you? Answer with one word.' });
  const answer = await repl.next((doc) => doc.type === 'result');
  expect(answer.result.state).toBe('succeeded');
  expect(answer.result.session_id).toBe(first.session);
  expect(answer.result.response.response.toUpperCase()).toContain('PAPAYA');
  const streamedTerminal = repl.lines.filter((doc) => doc.type === 'delta').length;
  repl.send({ op: 'command', text: '/history' });
  const history = await repl.next((doc) => doc.type === 'command');
  repl.send({ op: 'command', text: '/skills' });
  const terminalSkills = (await repl.next((doc) => doc.type === 'command')).result.skills;
  // The companion shows the same turns (with the terminal's answer and receipts) and skills.
  await nav(page, 'Sessions');
  await nav(page, 'Chat');
  await expect(page.locator('#transcript > li')).toHaveCount(2);
  await expect(page.locator('#transcript')).toContainText(/papaya/i);
  const companionView = await owner('GET', `/v1/sessions/${first.session}`);
  expect(companionView.turns.map((turn) => [turn.turn_id, turn.label, turn.receipts.length]))
    .toEqual(history.result.turns.map((turn) => [turn.turn_id, turn.label, turn.receipts.length]));
  const companionSkills = (await owner('GET', '/v1/skills')).skills;
  expect(terminalSkills.map((skill) => [skill.name, skill.review, skill.version]))
    .toEqual(companionSkills.map((skill) => [skill.name, skill.review, skill.version]));
  // The reverse: a conversation started in the terminal continues in the companion.
  repl.send({ op: 'command', text: '/new' });
  await repl.next((doc) => doc.type === 'command');
  repl.send({ op: 'chat', text: 'My favorite fruit is MANGO. Reply with just OK.' });
  const mango = await repl.next((doc) => doc.type === 'result');
  expect(mango.result.state).toBe('succeeded');
  await repl.close();
  await nav(page, 'Sessions');
  const short = mango.result.session_id.replace(/^session_/, '').slice(0, 10);
  await page.getByRole('button', { name: `Resume session ${short}` }).click();
  await expect(page.locator('#transcript')).toContainText('MANGO');
  await send(page, 'What fruit did I name? Answer with one word.');
  const reverse = await lastTurn(page);
  expect(reverse.session).toBe(mango.result.session_id);
  expect(reverse.turn.label).toBe('succeeded');
  expect(reverse.turn.response.toUpperCase()).toContain('MANGO');
  record('g8_parity', {
    companion_to_terminal: { session: first.session, answer: answer.result.response.response.slice(0, 80),
      terminal_deltas: streamedTerminal, turns: history.result.turns.length },
    terminal_to_companion: { session: mango.result.session_id, answer: reverse.turn.response.slice(0, 80) },
    skills_equal: terminalSkills.length === companionSkills.length, skills: companionSkills.map((skill) => skill.name),
  });
  // A schedule created in chat appears in the companion's inbox after it fires.
  await page.getByRole('button', { name: 'New session' }).click();
  await send(page, 'In 1 minute, write the word done into notes/sched.txt.');
  const scheduled = await lastTurn(page);
  expect(scheduled.turn.receipts.map((receipt) => receipt.tool)).toContain('schedule_create');
  await nav(page, 'Schedules and inbox');
  await expect(page.locator('#schedules-body .cards > li')).toHaveCount(1);
  const waitStarted = Date.now();
  let fired = false;
  while (Date.now() - waitStarted < 240_000) {
    await nav(page, 'Schedules and inbox');
    if (await page.locator('#inbox-body .cards > li .state-succeeded').count()) { fired = true; break; }
    await page.waitForTimeout(5_000);
  }
  expect(fired).toBe(true);
  const inbox = (await owner('GET', '/v1/inbox')).inbox;
  const content = readFileSync(join(cell.workspace, 'notes', 'sched.txt'), 'utf8');
  expect(content.toLowerCase()).toContain('done');
  record('g8_schedule', { waited_seconds: (Date.now() - waitStarted) / 1000, inbox_state: inbox[0].state,
    late: inbox[0].late, file: content.trim().slice(0, 40), answer: String((inbox[0].result || {}).response || '').slice(0, 160) });
  // How many Grail requests the whole live run used (every segment is one).
  let requests = 0;
  for (const item of (await owner('GET', '/v1/sessions')).sessions) {
    const view = await owner('GET', `/v1/sessions/${item.session_id}`);
    requests += view.turns.reduce((sum, turn) => sum + Math.max(1, turn.segments.length), 0);
  }
  record('live_grail_requests', requests);
});
