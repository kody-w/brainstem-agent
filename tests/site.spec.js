import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import { MIME_TYPES, PUBLIC_PATH } from './server.mjs';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const CANONICAL = 'https://kody-w.github.io/brainstem-agent/';
const PLATFORMS = [
  { value: 'macos', label: 'macOS' },
  { value: 'linux', label: 'Linux' },
  { value: 'windows', label: 'Windows' },
];
const COMMANDS = {
  macos: 'curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash',
  linux: 'curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash',
  windows: 'irm https://raw.githubusercontent.com/kody-w/rapp-installer/main/install.ps1 | iex',
};
// The agent runtime and first-run blocks are published verbatim from the site facts. No line carries
// an inline "# note": pasted into zsh, the default macOS shell, a note reaches the command as arguments.
const BLOCKS = {
  'command-agent': [
    'git clone https://github.com/kody-w/brainstem-agent.git',
    'cd brainstem-agent',
    'python3.11 -m venv .venv',
    '.venv/bin/pip install ./runtime',
    '.venv/bin/brainstem-agent setup',
    '.venv/bin/brainstem-agent doctor --deep',
  ].join('\n'),
  'command-first-run': [
    '.venv/bin/brainstem-agent chat "Create notes/hello.txt containing hi"',
    '.venv/bin/brainstem-agent serve --detach',
    '.venv/bin/brainstem-agent status',
  ].join('\n'),
  // The interactive session comes last: pasted as one block, the lines before it have run already,
  // so it never reads them as messages, in any shell.
  'command-everyday': [
    '.venv/bin/brainstem-agent open',
    '.venv/bin/brainstem-agent backup --output backups',
    '.venv/bin/brainstem-agent version',
    '.venv/bin/brainstem-agent',
  ].join('\n'),
  'command-remove': [
    '.venv/bin/brainstem-agent stop',
    '.venv/bin/brainstem-agent service uninstall',
  ].join('\n'),
};

const CANON = {
  status_labels: {
    available: 'Available (experimental)',
    partial: 'Partial',
    'in-development': 'In development',
    'not-yet': 'Not yet',
  },
  primary: [
    ['deploy', 'partial'], ['always-on', 'partial'], ['schedule', 'available'],
    ['connect', 'not-yet'], ['remember', 'available'], ['isolate', 'available'],
  ],
  secondary: [
    ['skills', 'available'], ['long-work', 'available'], ['delegate', 'available'],
    ['tools', 'available'], ['web', 'available'], ['mcp', 'available'],
    ['receipts', 'available'], ['companion', 'available'], ['browser-media', 'not-yet'],
  ],
};
const PRIMARY_TITLES = ['Deploy', 'Always available', 'Schedule', 'Connect', 'Remember', 'Isolate'];

const RUNS = {
  'long-chain': {
    prompt: 'Create chain/1.txt containing 1. Then repeatedly read the latest file and write the next file containing the value plus one, up to chain/6.txt. Then report the final value.',
    answer: '**Final value: 6**\n\nThe chain has been created successfully from chain/1.txt through chain/6.txt, with each file containing the value one greater than the previous file.',
    log: '…\n[read_file] chain/5.txt (1 bytes):\n5\n[write_file] Wrote 1 bytes to chain/6.txt.',
  },
  'parallel-helpers': {
    prompt: 'Delegate two independent sub-tasks to helpers in parallel: helper A writes par/a.txt containing alpha, helper B writes par/b.txt containing beta. Then tell me both file contents.',
    answer: 'Done! Here are the file contents:\n\n- **par/a.txt**: `alpha`\n- **par/b.txt**: `beta`\n\nBoth files were created in parallel by the helpers as requested.',
    log: "[delegate_tasks] 2 of 2 helpers succeeded (in parallel, …).\n\nHelper 1 (Helper A): succeeded\nDone. I've created the file `par/a.txt` with the content `alpha` (no trailing newline).\n\nHelper 2 (Helper B): succeeded\nDone. I've written the file `par/b.txt` with the content 'beta' (no trailing newline).\n…",
  },
  schedule: {
    prompt: 'In 1 minute, write the current time into notes/time.txt.',
    answer: "Done! I've scheduled a task to write the current time to notes/time.txt in 1 minute (at 2026-09-23T04:40:38).",
    log: "[schedule_create] A schedule may only hold capabilities from ['files.read', 'files.write', 'memory.read', 'memory.write', 'shell.run', 'schedule.read', 'schedule.write']; refused: ['run_command'].\n[schedule_create] Created schedule … with capabilities ['shell.run'].",
  },
};

const REPOSITORY = 'https://github.com/kody-w/brainstem-agent';
const RUNTIME_DOCS = `${REPOSITORY}/blob/main/runtime/README.md`;
const EXTERNAL_LINKS = new Set([
  REPOSITORY,
  RUNTIME_DOCS,
  `${REPOSITORY}/issues`,
  'https://github.com/kody-w/rapp-installer',
]);
// Rendered READMEs the page may link at a heading: the repository home (README.md) and the runtime docs.
const README_PAGES = new Set([REPOSITORY, RUNTIME_DOCS]);
const CORE_PREFIX = 'https://github.com/kody-w/rapp-installer/';
const MANIFEST_PATH = `${PUBLIC_PATH}assets/capabilities.json`;

