import { createServer } from 'node:http';
import { readFile, realpath, stat } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const PUBLIC_PATH = '/brainstem-agent/';
export const MIME_TYPES = Object.freeze({
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
  '.txt': 'text/plain; charset=utf-8',
});

export function createSiteServer({ root = fileURLToPath(new URL('../', import.meta.url)) } = {}) {
  return createServer(async (request, response) => {
    function reply(status, body, headers = {}) {
      response.writeHead(status, {
        'Content-Type': 'text/plain; charset=utf-8',
        'Content-Length': Buffer.byteLength(body),
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        ...headers,
      });
      response.end(request.method === 'HEAD' ? undefined : body);
    }

    if (!['GET', 'HEAD'].includes(request.method)) {
      return reply(405, 'Method not allowed\n', { Allow: 'GET, HEAD' });
    }

    let pathname;
    try {
      // Inspect the raw path before URL normalization can erase traversal segments.
      pathname = decodeURIComponent((request.url || '/').split('?')[0]);
    } catch {
      return reply(400, 'Bad request\n');
    }

    if (/[\u0000-\u001f\u007f\\]/u.test(pathname)
        || pathname.split('/').some(part => part === '.' || part === '..')) {
      return reply(400, 'Bad request\n');
    }
    if (pathname === PUBLIC_PATH.slice(0, -1)) {
      return reply(308, 'Redirecting\n', { Location: PUBLIC_PATH });
    }
    if (!pathname.startsWith(PUBLIC_PATH)) {
      return reply(404, 'Not found\n');
    }

    const filename = pathname.slice(PUBLIC_PATH.length) || 'index.html';
    const parts = filename.split('/');
    const type = MIME_TYPES[extname(filename).toLowerCase()];
    if ((filename !== 'index.html' && !filename.startsWith('assets/'))
        || parts.some(part => !part || part.startsWith('.') || part === 'node_modules')
        || !type) {
      return reply(404, 'Not found\n');
    }

    try {
      const directory = await realpath(root);
      const candidate = resolve(directory, filename);
      if (!candidate.startsWith(`${directory}${sep}`) || await realpath(candidate) !== candidate) {
        return reply(404, 'Not found\n');
      }
      if (!(await stat(candidate)).isFile()) {
        return reply(404, 'Not found\n');
      }
      return reply(200, await readFile(candidate), { 'Content-Type': type });
    } catch (error) {
      const missing = ['ENOENT', 'ENOTDIR', 'EACCES', 'ELOOP'].includes(error.code);
      return reply(missing ? 404 : 500, missing ? 'Not found\n' : 'Server error\n');
    }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 4173);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('PORT must be an integer between 1 and 65535.');
  }
  const server = createSiteServer();
  server.on('error', error => {
    console.error(error.message);
    process.exitCode = 1;
  });
  server.listen(port, '127.0.0.1', () => {
    console.log(`Serving http://127.0.0.1:${port}${PUBLIC_PATH}`);
  });
}
