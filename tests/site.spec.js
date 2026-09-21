import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { extname } from 'node:path';
import { gzipSync } from 'node:zlib';
import { MIME_TYPES, PUBLIC_PATH } from './server.mjs';

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

test('has the promised heading, working navigation, and absolute source links', async ({ page, baseURL }) => {
  await page.goto('./');
  await expect(page.locator('h1')).toHaveAccessibleName(/^Give your AI\s*a nervous\s*system\.$/);
  await expect(page.locator('h1')).toHaveCount(1);
  await expect(page.getByRole('main')).toHaveCount(1);
  const ctas = page.getByRole('link', { name: 'Give me my Brainstem', exact: true });
  expect(await ctas.count()).toBeGreaterThan(0);
  for (const cta of await ctas.all()) {
    await expect(cta).toHaveAttribute('href', '#install');
  }
  for (const id of ['install', 'how-it-works', 'example', 'questions']) {
    await expect(page.locator(`#${id}`)).toHaveCount(1);
    await page.locator(`a[href="#${id}"]`).first().click();
    expect(new URL(page.url()).hash).toBe(`#${id}`);
  }
  await expect(page.locator('#example')).toContainText('Illustrative walkthrough');

  const hrefs = await page.locator('a').evaluateAll(links => links.map(link => link.getAttribute('href')));
  for (const href of hrefs) {
    expect(href).toBeTruthy();
    expect(href.trim()).not.toBe('');
    expect(href).not.toBe('#');
    const url = new URL(href, baseURL);
    if (url.origin === new URL(baseURL).origin) {
      expect(url.pathname).toBe(PUBLIC_PATH);
      if (url.hash) {
        const id = decodeURIComponent(url.hash.slice(1));
        expect(await page.evaluate(target => Boolean(document.getElementById(target)), id)).toBe(true);
      }
    } else {
      expect(url.protocol).toBe('https:');
      expect(href).toMatch(/^https:\/\//);
    }
  }
  for (const href of [
    'https://github.com/kody-w/rapp-installer',
    'https://github.com/kody-w/brainstem-agent',
  ]) {
    expect(await page.locator(`a[href="${href}"]`).count()).toBeGreaterThan(0);
  }
});

test('exposes exact commands and keyboard-operable native platform radios', async ({ page }) => {
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
  await expectPlatform(page, 'macos');
  await expect(page.locator('button[data-copy="command-macos"]')).toBeVisible();
  await page.getByRole('radio', { name: 'macOS', exact: true }).focus();
  for (const { value, label } of [PLATFORMS[1], PLATFORMS[2], PLATFORMS[0]]) {
    await page.keyboard.press('ArrowRight');
    await expectPlatform(page, value);
    await expect(page.getByRole('radio', { name: label, exact: true })).toBeFocused();
  }
  await page.keyboard.press('ArrowLeft');
  await expectPlatform(page, 'windows');
});

test('copies each exact command and reports success only after clipboard completion', async ({ page }) => {
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
  const status = page.locator('#copy-status');
  await expect(status).toHaveAttribute('role', 'status');
  await expect(status).toHaveAttribute('aria-live', 'polite');
  const copied = [];
  for (const { value, label } of PLATFORMS) {
    await page.getByRole('radio', { name: label, exact: true }).check();
    await page.locator(`button[data-copy="command-${value}"]`).click();
    copied.push(COMMANDS[value]);
    await expect.poll(() => page.evaluate(() => window.copyCalls)).toEqual(copied);
    if (copied.length === 1) {
      await expect(status).not.toHaveText('Command copied.');
    }
    await page.evaluate(() => window.finishCopy());
    await expect(status).toHaveText('Command copied.');
  }
});

for (const unavailable of [false, true]) {
  test(`clipboard ${unavailable ? 'absence' : 'rejection'} provides an honest manual fallback`, async ({ page }) => {
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
    await page.locator('button[data-copy="command-macos"]').click();
    await expect(page.locator('#copy-status')).toHaveText(
      'Could not copy. Select the command and copy it manually.',
    );
    const command = page.locator('#command-macos');
    await expect(command).toBeVisible();
    expect(await command.textContent()).toBe(COMMANDS.macos);
    expect(await command.evaluate(element => getComputedStyle(element).userSelect)).not.toBe('none');
  });
}

test.describe('without JavaScript', () => {
  test.use({ javaScriptEnabled: false });

  test('keeps all commands and the illustrative example available', async ({ page }) => {
    await page.goto('./');
    for (const { value } of PLATFORMS) {
      await expect(page.locator(`article.command-panel[data-platform="${value}"]`)).toBeVisible();
      const command = page.locator(`#command-${value}`);
      await expect(command).toBeVisible();
      expect(await command.textContent()).toBe(COMMANDS[value]);
      await expect(page.locator(`button[data-copy="command-${value}"]`)).toBeHidden();
    }
    await expect(page.locator('#example')).toBeVisible();
    await expect(page.locator('#example')).toContainText('Illustrative walkthrough');
    await expect(page.getByRole('link', { name: 'Give me my Brainstem', exact: true }).first()).toBeVisible();
  });
});

for (const width of [320, 375, 768, 1440]) {
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

test('includes production canonical, description, favicon, and Open Graph metadata', async ({ page }) => {
  await page.goto('./');
  await expect(page).toHaveTitle(/Brainstem/i);
  await expect(page.locator('meta[name="description"]')).toHaveAttribute('content', /\S/);
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href', CANONICAL);
  await expect(page.locator('meta[property="og:title"]')).toHaveAttribute('content', /Brainstem/i);
  await expect(page.locator('meta[property="og:description"]')).toHaveAttribute('content', /\S/);
  await expect(page.locator('meta[property="og:url"]')).toHaveAttribute('content', CANONICAL);
  await expect(page.locator('meta[property="og:type"]')).toHaveAttribute('content', 'website');
  expect(await page.locator('link[rel~="icon"]').count()).toBeGreaterThan(0);
  const image = new URL(await page.locator('meta[property="og:image"]').getAttribute('content'));
  expect(image.origin).toBe(new URL(CANONICAL).origin);
  expect(image.pathname.startsWith(`${PUBLIC_PATH}assets/`)).toBe(true);
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
    const bytes = /^(text\/|application\/javascript|image\/svg\+xml)/.test(type)
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
  assets.push(`${socialImage.pathname}${socialImage.search}`);
  for (const asset of new Set(assets)) {
    const url = new URL(asset, baseURL);
    expect(url.origin, asset).toBe(origin);
    expect(url.pathname.startsWith(`${PUBLIC_PATH}assets/`), asset).toBe(true);
    const response = await request.get(url.href);
    expect(response.status(), asset).toBe(200);
    expect(response.headers()['content-type'], asset).toBe(MIME_TYPES[extname(url.pathname).toLowerCase()]);
  }
});

test('passes axe WCAG 2.2 AA checks for every platform state', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('./');
  await page.locator('details').evaluateAll(elements => {
    elements.forEach(element => { element.open = true; });
  });
  for (const { label } of PLATFORMS) {
    await page.getByRole('radio', { name: label, exact: true }).check();
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
      .analyze();
    expect(results.violations, `${label} accessibility violations`).toEqual([]);
  }
});

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

test('offers a visible keyboard skip link and a meaningful accessible page structure', async ({ page }) => {
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
  expect(structure).toContain('navigation "Main navigation"');
  expect(structure).toContain('main:');
  await expect(page.getByRole('group', { name: 'Choose your operating system' })).toHaveCount(1);
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
  }
});