// The file a link into this repository shows: a blob page shows that file; with a heading anchor,
// the home page shows README.md and a tree page shows its folder's README.md.
function repositoryFile(href) {
  const url = new URL(href);
  const match = url.origin === 'https://github.com'
    && url.pathname.match(/^\/kody-w\/brainstem-agent(?:\/(blob|tree)\/main((?:\/[^/]+)*))?\/?$/);
  if (!match) return null;
  const [, kind, path = ''] = match;
  const file = decodeURIComponent(path.replace(/^\//, ''));
  if (kind === 'blob') return file;
  if (!url.hash) return null;
  return file ? `${file}/README.md` : 'README.md';
}

// GitHub's heading anchors: the heading's text in lower case, without punctuation other than hyphens
// and underscores, each space a hyphen, repeats numbered -1, -2 and so on. Code blocks hold no headings.
function headingSlugs(markdown) {
  const slugs = new Set();
  const add = text => {
    const base = text
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/<[^>]*>/g, '')
      .replace(/`/g, '')
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{M}\p{N}\p{Pc} -]/gu, '')
      .replace(/ /g, '-');
    let slug = base;
    for (let count = 1; slugs.has(slug); count += 1) slug = `${base}-${count}`;
    slugs.add(slug);
  };
  let fence = null;
  let previous = '';
  for (const line of markdown.split(/\r?\n/)) {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length
          && !line.slice(marker[0].length).trim()) fence = null;
      continue;
    }
    if (marker) {
      fence = marker[1];
      previous = '';
      continue;
    }
    const atx = line.match(/^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/);
    if (atx) {
      add(atx[1] ?? '');
      previous = '';
      continue;
    }
    if (/^ {0,3}(?:=+|-+)[ \t]*$/.test(line) && previous && !/^ {0,3}(?:[-*+>|]|\d+[.)])/.test(previous)) {
      add(previous);
      previous = '';
      continue;
    }
    previous = line.trim() && !/^ {4}/.test(line) ? line : '';
  }
  return slugs;
}

test.beforeEach(async ({ context, baseURL }) => {
  const origin = new URL(baseURL).origin;
  await context.route('**/*', route => (
    new URL(route.request().url()).origin === origin ? route.continue() : route.abort()
  ));
});

async function expectPlatform(page, value) {
  for (const platform of PLATFORMS) {
    const panel = page.locator(`article.command-panel[data-platform="${platform.value}"]`);
    const radio = page.getByRole('radio', { name: platform.label, exact: true });
    if (platform.value === value) {
      await expect(panel).toBeVisible();
      await expect(radio).toBeChecked();
    } else {
      await expect(panel).toBeHidden();
      await expect(radio).not.toBeChecked();
    }
  }
}

async function loadManifest(request) {
  const response = await request.get('assets/capabilities.json');
  expect(response.status()).toBe(200);
  expect(response.headers()['content-type']).toBe('application/json');
  return response.json();
}

const normalize = text => text.replace(/\s+/g, ' ').trim();

test('has one h1, the brand CTA, resolving anchors and only allowed external links', async ({ page, baseURL }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('./');
  await expect(page.locator('h1')).toHaveAccessibleName(/^Give your AI\s*a nervous\s*system\.$/);
  await expect(page.locator('h1')).toHaveCount(1);
  await expect(page.getByRole('main')).toHaveCount(1);
  await expect(page.getByRole('link', { name: 'RAPP Brainstem home', exact: true })).toHaveAttribute('href', '#top');
  const ctas = page.getByRole('link', { name: 'Give me my Brainstem', exact: true });
  expect(await ctas.count()).toBeGreaterThanOrEqual(2);
  for (const cta of await ctas.all()) {
    await expect(cta).toHaveAttribute('href', '#install');
  }
  const nav = page.getByRole('navigation', { name: 'Main navigation' });
  for (const [label, id] of [
    ['Install', 'install'], ['Capabilities', 'capabilities'], ['Recorded runs', 'runs'],
    ['Limits', 'limits'], ['FAQ', 'questions'],
  ]) {
    await nav.getByRole('link', { name: label, exact: true }).click();
    expect(new URL(page.url()).hash).toBe(`#${id}`);
  }

  const hrefs = await page.locator('a').evaluateAll(links => links.map(link => link.getAttribute('href')));
  for (const href of hrefs) {
    expect(href).toBeTruthy();
    expect(href.trim()).not.toBe('');
    expect(href).not.toBe('#');
    const url = new URL(href, baseURL);
    if (url.origin === new URL(baseURL).origin) {
      expect([PUBLIC_PATH, MANIFEST_PATH], href).toContain(url.pathname);
      if (url.hash) {
        const id = decodeURIComponent(url.hash.slice(1));
        expect(await page.evaluate(target => Boolean(document.getElementById(target)), id), href).toBe(true);
      }
    } else {
      expect(href, 'external links are absolute https').toMatch(/^https:\/\//);
      const headingLink = href.includes('#') && README_PAGES.has(href.slice(0, href.indexOf('#')));
      expect(EXTERNAL_LINKS.has(href) || headingLink || href.startsWith(CORE_PREFIX), `unexpected destination ${href}`)
        .toBe(true);
    }
  }
  for (const href of [...EXTERNAL_LINKS, 'https://github.com/kody-w/rapp-installer/issues']) {
    expect(await page.locator(`a[href="${href}"]`).count(), href).toBeGreaterThan(0);
  }
});

test('links into this repository reach files and README headings that exist', async ({ page }) => {
  // The slug rules, on headings shaped like the ones GitHub renders in these READMEs.
  for (const [markdown, slugs] of [
    ['## Quick start (every command accepts `--json`)', ['quick-start-every-command-accepts---json']],
    ['# Runtime (experimental, 0.2.0)', ['runtime-experimental-020']],
    ['### Setup (recommended)', ['setup-recommended']],
    ['### `GET /v1/status`', ['get-v1status']],
    ['## Backups & restores ##', ['backups--restores']],
    ['## Privacy: what leaves your [Mac](README.md)', ['privacy-what-leaves-your-mac']],
    ['# Install\n\n```sh\n# not a heading\n```\n\n## Install\n\nSetext heading\n---\n', ['install', 'install-1', 'setext-heading']],
  ]) {
    expect([...headingSlugs(markdown)], markdown).toEqual(slugs);
  }

  await page.goto('./');
  const hrefs = await page.locator('a[href]').evaluateAll(links => links.map(link => link.getAttribute('href')));
  const checked = [];
  for (const href of new Set(hrefs)) {
    if (!/^https:\/\//.test(href)) continue;
    const file = repositoryFile(href);
    if (file === null) continue;
    expect(existsSync(join(ROOT, file)), `${href} shows ${file}, which exists`).toBe(true);
    const { hash } = new URL(href);
    if (hash) {
      expect(file, `${href} anchors a Markdown file`).toMatch(/\.md$/);
      const anchors = [...headingSlugs(readFileSync(join(ROOT, file), 'utf8'))];
      expect(anchors, `${href} names a heading of ${file}`).toContain(decodeURIComponent(hash.slice(1)));
    }
    checked.push(href);
  }
  expect(checked, 'the runtime documentation link is checked').toContain(RUNTIME_DOCS);
});

test('puts the requirements checklist before every command, then two ordered steps', async ({ page }) => {
  await page.goto('./');
  const requirements = page.locator('#install #requirements');
  await expect(requirements).toBeVisible();
  for (const phrase of [
    'macOS only', 'Apple silicon', '/usr/bin/sandbox-exec', 'Python 3.11 or newer', '3.13', 'Git',
    'internet access', 'GitHub account with Copilot access', 'no API key', '0.2.0', 'not a hosted service',
  ]) {
    await expect(requirements).toContainText(phrase);
  }
  const order = await page.evaluate(() => {
    const checklist = document.getElementById('requirements');
    const commands = [...document.querySelectorAll('pre code')];
    const before = (a, b) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    return {
      checklistFirst: commands.length > 0 && commands.every(code => before(checklist, code)),
      steps: [...document.querySelectorAll('ol.install-steps > li')].map(step => step.id),
      sequence: ['requirements', 'step-core', 'step-agent', 'first-run', 'everyday', 'doctor', 'troubleshooting', 'remove']
        .map(id => document.getElementById(id))
        .every((element, index, all) => element && (index === 0 || before(all[index - 1], element))),
    };
  });
  expect(order).toEqual({ checklistFirst: true, steps: ['step-core', 'step-agent'], sequence: true });

  const agent = page.locator('#step-agent');
  await expect(agent.locator('.tag-strong', { hasText: 'macOS only' })).toBeVisible();
  await expect(agent.locator('.tag-strong', { hasText: 'Experimental 0.2.0' })).toBeVisible();
  await expect(agent).toContainText('--json');
  await expect(page.locator('#step-core')).toContainText('http://localhost:7071');
  await expect(page.locator('#doctor')).toContainText('doctor --deep');
  const failures = await page.locator('#troubleshooting dt').allTextContents();
  for (const failure of ['Not signed in', 'Wrong Python version', 'Not macOS', 'Sandbox unavailable']) {
    expect(failures).toContain(failure);
  }
  for (const phrase of ['stop', 'service uninstall', '~/.brainstem-agent', '~/.brainstem']) {
    await expect(page.locator('#remove')).toContainText(phrase);
  }
  // The everyday block explains each line beside it, in prose.
  for (const phrase of ['/help', 'Ctrl-C', 'Ctrl-D', 'one-time sign-in link', '127.0.0.1',
    'nothing opens a browser for you', 'backups', 'new or empty', 'unencrypted', 'version',
    'The last line', 'paste the whole block']) {
    await expect(page.locator('#everyday')).toContainText(phrase);
  }
});

test('shows every command exactly, with the runtime blocks verbatim and a copy button each', async ({ page }) => {
  await page.goto('./');
  await expect(page.locator('.platform-picker input[type="radio"][name="platform"]')).toHaveCount(3);
  for (const { value, label } of PLATFORMS) {
    const radio = page.getByRole('radio', { name: label, exact: true });
    await expect(radio).toHaveAttribute('type', 'radio');
    await expect(radio).toHaveAttribute('name', 'platform');
    await expect(radio).toHaveAttribute('value', value);
    expect(await page.locator(`#command-${value}`).textContent()).toBe(COMMANDS[value]);
    await expect(page.locator(`button[data-copy="command-${value}"]`)).toHaveText('Copy command');
  }
  for (const [id, text] of Object.entries(BLOCKS)) {
    expect(await page.locator(`#${id}`).textContent(), id).toBe(text);
    await expect(page.locator(`button[data-copy="${id}"]`)).toBeVisible();
    await expect(page.locator(`button[data-copy="${id}"]`)).toHaveText('Copy commands');
  }
  const unbound = await page.locator('pre code').evaluateAll(codes => codes
    .filter(code => !document.querySelector(`button[data-copy="${code.id}"]`))
    .map(code => code.id || code.textContent));
  expect(unbound).toEqual([]);
});

test('uses only brainstem-agent commands, flags and paths the runtime documentation lists', async ({ page }) => {
  const readme = readFileSync(join(ROOT, 'runtime', 'README.md'), 'utf8');
  await page.goto('./');
  const snippets = await page.locator('code').allTextContents();
  const lines = snippets.flatMap(snippet => snippet.split('\n'));
  const subcommands = new Set();
  for (const line of lines) {
    const match = line.match(/(?:^|\s|\/)brainstem-agent\s+([a-z-]+)(?:\s+(install|uninstall))?/);
    if (match) {
      subcommands.add(match[1]);
      if (match[2]) expect(readme).toContain(match[2]);
    }
    for (const flag of line.match(/(?<=^|\s)--[a-z-]+/g) || []) {
      expect(readme, `flag ${flag}`).toContain(flag);
    }
  }
  expect([...subcommands].sort()).toEqual([
    'backup', 'chat', 'doctor', 'open', 'serve', 'service', 'setup', 'status', 'stop', 'version',
  ]);
  for (const subcommand of subcommands) {
    expect(readme, `subcommand ${subcommand}`).toMatch(new RegExp(`brainstem[-_]agent ${subcommand}\\b`));
  }
  for (const word of ['stop', 'service install', 'service uninstall', 'serve --detach', 'doctor --deep', 'status', 'setup']) {
    expect(snippets, `inline mention of ${word}`).toContain(word);
  }
  for (const path of snippets.filter(snippet => snippet.startsWith('~/'))) {
    expect(readme, path).toContain(path);
  }
});

test('copies every command block exactly as shown and reports success only after completion', async ({ page }) => {
  await page.addInitScript(() => {
    window.copyCalls = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText(text) {
          window.copyCalls.push(text);
          return new Promise(resolve => { window.finishCopy = resolve; });
        },
      },
    });
  });
  await page.goto('./');
  const copied = [];
  const targets = [
    ...PLATFORMS.map(({ value, label }) => ({ id: `command-${value}`, text: COMMANDS[value], label, done: 'Command copied.' })),
    ...Object.entries(BLOCKS).map(([id, text]) => ({ id, text, done: 'Commands copied.' })),
  ];
  for (const target of targets) {
    if (target.label) await page.getByRole('radio', { name: target.label, exact: true }).check();
    const button = page.locator(`button[data-copy="${target.id}"]`);
    const status = page.locator(`#${await button.getAttribute('data-copy-status')}`);
    await expect(status).toHaveAttribute('role', 'status');
    await expect(status).toHaveAttribute('aria-live', 'polite');
    await button.click();
    copied.push(target.text);
    await expect.poll(() => page.evaluate(() => window.copyCalls)).toEqual(copied);
    await expect(status).not.toHaveText(target.done);
    await page.evaluate(() => window.finishCopy());
    await expect(status).toHaveText(target.done);
  }
});

