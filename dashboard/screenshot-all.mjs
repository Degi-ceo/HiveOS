#!/usr/bin/env node
/**
 * HiveOS UI Preview — Screenshot Generator
 * Captures all screen + tab combinations at HiDPI (2880x1800 = 1440x900 @2x)
 * Output: screenshots-output/ + HiveOS_UI_v0.8.4.zip
 */

import { createServer } from 'http';
import { readFileSync, writeFileSync, mkdirSync, existsSync, statSync, createReadStream } from 'fs';
import { execFileSync } from 'child_process';
import { join as pathJoin, resolve, sep } from 'path';

// ── Paths ──────────────────────────────────────────────────────────────────────
const WORKDIR   = '/home/hive/hiveos/.claude/worktrees/gpt-ui-improvements/dashboard';
const DIST_DIR   = pathJoin(WORKDIR, 'dist');
const OUT_DIR    = pathJoin(WORKDIR, 'screenshots-output');
const ZIP_PATH   = '/home/hive/hiveos/.claude/worktrees/gpt-ui-improvements/HiveOS_UI_v0.8.4.zip';
const SCREEN_CAT = pathJoin(WORKDIR, 'src/ui-preview/screenCatalog.js');

// ── Playwright-core import ─────────────────────────────────────────────────────
// playwright-core exports chromium at .default.chromium in ESM
const pwModule = await import(pathJoin(WORKDIR, 'node_modules/playwright-core/index.js'));
const { chromium } = pwModule.default;

// ── Constants ─────────────────────────────────────────────────────────────────
const VERSION   = '0.8.4';
const VIEW_W    = 2880;   // HiDPI: 1440 x 2
const VIEW_H     = 1800;   // HiDPI: 900  x 2
const VIEWPORT   = `${VIEW_W}x${VIEW_H} @2x`;
const BASE_PORT  = 0;      // dynamic port allocation

// ── Screen catalog (tab slugs → display labels) ───────────────────────────────
const TAB_SLUGS = {
  // tab display label → URL slug
  'Conversation': 'conversation',
  'Run details':  'run-details',
  'All':          'all',
  'Important':    'important',
  'Topics':       'topics',
  'Sessions':     'sessions',
  'Pinned':       'pinned',
  'Active':       'active',
  'Stale':        'stale',
  'Archived':     'archived',
  'Workspace':     'workspace',
  'Recent':       'recent',
  'Shared':       'shared',
  'Active now':   'active-now',
  'All agents':   'all-agents',
  'By type':      'by-type',
  'Kanban':       'kanban',
  'Cron':         'cron',
  'Promises':     'promises',
  'Telegram':     'telegram',
  'Discord':      'discord',
  'Slack':        'slack',
  'Email':        'email',
  'Webhooks':     'webhooks',
  'Servers':      'servers',
  'Tools':        'tools',
  'Health':       'health',
  'Gateway':      'gateway',
  'Agents':       'agents',
  'System':       'system',
  'Self-improve': 'self-improve',
  'Live':         'live',
  'Audit':        'audit',
  'Traces':       'traces',
  'Events':       'events',
  'Loop-guard':   'loop-guard',
  'By model':     'by-model',
  'By date':      'by-date',
  'Errors':       'errors',
  'Pending':      'pending',
  'Edits log':    'edits-log',
  'Verdicts':     'verdicts',
  'History':      'history',
  'Pending edits':'pending-edits',
  'Run tests':    'run-tests',
  'Learning':     'learning',
  'Cost':         'cost',
  'Tokens':       'tokens',
  'Skill usage':  'skill-usage',
  'Personal':     'personal',
  'Account':      'account',
  'Overview':     'overview',
  'Performance':  'performance',
  'Logs':         'logs',
  'Latest':       'latest',
  'All versions': 'all-versions',
};

// Slugify a tab label for URL
function tabSlug(label) {
  return TAB_SLUGS[label] ?? label.toLowerCase().replace(/\s+/g, '-');
}

