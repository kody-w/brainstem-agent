// Support for the companion browser specs: a real daemon with scripted workers
// (runtime/tests/companion_support.py), the CLI's `open` link, and hostile origins.
import { spawn, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, readFileSync, existsSync, chmodSync } from 'node:fs';
import { createServer } from 'node:http';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';

export const REPO = resolve(new URL('../../..', import.meta.url).pathname);
export const RUNTIME = join(REPO, 'runtime');
export const PYTHON = process.env.BRAINSTEM_AGENT_TEST_PYTHON || 'python3.11';
const SCRATCH = join(REPO, '.cache', 'browser');

export function scratchDir(prefix) {
  mkdirSync(SCRATCH, { recursive: true, mode: 0o700 });
  const dir = mkdtempSync(join(SCRATCH, prefix));
  chmodSync(dir, 0o700);
  return dir;
}

// Start a daemon (scripted workers) in its own process; resolves with its ready record.
export async function startCell({ seed = null } = {}) {
  return spawnCell(scratchDir('cell-'), seed);
}

// Start the daemon of a cell again (the same home, after its process ended): the cell's
// record is updated in place (child, port, origin).
export async function restartCell(cell) {
  const again = await spawnCell(cell.dir, null);
  Object.assign(cell, again);
  return cell;
}

async function spawnCell(dir, seed) {
  const args = [join(RUNTIME, 'tests', 'companion_support.py'), 'serve', dir];
  if (seed) args.push('--seed', seed);
  const env = { ...process.env, PYTHONPATH: `${RUNTIME}:${join(RUNTIME, 'tests')}` };
  for (const key of Object.keys(env)) if (key.startsWith('BRAINSTEM_')) delete env[key];
  const child = spawn(PYTHON, args, { env, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr = (stderr + chunk).slice(-4000); });
  const ready = await new Promise((resolvePromise, reject) => {
    const lines = createInterface({ input: child.stdout });
    const timer = setTimeout(() => reject(new Error(`cell never ready: ${stderr}`)), 60_000);
    lines.on('line', (line) => {
      try {
        const record = JSON.parse(line);
        if (record.ready) { clearTimeout(timer); resolvePromise(record); }
      } catch { /* not ours */ }
    });
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error(`cell exited ${code}: ${stderr}`)); });
  });
  const origin = `http://127.0.0.1:${ready.port}`;
  const cellEnv = { ...env, ...ready.env, PATH: '/usr/bin:/bin', PYTHONPATH: RUNTIME };
  return { ...ready, dir, child, origin, cellEnv };
}

export async function stopCell(cell) {
  if (!cell) return;
  if (cell.child.exitCode === null) {
    const exited = new Promise((resolvePromise) => cell.child.once('exit', resolvePromise));
    cell.child.kill('SIGTERM');
    await Promise.race([exited, new Promise((r) => setTimeout(r, 30_000))]);
    if (cell.child.exitCode === null) cell.child.kill('SIGKILL');
  }
  rmSync(cell.dir, { recursive: true, force: true });
}

// The owner's CLI, exactly as a person or agent runs it.
export function cli(cell, args) {
  const result = spawnSync(PYTHON, ['-m', 'brainstem_agent', ...args], {
    env: cell.cellEnv, encoding: 'utf8', timeout: 120_000,
  });
  if (result.status !== 0) throw new Error(`cli ${args.join(' ')} failed: ${result.stderr}`);
  return args.includes('--json') ? JSON.parse(result.stdout) : result.stdout;
}

export function loginURL(cell) {
  return cli(cell, ['open', '--json']).url;
}

export async function login(page, cell) {
  const url = loginURL(cell);
  await page.goto(url);
  await page.waitForURL(`${cell.origin}/`);
  await page.getByRole('heading', { level: 1, name: 'Brainstem Agent' }).waitFor();
  return url;
}

// The owner's own view of the cell, through the bearer API (never the page's session).
export async function ownerAPI(cell, method, path, body) {
  const record = JSON.parse(readFileSync(join(cell.home, 'run', 'daemon.json'), 'utf8'));
  const response = await fetch(`http://127.0.0.1:${record.port}${path}`, {
    method,
    headers: { Authorization: `Bearer ${record.token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
}

export function workspaceFile(cell, name) {
  return existsSync(join(cell.workspace, name));
}

// A hostile web server: serves attack pages on its own port (another origin).
export async function startHostile(pages) {
  const server = createServer((request, response) => {
    const path = new URL(request.url, 'http://x').pathname;
    const page = pages[path];
    if (!page) { response.writeHead(404); response.end(); return; }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(page);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, port: server.address().port };
}

export function stopHostile(hostile) {
  if (hostile) hostile.server.close();
}