test('command blocks carry no # notes, and Copy copies exactly the text each block displays', async ({ page }) => {
  await page.addInitScript(() => {
    window.copyCalls = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { async writeText(text) { window.copyCalls.push(text); } },
    });
  });
  await page.goto('./');
  const ids = await page.locator('pre > code').evaluateAll(codes => codes.map(code => code.id));
  expect([...ids].sort()).toEqual([...PLATFORMS.map(({ value }) => `command-${value}`), ...Object.keys(BLOCKS)].sort());
  for (const id of ids) {
    const platform = PLATFORMS.find(({ value }) => id === `command-${value}`);
    if (platform) await page.getByRole('radio', { name: platform.label, exact: true }).check();
    await expect(page.locator(`#${id}`)).toBeVisible();
    const block = await page.locator(`#${id}`).evaluate(code => ({
      displayed: code.innerText,
      source: code.textContent,
      elements: code.children.length,
      generated: [code, code.closest('pre')].flatMap(node => ['::before', '::after']
        .map(pseudo => getComputedStyle(node, pseudo).content))
        .filter(content => !['none', 'normal'].includes(content)),
    }));
    // Plain text only: nothing is hidden from the copy, styled out of a selection or added by CSS.
    expect(block.elements, `${id} holds only text`).toBe(0);
    expect(block.generated, `${id} has no generated text`).toEqual([]);
    expect(block.displayed, `${id} displays its text unchanged`).toBe(block.source);
    for (const line of block.displayed.split('\n')) {
      expect(line, `${id} has no inline # note`).not.toContain(' #');
      expect(line, `${id} has no comment line`).not.toMatch(/^\s*#/);
    }
    // A line that opens the interactive session reads the terminal, so only a block's last line may.
    const interactive = block.displayed.split('\n').slice(0, -1)
      .filter(line => /(?:^|\/)brainstem-agent(?:\s+repl\b.*)?$/.test(line.trim()));
    expect(interactive, `${id} opens the interactive session only on its last line`).toEqual([]);
    const before = await page.evaluate(() => window.copyCalls.length);
    await page.locator(`button[data-copy="${id}"]`).click();
    await expect.poll(() => page.evaluate(() => window.copyCalls.length)).toBe(before + 1);
    expect(await page.evaluate(() => window.copyCalls.at(-1)), `${id} copies what it displays`).toBe(block.displayed);
  }
});