// Build URL for a screen + optional tab
function buildUrl(screenId, tabLabel) {
  const params = new URLSearchParams({ 'ui-preview': '1', screen: screenId });
  if (tabLabel) params.set('tab', tabSlug(tabLabel));
  return `/?${params.toString()}`;
}

// Filename for a screen + tab combo
function screenshotFilename(screenId, tabLabel) {
  const base = '1440p';
  const parts = [base, screenId];
  if (tabLabel) parts.push(tabSlug(tabLabel));
  return parts.join('_') + '.png';
}

// ── HTTP Server ───────────────────────────────────────────────────────────────
function startServer(port) {
  return new Promise((promiseResolve, promiseReject) => {
    const server = createServer((req, res) => {
      // ── CORS headers ──────────────────────────────────────────────────────
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

      if (req.method === 'OPTIONS') {
        res.writeHead(204);
        res.end();
        return;
      }

      if (req.method !== 'GET') {
        res.writeHead(405);
        res.end('Method Not Allowed');
        return;
      }

      // ── Path traversal protection ────────────────────────────────────────
      const urlPath = req.url.split('?')[0];
      const decodedPath = decodeURIComponent(urlPath);
      // Block paths with null bytes or traversals
      if (
        decodedPath.includes('\0') ||
        decodedPath.includes('..') ||
        decodedPath.includes('%00') ||
        decodedPath.includes('%2e%2e')
      ) {
        res.writeHead(400);
        res.end('Bad Request');
        return;
      }

      // Normalize and resolve
      const requestedPath = decodedPath === '/' ? '/index.html' : decodedPath;
      const pathToResolve = requestedPath.startsWith('/') ? requestedPath.slice(1) : requestedPath;
      const filePath = resolve(DIST_DIR, pathToResolve);

      // Ensure resolved path is within DIST_DIR
      if (!filePath.startsWith(DIST_DIR + sep)) {
        res.writeHead(403);
        res.end('Forbidden');
        return;
      }

      // ── Serve file or SPA fallback ──────────────────────────────────────
      try {
        if (existsSync(filePath) && statSync(filePath).isFile()) {
          const ext = filePath.split('.').pop();
          const mimeTypes = {
            html: 'text/html',
            js:   'application/javascript',
            css:  'text/css',
            png:  'image/png',
            jpg:  'image/jpeg',
            jpeg: 'image/jpeg',
            svg:  'image/svg+xml',
            ico:  'image/x-icon',
            json: 'application/json',
            txt:  'text/plain',
          };
          res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
          createReadStream(filePath).pipe(res);
        } else {
          // SPA fallback
          const indexPath = pathJoin(DIST_DIR, 'index.html');
          if (existsSync(indexPath)) {
            res.writeHead(200, { 'Content-Type': 'text/html' });
            createReadStream(indexPath).pipe(res);
          } else {
            res.writeHead(404);
            res.end('Not Found');
          }
        }
      } catch (err) {
        console.error('[server] error:', err.message);
        res.writeHead(500);
        res.end('Internal Server Error');
      }
    });

    server.on('error', (err) => {
      if (err.code === 'EADDRINUSE' && port < 9000) {
        // Try next port
        server.listen(0, '127.0.0.1');
      } else {
        promiseReject(err);
      }
    });

    server.listen(port, '127.0.0.1', () => {
      const addr = server.address();
      promiseResolve({ server, port: addr.port });
    });
  });
}

