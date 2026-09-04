import { createServer } from 'node:http';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { dirname, extname, isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const DASHBOARD_DIR = dirname(fileURLToPath(import.meta.url));
export const DIST_DIR = resolve(DASHBOARD_DIR, 'dist');

const MIME_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
};

function isInside(root, candidate) {
  const path = relative(root, candidate);
  return path !== '' && !path.startsWith(`..${sep}`) && path !== '..' && !isAbsolute(path);
}

function resolveRequestPath(rawUrl) {
  let decoded;
  try {
    decoded = decodeURIComponent(new URL(rawUrl, 'http://127.0.0.1').pathname);
  } catch {
    return null;
  }
  if (decoded.includes('\0')) return null;
  const requestPath = decoded === '/' ? 'index.html' : decoded.replace(/^\/+/, '');
  const candidate = resolve(DIST_DIR, requestPath);
  return isInside(DIST_DIR, candidate) ? candidate : null;
}

function sendFile(res, filePath) {
  res.writeHead(200, {
    'Cache-Control': 'no-store',
    'Content-Type': MIME_TYPES[extname(filePath)] || 'application/octet-stream',
  });
  createReadStream(filePath).pipe(res);
}

export function startPreviewServer() {
  if (!existsSync(resolve(DIST_DIR, 'index.html'))) {
    throw new Error('dashboard/dist is missing. Run `npm run build` before browser verification.');
  }

  return new Promise((resolveServer, rejectServer) => {
    const server = createServer((req, res) => {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405);
        res.end('Method Not Allowed');
        return;
      }

      const filePath = resolveRequestPath(req.url || '/');
      if (!filePath) {
        res.writeHead(400);
        res.end('Bad Request');
        return;
      }

      const target = existsSync(filePath) && statSync(filePath).isFile()
        ? filePath
        : resolve(DIST_DIR, 'index.html');
      sendFile(res, target);
    });

    server.once('error', rejectServer);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', rejectServer);
      const address = server.address();
      resolveServer({
        baseUrl: `http://127.0.0.1:${address.port}`,
        close: () => new Promise((resolveClose, rejectClose) => {
          server.close((error) => (error ? rejectClose(error) : resolveClose()));
        }),
      });
    });
  });
}

export function observePage(page) {
  const consoleErrors = [];
  const backendRequests = [];
  const pageErrors = [];

  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('request', (request) => {
    if (['fetch', 'xhr', 'websocket'].includes(request.resourceType())) {
      backendRequests.push(`${request.resourceType()}: ${request.url()}`);
    }
  });

  return {
    assertClean(label) {
      const failures = [
        ...consoleErrors.map((error) => `console: ${error}`),
        ...pageErrors.map((error) => `page: ${error}`),
        ...backendRequests.map((error) => `network: ${error}`),
      ];
      if (failures.length) throw new Error(`${label}: ${failures.join(' | ')}`);
    },
  };
}

export async function assertLayout(page, label) {
  const problems = await page.evaluate(() => {
    const issues = [];
    const root = document.documentElement;
    if (root.scrollWidth > root.clientWidth + 1) {
      issues.push(`document overflow ${root.scrollWidth}px > ${root.clientWidth}px`);
    }

    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const selectors = [
      '.ui-preview__header',
      '.ui-preview__workspace',
      '.hub__primary-row',
      '.memory-view__workspace',
      '.tasks-view__kanban',
      '.approvals-view__list',
    ];
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        const style = getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const rect = element.getBoundingClientRect();
        if (rect.width > 0 && (rect.left < -1 || rect.right > viewportWidth + 1)) {
          issues.push(`${selector} outside horizontal viewport (${Math.round(rect.left)}..${Math.round(rect.right)} / ${viewportWidth})`);
        }
        if (rect.width === 0 || rect.height === 0) issues.push(`${selector} has zero-size layout`);
        if (!Number.isFinite(rect.left + rect.top + rect.width + rect.height)) issues.push(`${selector} has invalid bounds`);
      }
    }

    const nav = document.querySelector('.ui-preview__mobile-nav');
    if (viewportWidth <= 640 && nav) {
      const rect = nav.getBoundingClientRect();
      if (rect.bottom > viewportHeight + 1 || rect.top < 0) issues.push('mobile navigation is outside the viewport');
    }
    return issues;
  });
  if (problems.length) throw new Error(`${label}: ${problems.join(' | ')}`);
}

export async function assertScreen(page, screenId, title) {
  await page.getByRole('heading', { level: 1, name: title, exact: true }).waitFor({ state: 'visible' });
  const current = await page.getByTestId('ui-preview').getAttribute('data-screen');
  if (current !== screenId) throw new Error(`Expected screen ${screenId}, received ${current}`);
}