for (const unavailable of [false, true]) {
  test(`clipboard ${unavailable ? 'absence' : 'rejection'} selects the commands for a manual copy`, async ({ page }) => {
    await page.addInitScript(missing => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: missing ? undefined : {
          async writeText() {
            throw new DOMException('Clipboard permission denied', 'NotAllowedError');
          },
        },
      });
    }, unavailable);
    await page.goto('./');
    for (const [id, text, message] of [
      ['command-macos', COMMANDS.macos, 'Could not copy. Select the command and copy it manually.'],
      ['command-agent', BLOCKS['command-agent'], 'Could not copy. Select the commands and copy them manually.'],
    ]) {
      const button = page.locator(`button[data-copy="${id}"]`);
      await button.click();
      await expect(page.locator(`#${await button.getAttribute('data-copy-status')}`)).toHaveText(message);
      const command = page.locator(`#${id}`);
      await expect(command).toBeVisible();
      expect(await command.textContent()).toBe(text);
      expect(await command.evaluate(element => getComputedStyle(element).userSelect)).not.toBe('none');
      await expect(page.locator(`pre:has(#${id})`)).toBeFocused();
      expect(await page.evaluate(() => window.getSelection().toString())).toBe(text);
    }
  });
}

test.describe('without JavaScript', () => {
  test.use({ javaScriptEnabled: false });

  test('keeps every command, capability, status, recorded run and limit readable', async ({ page }) => {
    await page.goto('./');
    for (const { value } of PLATFORMS) {
      await expect(page.locator(`article.command-panel[data-platform="${value}"]`)).toBeVisible();
      await expect(page.locator(`#command-${value}`)).toBeVisible();
      expect(await page.locator(`#command-${value}`).textContent()).toBe(COMMANDS[value]);
    }
    for (const [id, text] of Object.entries(BLOCKS)) {
      await expect(page.locator(`#${id}`)).toBeVisible();
      expect(await page.locator(`#${id}`).textContent()).toBe(text);
    }
    await expect(page.locator('button[data-copy]:visible')).toHaveCount(0);
    const entries = [...CANON.primary, ...CANON.secondary];
    await expect(page.locator('[data-capability]')).toHaveCount(entries.length);
    for (const [id, status] of entries) {
      const card = page.locator(`[data-capability="${id}"]`);
      await expect(card).toBeVisible();
      await expect(card.locator('[data-status]')).toHaveText(CANON.status_labels[status]);
      await expect(card.locator('[data-status]')).toBeVisible();
    }
    await expect(page.locator('[data-run]')).toHaveCount(3);
    for (const run of await page.locator('[data-run]').all()) {
      await expect(run).toBeVisible();
      await expect(run).toContainText('Recorded run');
    }
    await expect(page.locator('#limits')).toBeVisible();
    await expect(page.locator('#requirements')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Give me my Brainstem', exact: true }).first()).toBeVisible();
  });
});

test('capability manifest and page match both ways', async ({ page, request }) => {
  const manifest = await loadManifest(request);
  expect(Object.keys(manifest)).toEqual(['schema', 'updated', 'runtime', 'status_labels', 'capabilities']);
  expect(manifest.schema).toBe('rapp-brainstem/site-capabilities-v1');
  expect(manifest.updated).toBe('2026-09-23');
  expect(manifest.runtime).toMatchObject({ name: 'Brainstem Agent', version: '0.2.0' });
  const pyproject = readFileSync(join(ROOT, 'runtime', 'pyproject.toml'), 'utf8');
  expect(manifest.runtime.version, 'the published version is the runtime package version')
    .toBe(pyproject.match(/^version\s*=\s*"([^"]+)"/m)?.[1]);
  expect(manifest.runtime.platform).toMatch(/macOS/);
  expect(manifest.runtime.grail).toEqual({
    repository: 'kody-w/rapp-installer',
    commit: '49db80c8c6b6caa7647369beaf477d374a8f293c',
    version: '0.6.16',
    kernel_sha256: 'bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4',
  });
  expect(manifest.status_labels).toEqual(CANON.status_labels);
  const expected = [
    ...CANON.primary.map(([id, status]) => ({ id, status, group: 'primary' })),
    ...CANON.secondary.map(([id, status]) => ({ id, status, group: 'secondary' })),
  ];
  expect(manifest.capabilities.map(({ id, status, group }) => ({ id, status, group }))).toEqual(expected);

  await page.goto('./');
  const runs = await page.locator('[data-run]').evaluateAll(items => items.map(item => item.dataset.run));
  for (const capability of manifest.capabilities) {
    expect(Object.keys(capability)).toEqual(['id', 'title', 'group', 'status', 'summary', 'limits', 'evidence']);
    expect(capability.title).toMatch(/\S/);
    expect(capability.summary).toMatch(/\S/);
    expect(Array.isArray(capability.limits)).toBe(true);
    expect(capability.evidence.length).toBeGreaterThan(0);
    for (const evidence of capability.evidence) {
      expect(['recorded-run', 'docs', 'tests']).toContain(evidence.kind);
      if (evidence.kind === 'recorded-run') {
        expect(runs, `${capability.id} cites a run shown on the page`).toContain(evidence.ref);
      } else {
        expect(evidence.ref).toMatch(/^runtime\//);
        expect(statSync(join(ROOT, evidence.ref)).isFile(), evidence.ref).toBe(true);
      }
    }
  }

  const shown = await page.locator('[data-capability]').evaluateAll(cards => cards.map(card => ({
    id: card.dataset.capability,
    group: card.closest('.cap-grid') ? 'primary' : card.closest('.more-grid') ? 'secondary' : 'unbound',
    title: card.querySelector('.cap-title')?.textContent.trim(),
    statuses: [...card.querySelectorAll('[data-status]')].map(status => ({
      status: status.dataset.status,
      text: status.textContent,
      visible: status.getClientRects().length > 0,
    })),
    summary: card.querySelector('.cap-summary')?.textContent.replace(/\s+/g, ' ').trim(),
    limit: card.querySelector('.cap-limit-text')?.textContent.replace(/\s+/g, ' ').trim() ?? '',
  })));
  // Page to manifest: no unbound card, no duplicate.
  const byId = new Map(manifest.capabilities.map(capability => [capability.id, capability]));
  expect(new Set(shown.map(card => card.id)).size).toBe(shown.length);
  for (const card of shown) {
    const capability = byId.get(card.id);
    expect(capability, `card ${card.id} is in the manifest`).toBeTruthy();
    expect(card.group).toBe(capability.group);
    expect(card.title).toBe(capability.title);
    expect(card.statuses).toEqual([{
      status: capability.status, text: manifest.status_labels[capability.status], visible: true,
    }]);
    expect(card.summary).toBe(normalize(capability.summary));
    expect(card.limit).toBe(normalize(capability.limits.join(' ')));
  }
  // Manifest to page: every entry shown exactly once, in manifest order.
  expect(shown.map(card => card.id)).toEqual(manifest.capabilities.map(capability => capability.id));
  // Every status on the page belongs to a capability.
  expect(await page.locator('[data-status]').count()).toBe(manifest.capabilities.length);
  for (const binding of await page.locator('[data-manifest]').all()) {
    const path = await binding.getAttribute('data-manifest');
    const value = path.split('.').reduce((node, key) => node?.[key], manifest);
    expect(await binding.textContent(), path).toBe(value);
  }
});

test('shows six illustrated primary cards in order, then the nine further capabilities', async ({ page }) => {
  await page.goto('./');
  const primary = page.locator('#capabilities .cap-grid > [data-capability]');
  await expect(primary).toHaveCount(6);
  expect(await primary.evaluateAll(cards => cards.map(card => card.dataset.capability)))
    .toEqual(CANON.primary.map(([id]) => id));
  expect(await primary.locator('.cap-title').allTextContents()).toEqual(PRIMARY_TITLES);
  for (const card of await primary.all()) {
    const art = card.locator('svg.cap-art');
    await expect(art).toHaveCount(1);
    await expect(art).toHaveAttribute('aria-hidden', 'true');
    expect(await art.locator('path, rect, circle').count()).toBeGreaterThan(2);
    await expect(card.locator('.cap-limit-text')).toHaveText(/\S/);
  }
  const secondary = page.locator('#capabilities .more-grid > [data-capability]');
  expect(await secondary.evaluateAll(cards => cards.map(card => card.dataset.capability)))
    .toEqual(CANON.secondary.map(([id]) => id));
  const lastPrimary = await primary.last().boundingBox();
  const firstSecondary = await secondary.first().boundingBox();
  expect(firstSecondary.y).toBeGreaterThan(lastPrimary.y);
});

test('quotes recorded runs verbatim, labelled and dated, never as live', async ({ page }) => {
  await page.goto('./');
  const runs = page.locator('#runs [data-run]');
  const ids = await runs.evaluateAll(items => items.map(item => item.dataset.run));
  expect(ids.length).toBeGreaterThanOrEqual(1);
  expect(ids.length).toBeLessThanOrEqual(3);
  await expect(page.locator('#runs')).toContainText('Not a live demo');
  for (const id of ids) {
    const run = page.locator(`#runs [data-run="${id}"]`);
    const expected = RUNS[id];
    expect(expected, id).toBeTruthy();
    await expect(run.locator('.run-label')).toHaveText(/^Recorded run\s+2026-09-23\s+experimental runtime$/);
    await expect(run.locator('.console-tag')).toHaveText('Not live');
    expect(await run.locator('.run-prompt').textContent()).toBe(expected.prompt);
    expect(await run.locator('.run-answer').textContent()).toBe(expected.answer);
    expect(await run.locator('.run-log').textContent()).toBe(expected.log);
    await expect(run.locator('input, textarea, form, button')).toHaveCount(0);
  }
  const text = await page.locator('body').textContent();
  expect(text).not.toMatch(/\b(?:sch|ses|turn|occ|proc|call)_[0-9a-f]{6,}\b/);
  expect(text).not.toMatch(/fetched_at|searched_at|session_id|America\/|\d+\.\d{3}s/);
  expect(text).not.toMatch(/\blive (?:demo|agent|session)\b(?<!Not a live demo)/i);
});

test('states requirements and limits in a visible section, not only in the FAQ', async ({ page }) => {
  await page.goto('./');
  const limits = page.locator('#limits');
  await expect(limits).toBeVisible();
  expect(await limits.evaluate(element => Boolean(element.closest('details')))).toBe(false);
  for (const phrase of [
    'macOS only', 'Experimental, version 0.2.0', 'GitHub Copilot sign-in through your Brainstem',
    'Inference is remote', 'leave your Mac', 'Work pauses while the Mac sleeps', 'Not a hosted service',
    'messaging channels', 'cloud or remote hosting', 'browser automation', 'images and voice',
    'encrypted, scheduled or off-machine backups', 'The core stays unchanged', 'Linux and Windows',
  ]) {
    await expect(limits).toContainText(phrase);
  }
  // What 0.2.0 ships is never listed as missing: backups and restores, upgrades and rollbacks,
  // sign-in replacement, the terminal session and the web companion.
  const notYet = normalize(await limits.locator('li', { hasText: 'Not yet:' }).textContent());
  for (const shipped of [/backup and restore|restores?\b/i, /upgrade|rollback/i, /credential|sign-in/i,
    /companion|terminal/i]) {
    expect(notYet).not.toMatch(shipped);
  }
  const claims = normalize(await page.locator('[data-capability], #limits, #install, #questions')
    .evaluateAll(parts => parts.map(part => part.textContent).join(' ')));
  for (const stale of [
    /\b(?:backup|restore|upgrade|rollback|credential rotation|companion)\b[^.;]*\bnot (?:there |released )?yet\b/i,
    /not released/i, /being built/i, /credential rotation/i,
  ]) {
    expect(claims).not.toMatch(stale);
  }
  const facts = page.getByRole('list', { name: 'At a glance' });
  await expect(facts).toBeVisible();
  for (const phrase of ['macOS only', 'Experimental 0.2.0', 'Inference is remote', 'Not a hosted service']) {
    await expect(facts).toContainText(phrase);
  }
});

for (const width of [320, 390, 768, 1024, 1440]) {
  test(`has no horizontal overflow at ${width}px, including every command and expanded FAQ`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('./');
    await page.evaluate(async () => {
      await document.fonts.ready;
      document.querySelectorAll('details').forEach(details => { details.open = true; });
    });
    for (const { label } of PLATFORMS) {
      await page.getByRole('radio', { name: label, exact: true }).check();
      const size = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        viewport: document.documentElement.clientWidth,
      }));
      expect(size.document).toBeLessThanOrEqual(size.viewport + 1);
      expect(size.body).toBeLessThanOrEqual(size.viewport + 1);
    }
  });
}