// ── Screen catalog parser ────────────────────────────────────────────────────
function loadScreens() {
  // Read the screenCatalog as a module — we parse it manually
  const content = readFileSync(SCREEN_CAT, 'utf8');

  // Extract the screens object using regex (avoids import complexity)
  const screens = {};

  // Match each screen entry:  key: page({ ... })
  const screenRegex = /^\s{2}(\w+):\s*page\(\{([^}]+(?:\{[^}]*\}[^}]*)*)\}\)/gm;
  let match;
  while ((match = screenRegex.exec(content)) !== null) {
    const key    = match[1];
    const body   = match[2];

    // Extract tabs array
    const tabsMatch = body.match(/tabs:\s*\[([^\]]*)\]/);
    const tabs = tabsMatch
      ? tabsMatch[1].split(',').map(t => t.trim().replace(/['"]/g, '')).filter(Boolean)
      : [];

    screens[key] = { key, tabs };
  }

  return screens;
}

// ── Screenshot capture ────────────────────────────────────────────────────────
async function captureScreenshots(baseUrl, screens) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport:   { width: VIEW_W, height: VIEW_H },
    deviceScaleFactor: 1,  // viewport size already accounts for 2x
  });

  const errors = [];   // collected console errors → exit code 1
  const manifest = {
    version:   VERSION,
    generated: new Date().toISOString(),
    viewport:  VIEWPORT,
    screens:   [],
  };

  for (const [screenId, screen] of Object.entries(screens)) {
    const screenErrors = [];

    // ── Default view (no tab) ────────────────────────────────────────────
    const url = `${baseUrl}${buildUrl(screenId, null)}`;
    const filename = screenshotFilename(screenId, null);
    const outPath  = pathJoin(OUT_DIR, filename);

    console.log(`  Capturing ${filename} ...`);
    const page = await context.newPage();

    // Collect console errors
    const pageErrors = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        pageErrors.push(msg.text());
      }
    });
    page.on('pageerror', err => {
      pageErrors.push(err.message);
    });

    try {
      await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
      // Small settle
      await page.waitForTimeout(800);
      await page.screenshot({ path: outPath, fullPage: false });
      screenErrors.push(...pageErrors);
    } catch (err) {
      console.error(`    ERROR capturing ${filename}: ${err.message}`);
      screenErrors.push(err.message);
    } finally {
      await page.close();
    }

    manifest.screens.push({
      screen:   screenId,
      tab:      null,
      route:    buildUrl(screenId, null),
      filename,
      status:   screenErrors.length ? 'fail' : 'pass',
      errors:   screenErrors,
    });

    // ── Tab variants ─────────────────────────────────────────────────────
    for (const tabLabel of screen.tabs) {
      const tabUrl     = `${baseUrl}${buildUrl(screenId, tabLabel)}`;
      const tabFilename = screenshotFilename(screenId, tabLabel);
      const tabOutPath  = pathJoin(OUT_DIR, tabFilename);
      const tabErrors   = [];

      console.log(`  Capturing ${tabFilename} ...`);
      const tabPage = await context.newPage();

      tabPage.on('console', msg => {
        if (msg.type() === 'error') tabErrors.push(msg.text());
      });
      tabPage.on('pageerror', err => {
        tabErrors.push(err.message);
      });

      try {
        await tabPage.goto(tabUrl, { waitUntil: 'networkidle', timeout: 15000 });
        await tabPage.waitForTimeout(800);
        await tabPage.screenshot({ path: tabOutPath, fullPage: false });
        screenErrors.push(...tabErrors);
      } catch (err) {
        console.error(`    ERROR capturing ${tabFilename}: ${err.message}`);
        screenErrors.push(err.message);
      } finally {
        await tabPage.close();
      }

      manifest.screens.push({
        screen:   screenId,
        tab:      tabLabel,
        route:    buildUrl(screenId, tabLabel),
        filename: tabFilename,
        status:   tabErrors.length ? 'fail' : 'pass',
        errors:   tabErrors,
      });
    }

    // Accumulate all errors for this screen
    if (screenErrors.length) errors.push(...screenErrors);
  }

  await browser.close();

  return { manifest, errors };
}

