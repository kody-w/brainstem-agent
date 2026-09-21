import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, rm, symlink, writeFile } from 'node:fs/promises';
import { request } from 'node:http';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { createSiteServer } from './server.mjs';

const root = fileURLToPath(new URL(`.server-fixture-${randomUUID()}/`, import.meta.url));
const index = '<!doctype html><title>Server fixture</title>';
let server;
let port;

before(async () => {
  await mkdir(join(root, 'assets', 'node_modules'), { recursive: true });
  await Promise.all([
    writeFile(join(root, 'index.html'), index),
    writeFile(join(root, 'private.js'), 'not public'),
    writeFile(join(root, 'assets', 'site.js'), 'export const fixture = true;\n'),
    writeFile(join(root, 'assets', 'site.css'), 'body { color: black; }\n'),
    writeFile(join(root, 'assets', 'mark.svg'), '<svg xmlns="http://www.w3.org/2000/svg"/>'),
    writeFile(join(root, 'assets', 'image.png'), Buffer.from([137, 80, 78, 71])),
    writeFile(join(root, 'assets', 'font.woff2'), Buffer.from('wOF2')),
    writeFile(join(root, 'assets', 'OFL.txt'), 'License fixture\n'),
    writeFile(join(root, 'assets', '.private.css'), 'not public'),
    writeFile(join(root, 'assets', 'source.map'), 'not public'),
    writeFile(join(root, 'assets', 'node_modules', 'dependency.js'), 'not public'),
  ]);
  await symlink('../private.js', join(root, 'assets', 'leak.js'));
  await symlink('site.js', join(root, 'assets', 'alias.js'));
  server = createSiteServer({ root });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  port = server.address().port;
});

after(async () => {
  if (server?.listening) {
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
  await rm(root, { recursive: true, force: true });
});

function fetchRaw(path, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port, path, method }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({
        status: response.statusCode,
        headers: response.headers,
        body: Buffer.concat(chunks).toString(),
      }));
      response.on('error', reject);
    });
    req.on('error', reject);
    req.end();
  });
}

test('serves the document only beneath the project prefix', async () => {
  for (const path of ['/brainstem-agent/', '/brainstem-agent/index.html?v=1']) {
    const response = await fetchRaw(path);
    assert.equal(response.status, 200);
    assert.equal(response.body, index);
    assert.equal(response.headers['content-type'], 'text/html; charset=utf-8');
    assert.equal(response.headers['x-content-type-options'], 'nosniff');
    assert.equal(response.headers['cache-control'], 'no-store');
  }
  for (const path of ['/', '/index.html', '/assets/site.js', '/brainstem-agent-other/']) {
    assert.equal((await fetchRaw(path)).status, 404, path);
  }
});

test('redirects the bare project prefix to its trailing slash', async () => {
  const response = await fetchRaw('/brainstem-agent');
  assert.equal(response.status, 308);
  assert.equal(response.headers.location, '/brainstem-agent/');
});

test('serves explicit asset MIME types', async () => {
  for (const [filename, type] of Object.entries({
    'site.js': 'application/javascript; charset=utf-8',
    'site.css': 'text/css; charset=utf-8',
    'mark.svg': 'image/svg+xml',
    'image.png': 'image/png',
    'font.woff2': 'font/woff2',
    'OFL.txt': 'text/plain; charset=utf-8',
  })) {
    const response = await fetchRaw(`/brainstem-agent/assets/${filename}`);
    assert.equal(response.status, 200, filename);
    assert.equal(response.headers['content-type'], type, filename);
  }
});

test('HEAD preserves headers without returning a body', async () => {
  const response = await fetchRaw('/brainstem-agent/', 'HEAD');
  assert.equal(response.status, 200);
  assert.equal(response.body, '');
  assert.equal(Number(response.headers['content-length']), Buffer.byteLength(index));
  const missing = await fetchRaw('/brainstem-agent/missing', 'HEAD');
  assert.equal(missing.status, 404);
  assert.equal(missing.body, '');
});

test('rejects write methods', async () => {
  const response = await fetchRaw('/brainstem-agent/', 'POST');
  assert.equal(response.status, 405);
  assert.equal(response.headers.allow, 'GET, HEAD');
});

test('rejects raw and encoded traversal, backslashes, and malformed paths', async () => {
  for (const path of [
    '/brainstem-agent/assets/../private.js',
    '/brainstem-agent/assets/%2e%2e/private.js',
    '/brainstem-agent/assets/%2E%2E%2Fprivate.js',
    '/brainstem-agent/assets/%5cprivate.js',
    '/brainstem-agent/assets/./site.js',
    '/brainstem-agent/assets/%00site.js',
    '/brainstem-agent/assets/%',
  ]) {
    assert.equal((await fetchRaw(path)).status, 400, path);
  }
});

test('never exposes private files, dependencies, directories, or unknown asset types', async () => {
  for (const path of [
    'private.js', 'package.json', 'README.md', 'tests/server.mjs', '.git/config',
    'assets/', 'assets/.private.css', 'assets/node_modules/dependency.js',
    'assets/source.map', 'assets/missing.css',
  ]) {
    assert.equal((await fetchRaw(`/brainstem-agent/${path}`)).status, 404, path);
  }
});

test('does not follow asset symlinks, even to another public file', async () => {
  for (const filename of ['leak.js', 'alias.js']) {
    assert.equal((await fetchRaw(`/brainstem-agent/assets/${filename}`)).status, 404, filename);
  }
});