// Impact, the first display face, is missing on Linux (the CI runner), where a wider face stands in.
const WIDER_DISPLAY_FACES = [
  { name: 'Arial', family: 'Arial, "Liberation Sans", sans-serif' },
  { name: 'Verdana, as wide as the Linux default DejaVu Sans', family: 'Verdana, "DejaVu Sans", sans-serif' },
  { name: 'a face wider than any fallback', family: 'Verdana, "DejaVu Sans", sans-serif', tracking: '0.2em' },
];

for (const width of [320, 390]) {
  test(`wraps display headings instead of overflowing at ${width}px when a wider face replaces the display font`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('./');
    await page.evaluate(() => document.querySelectorAll('details').forEach(details => { details.open = true; }));
    for (const face of WIDER_DISPLAY_FACES) {
      const result = await page.evaluate(({ family, tracking }) => {
        document.getElementById('wider-display-face')?.remove();
        for (const element of document.querySelectorAll('[data-wider-face]')) delete element.dataset.widerFace;
        const style = document.createElement('style');
        style.id = 'wider-display-face';
        style.textContent = `:root { --display: ${family} !important; }`;
        document.head.append(style);
        const display = [...document.querySelectorAll('body *')]
          .filter(element => getComputedStyle(element).fontFamily === family);
        if (tracking) {
          for (const element of display) element.dataset.widerFace = '';
          style.textContent += ` [data-wider-face] { letter-spacing: ${tracking} !important; }`;
        }
        const viewport = document.documentElement.clientWidth;
        const overflowing = display
          .filter(element => getComputedStyle(element).display !== 'inline')
          .filter(element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            const box = element.getBoundingClientRect();
            const right = Math.max(box.left, ...[...range.getClientRects()].map(rect => rect.right));
            return right > Math.min(box.right, viewport) + 1;
          })
          .map(element => `${element.tagName.toLowerCase()} "${element.textContent.trim().slice(0, 40)}"`);
        return {
          headings: [...new Set(display.map(element => element.tagName.toLowerCase()))],
          overflowing,
          document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          viewport,
        };
      }, face);
      expect(result.headings, `${face.name} sets the display headings`).toEqual(expect.arrayContaining(['h1', 'h2', 'h3']));
      expect(result.overflowing, `${face.name}: headings stay inside their boxes`).toEqual([]);
      expect(result.document, `${face.name}: page width`).toBeLessThanOrEqual(result.viewport + 1);
      expect(result.body, `${face.name}: body width`).toBeLessThanOrEqual(result.viewport + 1);
    }
  });
}

