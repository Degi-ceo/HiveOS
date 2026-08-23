/**
 * Screenshot capture script for HiveOS UI Preview.
 * Uses playwright-core from node_modules.
 */
import pkg from './node_modules/playwright-core/index.js';
const { chromium } = pkg;
import { createServer } from 'http';
import { readFileSync, existsSync, statSync } from 'fs';
import { join, extname } from 'path';
import { fileURLToPath } from 'url';
import { dirname } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = __dirname;
const DIST = join(ROOT, 'dist');
const OUT = join(ROOT, 'screenshots-output');

const MIME = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

function serveFile(filePath) {
  if (!existsSync(filePath) || statSync(filePath).isDirectory()) {
    return null;
  }
  const ext = extname(filePath);
  const contentType = MIME[ext] || 'application/octet-stream';
  return { contentType, data: readFileSync(filePath) };
}

function startServer(port) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      const urlPath = req.url.split('?')[0];
      const normalized = urlPath === '/' ? '/index.html' : urlPath;
      const filePath = join(DIST, normalized);
      const result = serveFile(filePath);

      if (result) {
        res.writeHead(200, { 'Content-Type': result.contentType });
        res.end(result.data);
        return;
      }

      // SPA fallback: always serve index.html
      const fallback = join(DIST, 'index.html');
      const fallbackResult = serveFile(fallback);
      if (fallbackResult) {
        res.writeHead(200, { 'Content-Type': fallbackResult.contentType });
        res.end(fallbackResult.data);
        return;
      }

      res.writeHead(404);
      res.end('Not found');
    });
    server.listen(port, () => resolve(server));
  });
}

const VIEWPORTS = [
  { name: '1440p', width: 1440, height: 900 },
  { name: '1280p', width: 1280, height: 800 },
  { name: '1024p', width: 1024, height: 768 },
  { name: '768p',  width: 768,  height: 600 },
  { name: '390p',  width: 390,  height: 844 },
];

const SCREENS = [
  'hub', 'chat', 'memory', 'skills', 'files', 'agents', 'tasks', 'channels',
  'mcp', 'logs', 'activity', 'sessions', 'approvals', 'self-improve',
  'analytics', 'docs', 'settings',
  'agent-detail', 'command-palette', 'approval-modal', 'trace-detail',
  'new-task', 'notifications', 'release-log',
  'cron', 'commitments', 'mobile-hub', 'mobile-chat', 'mobile-nav',
];

async function main() {
  const { mkdirSync } = await import('fs');
  mkdirSync(OUT, { recursive: true });

  const PORT = 4747;
  const server = await startServer(PORT);
  console.log(`Server running on http://localhost:${PORT}`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });

  const results = [];

  for (const screenId of SCREENS) {
    const page = await context.newPage();
    const url = `http://localhost:${PORT}/?ui-preview=1&screen=${screenId}`;
    const errors = [];
    const onConsole = (msg) => { if (msg.type() === 'error') errors.push(msg.text()); };
    const onPageError = (err) => errors.push(err.message);
    page.on('console', onConsole);
    page.on('pageerror', onPageError);

    console.log(`  Capturing ${screenId}...`);

    for (const vp of VIEWPORTS) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
      await page.waitForTimeout(600);

      const filename = `${vp.name}_${screenId}.png`;
      const filepath = join(OUT, filename);
      await page.screenshot({ path: filepath, timeout: 10000 });
      results.push({ viewport: vp.name, screen: screenId, filename, errors: [...errors] });
      errors.length = 0;
    }

    page.off('console', onConsole);
    page.off('pageerror', onPageError);
    await page.close();
  }

  await browser.close();
  server.close();

  const { writeFileSync } = await import('fs');
  const manifest = results.map((r) => ({
    viewport: r.viewport,
    screen: r.screen,
    filename: r.filename,
    status: r.errors.length === 0 ? 'OK' : `ERRORS: ${r.errors.join('; ')}`,
  }));
  writeFileSync(join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 2));

  const errored = manifest.filter((r) => r.status !== 'OK');
  console.log(`\nDone. ${results.length} screenshots in ${OUT}`);
  console.log(`Errors: ${errored.length}`);
  if (errored.length > 0) {
    errored.slice(0, 10).forEach((e) => console.log(`  [${e.viewport}] ${e.screen}: ${e.status}`));
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