// ── ZIP creation ──────────────────────────────────────────────────────────────
function createZip() {
  console.log('\nCreating ZIP archive...');
  try {
    // Ensure output dir exists for manifest
    mkdirSync(OUT_DIR, { recursive: true });

    // Write manifest.json into the output dir (included in ZIP)
    const manifestPath = pathJoin(OUT_DIR, 'manifest.json');
    writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

    // Create ZIP using system zip command (inputs are controlled paths, safe to use execFile)
    execFileSync('zip', ['-r', ZIP_PATH, '.', '-x', '.*'], {
      cwd: OUT_DIR,
      stdio: 'pipe',
    });

    const stat = statSync(ZIP_PATH);
    const sizeMB = (stat.size / 1024 / 1024).toFixed(2);
    console.log(`  ZIP: ${ZIP_PATH} (${sizeMB} MB)`);
    return true;
  } catch (err) {
    console.error(`  ZIP error: ${err.message}`);
    return false;
  }
}

// ── Summary printer ──────────────────────────────────────────────────────────
function printSummary(manifest, errors) {
  const passCount = manifest.screens.filter(s => s.status === 'pass').length;
  const failCount = manifest.screens.filter(s => s.status === 'fail').length;
  const totalTabs = manifest.screens.filter(s => s.tab !== null).length;
  const totalScreens = new Set(manifest.screens.map(s => s.screen)).size;

  console.log('\n══════════════════════════════════════');
  console.log('  HiveOS UI Screenshot Summary');
  console.log('══════════════════════════════════════');
  console.log(`  Screens:     ${totalScreens}`);
  console.log(`  Tab views:   ${totalTabs}`);
  console.log(`  Total shots: ${manifest.screens.length}`);
  console.log(`  Passed:      ${passCount}`);
  console.log(`  Failed:      ${failCount}`);

  let zipSize = 'n/a';
  try {
    const stat = statSync(ZIP_PATH);
    zipSize = (stat.size / 1024 / 1024).toFixed(2) + ' MB';
  } catch {}
  console.log(`  ZIP size:   ${zipSize}`);
  console.log('══════════════════════════════════════\n');

  if (errors.length) {
    console.log('Console errors detected (sample):');
    const unique = [...new Set(errors.slice(0, 5))];
    unique.forEach(e => console.log('  -', e));
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────
let manifest;

async function main() {
  console.log('HiveOS UI Screenshot Generator v' + VERSION);
  console.log(`Viewport: ${VIEWPORT}`);
  console.log(`Output:   ${OUT_DIR}\n`);

  // Ensure output dir
  mkdirSync(OUT_DIR, { recursive: true });

  // Load screen catalog
  const screens = loadScreens();
  const screenCount = Object.keys(screens).length;
  console.log(`Loaded ${screenCount} screens from catalog`);

  // Start HTTP server on dynamic port
  console.log('Starting loopback server...');
  let port = 3333;
  let serverHandle;
  let attempts = 0;
  while (attempts < 20) {
    try {
      serverHandle = await startServer(port);
      break;
    } catch (err) {
      if (err.code === 'EADDRINUSE') {
        port++;
        attempts++;
        continue;
      }
      throw err;
    }
  }
  const baseUrl = `http://127.0.0.1:${serverHandle.port}`;
  console.log(`Server running at ${baseUrl}\n`);

  // Capture all screens
  console.log('Starting screenshot capture...\n');
  const { manifest: m, errors } = await captureScreenshots(baseUrl, screens);
  manifest = m;

  // Stop server
  serverHandle.server.close();

  // Create ZIP
  const zipOk = createZip();

  // Print summary
  printSummary(manifest, errors);

  if (errors.length) {
    console.error('EXIT 1 — console errors detected during capture');
    process.exit(1);
  }

  if (!zipOk) {
    console.error('EXIT 1 — ZIP creation failed');
    process.exit(1);
  }

  console.log('Done.');
}

main().catch(err => {
  console.error('Fatal:', err);
  process.exit(1);
});