test('describes the current product in title, description, Open Graph and Twitter metadata', async ({ page }) => {
  await page.goto('./');
  await expect(page).toHaveTitle(/^RAPP Brainstem\b.*\bexperimental\b.*\bmacOS\b/);
  const content = selector => page.locator(selector).getAttribute('content');
  const description = await content('meta[name="description"]');
  for (const phrase of ['RAPP Brainstem', 'Brainstem Agent', 'experimental', '0.2.0', 'macOS only', 'unchanged', 'remote']) {
    expect(description).toContain(phrase);
  }
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href', CANONICAL);
  await expect(page.locator('meta[property="og:url"]')).toHaveAttribute('content', CANONICAL);
  await expect(page.locator('meta[property="og:type"]')).toHaveAttribute('content', 'website');
  await expect(page.locator('meta[property="og:site_name"]')).toHaveAttribute('content', 'RAPP Brainstem');
  await expect(page.locator('meta[name="twitter:card"]')).toHaveAttribute('content', 'summary_large_image');
  for (const selector of ['meta[property="og:title"]', 'meta[name="twitter:title"]']) {
    expect(await content(selector)).toMatch(/^RAPP Brainstem\b.*experimental.*macOS/);
  }
  for (const selector of ['meta[property="og:description"]', 'meta[name="twitter:description"]']) {
    const text = await content(selector);
    expect(text).toMatch(/experimental/i);
    expect(text).toContain('0.2.0');
    expect(text).toContain('remote');
  }
  expect(await page.locator('link[rel~="icon"]').count()).toBeGreaterThan(0);
  for (const selector of ['meta[property="og:image"]', 'meta[name="twitter:image"]']) {
    const image = new URL(await content(selector));
    expect(image.origin).toBe(new URL(CANONICAL).origin);
    expect(image.pathname).toBe(`${PUBLIC_PATH}assets/social-card.png`);
  }
});

test('loads only local public assets, with valid MIME types and bounded compressed transfer', async ({
  page, request, baseURL,
}) => {
  const origin = new URL(baseURL).origin;
  const responses = [];
  const requests = [];
  const failures = [];
  const errors = [];
  const sockets = [];
  page.on('response', response => responses.push(response));
  page.on('request', resource => requests.push(resource.url()));
  page.on('requestfailed', resource => failures.push(resource.url()));
  page.on('pageerror', error => errors.push(error.message));
  page.on('websocket', socket => sockets.push(socket.url()));
  await page.goto('./', { waitUntil: 'networkidle' });
  await page.evaluate(async () => { await document.fonts.ready; });
  expect(requests.filter(url => new URL(url).origin !== origin)).toEqual([]);
  expect(failures).toEqual([]);
  expect(errors).toEqual([]);
  expect(sockets).toEqual([]);

  const transfers = await Promise.all(responses.map(async response => {
    expect(response.status(), response.url()).toBe(200);
    const url = new URL(response.url());
    expect(url.pathname.startsWith(PUBLIC_PATH)).toBe(true);
    const type = response.headers()['content-type'];
    expect(type).toBe(MIME_TYPES[extname(url.pathname).toLowerCase() || '.html']);
    const body = await response.body();
    // Measure actual response bodies with gzip for text; count precompressed media in full.
    const bytes = /^(text\/|application\/javascript|application\/json|image\/svg\+xml)/.test(type)
      ? gzipSync(body).length : body.length;
    return { url: url.pathname, bytes, script: response.request().resourceType() === 'script' };
  }));
  const initialBytes = transfers.reduce((total, asset) => total + asset.bytes, 0);
  const inlineScripts = await page.locator('script:not([src])').allTextContents();
  const scriptBytes = transfers.filter(asset => asset.script).reduce((total, asset) => total + asset.bytes, 0)
    + inlineScripts.filter(source => source.trim()).reduce((total, source) => total + gzipSync(source).length, 0);
  expect(initialBytes, 'Initial compressed page and assets exceed 250 KB').toBeLessThanOrEqual(250_000);
  expect(scriptBytes, 'Compressed JavaScript exceeds 8 KB').toBeLessThanOrEqual(8_000);
  await test.info().attach('transfer-budget', {
    body: JSON.stringify({ initialBytes, scriptBytes, transfers }, null, 2),
    contentType: 'application/json',
  });

  const assets = await page.evaluate(() => {
    const references = [...document.querySelectorAll(
      'script[src], img[src], source[src], link[rel~="stylesheet"], link[rel~="icon"], link[rel~="preload"]',
    )].map(element => element.getAttribute('src') || element.getAttribute('href'));
    for (const element of document.querySelectorAll('[srcset]')) {
      references.push(...element.getAttribute('srcset').split(',').map(value => value.trim().split(/\s+/)[0]));
    }
    return references.filter(Boolean);
  });
  const socialImage = new URL(await page.locator('meta[property="og:image"]').getAttribute('content'));
  assets.push(`${socialImage.pathname}${socialImage.search}`, 'assets/capabilities.json');
  for (const asset of new Set(assets)) {
    const url = new URL(asset, baseURL);
    expect(url.origin, asset).toBe(origin);
    expect(url.pathname.startsWith(`${PUBLIC_PATH}assets/`), asset).toBe(true);
    const response = await request.get(url.href);
    expect(response.status(), asset).toBe(200);
    expect(response.headers()['content-type'], asset).toBe(MIME_TYPES[extname(url.pathname).toLowerCase()]);
  }
});

test('keeps the published files within 180 KB, with no web fonts and only original SVG images', async ({ page }) => {
  const files = readdirSync(join(ROOT, 'assets'), { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => join(entry.parentPath ?? entry.path, entry.name));
  const names = files.map(file => file.slice(join(ROOT, 'assets').length + 1)).sort();
  for (const name of names) {
    expect(['.css', '.js', '.json', '.svg'].includes(extname(name)) || name === 'social-card.png', name).toBe(true);
  }
  const bytes = [join(ROOT, 'index.html'), ...files.filter(file => !file.endsWith('social-card.png'))]
    .reduce((total, file) => total + statSync(file).size, 0);
  expect(bytes, `index.html + assets without the social card: ${bytes} bytes`).toBeLessThanOrEqual(180 * 1024);
  await test.info().attach('site-weight', { body: JSON.stringify({ bytes, names }), contentType: 'application/json' });

  await page.goto('./');
  expect(await page.evaluate(() => [...document.fonts].length)).toBe(0);
  const images = await page.locator('img').evaluateAll(items => items.map(item => item.getAttribute('src')));
  for (const src of images) expect(src).toMatch(/^assets\/[a-z-]+\.svg$/);
  const css = readFileSync(join(ROOT, 'assets', 'site.css'), 'utf8');
  expect(css).not.toMatch(/@font-face|@import|url\(/);
});

for (const width of [1440, 390]) {
  test(`passes axe WCAG 2.2 AA checks at ${width}px`, async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('./');
    await page.locator('details').evaluateAll(elements => {
      elements.forEach(element => { element.open = true; });
    });
    const states = width === 1440 ? PLATFORMS : PLATFORMS.slice(0, 1);
    for (const { label } of states) {
      await page.getByRole('radio', { name: label, exact: true }).check();
      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
        .analyze();
      const serious = results.violations.filter(violation => ['serious', 'critical'].includes(violation.impact));
      expect(serious, `${label} serious or critical violations at ${width}px`).toEqual([]);
      expect(results.violations, `${label} violations at ${width}px`).toEqual([]);
    }
  });
}

test('reduced motion removes animated movement and smooth scrolling', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('./');
  const movement = await page.evaluate(() => {
    const seconds = value => parseFloat(value) / (value.trim().endsWith('ms') ? 1000 : 1);
    const properties = /^(all|transform|translate|rotate|scale|perspective|top|right|bottom|left|width|height|margin.*|padding.*|offset.*)$/;
    const problems = [];
    for (const element of document.querySelectorAll('*')) {
      for (const pseudo of [null, '::before', '::after']) {
        const style = getComputedStyle(element, pseudo);
        const durations = style.transitionDuration.split(',').map(seconds);
        const movingTransition = style.transitionProperty.split(',').some((property, index) => (
          properties.test(property.trim()) && durations[index % durations.length] > 0.01
        ));
        const animationDurations = style.animationDuration.split(',').map(seconds);
        const animated = style.animationName.split(',').some((name, index) => (
          name.trim() !== 'none' && animationDurations[index % animationDurations.length] > 0.01
        ));
        if (movingTransition || animated || style.scrollBehavior === 'smooth') {
          problems.push(`${element.tagName.toLowerCase()}#${element.id}${pseudo || ''}`);
        }
      }
    }
    return problems;
  });
  expect(movement).toEqual([]);
});

test('offers a visible skip link, landmarks and a logical heading outline', async ({ page }) => {
  await page.goto('./');
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: 'Skip to content', exact: true });
  await expect(skip).toBeFocused();
  const bounds = await skip.boundingBox();
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  await page.keyboard.press('Enter');
  expect(new URL(page.url()).hash).toBe('#main');
  await expect(page.getByRole('main')).toBeFocused();
  const structure = await page.locator('body').ariaSnapshot();
  expect(structure).toContain('banner:');
  expect(structure).toContain('navigation "Main navigation"');
  expect(structure).toContain('main:');
  expect(structure).toContain('contentinfo:');
  await expect(page.getByRole('group', { name: 'Choose your operating system' })).toHaveCount(1);
  const levels = await page.locator('h1, h2, h3, h4, h5, h6').evaluateAll(headings => headings.map(heading => Number(heading.tagName[1])));
  expect(levels[0]).toBe(1);
  expect(levels.filter(level => level === 1)).toHaveLength(1);
  levels.forEach((level, index) => {
    if (index > 0) expect(level - levels[index - 1], `heading ${index} jumps a level`).toBeLessThanOrEqual(1);
  });
});

test('works by keyboard alone with a visible focus indicator on every stop', async ({ page }) => {
  await page.addInitScript(() => {
    window.copyCalls = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { async writeText(text) { window.copyCalls.push(text); } },
    });
  });
  await page.goto('./');
  const stops = [];
  for (let index = 0; index < 120; index += 1) {
    await page.keyboard.press('Tab');
    const stop = await page.evaluate(() => {
      const element = document.activeElement;
      const target = element.matches('input') ? element.closest('label') : element;
      const style = getComputedStyle(target);
      return {
        key: element.matches('input[type="radio"]') ? `radio:${element.value}`
          : element.dataset.copy || element.getAttribute('href') || element.id || element.textContent.trim().slice(0, 80),
        visibleFocus: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) >= 2,
        footer: Boolean(element.closest('footer')),
      };
    });
    stops.push(stop);
    if (stop.footer && stop.key === 'https://github.com/kody-w/rapp-installer/issues') break;
  }
  expect(stops.filter(stop => !stop.visibleFocus)).toEqual([]);
  const keys = stops.map(stop => stop.key);
  for (const key of ['#main', '#install', '#capabilities', 'radio:macos', 'command-macos', '#troubleshooting',
    'command-agent', 'command-first-run', 'command-everyday', 'command-remove', '#run-schedule',
    'How are RAPP Brainstem and Brainstem Agent different?', 'https://github.com/kody-w/rapp-installer/issues']) {
    expect(keys, key).toContain(key);
  }
  expect(keys.filter(key => key.startsWith('radio:')), 'one tab stop for the radio group').toEqual(['radio:macos']);

  await page.locator('button[data-copy="command-agent"]').focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => window.copyCalls)).toEqual([BLOCKS['command-agent']]);
  await page.locator('button[data-copy="command-first-run"]').focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#copy-status-first-run')).toHaveText('Commands copied.');

  const question = page.locator('.faq-list summary').nth(1);
  await question.focus();
  await page.keyboard.press('Enter');
  await expect(question.locator('xpath=..')).toHaveAttribute('open', '');

  await page.getByRole('radio', { name: 'macOS', exact: true }).focus();
  for (const { value, label } of [PLATFORMS[1], PLATFORMS[2], PLATFORMS[0]]) {
    await page.keyboard.press('ArrowRight');
    await expectPlatform(page, value);
    await expect(page.getByRole('radio', { name: label, exact: true })).toBeFocused();
  }
  await page.keyboard.press('ArrowLeft');
  await expectPlatform(page, 'windows');
});

test('keeps the layout contained at 200% CSS zoom and in mobile landscape', async ({ page }) => {
  for (const layout of [
    { width: 1440, height: 1000, zoom: '2' },
    { width: 667, height: 375, zoom: '1' },
  ]) {
    await page.setViewportSize({ width: layout.width, height: layout.height });
    await page.goto('./');
    await page.evaluate(zoom => { document.documentElement.style.zoom = zoom; }, layout.zoom);
    await page.getByRole('radio', { name: 'Windows', exact: true }).check();
    const width = await page.evaluate(() => ({
      content: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(width.content).toBeLessThanOrEqual(width.viewport + 1);
    await expect(page.locator('#command-windows')).toBeVisible();
    await expect(page.locator('#command-agent')).toBeVisible();
  }
});
