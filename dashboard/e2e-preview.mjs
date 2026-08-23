/**
 * Playwright e2e verification — every screen must render h1 and produce zero console errors.
 */
import pkg from './node_modules/playwright-core/index.js';
const { chromium } = pkg;

const SCREENS = [
  'hub', 'chat', 'memory', 'skills', 'files', 'agents', 'tasks', 'channels',
  'mcp', 'logs', 'activity', 'sessions', 'approvals', 'self-improve',
  'analytics', 'docs', 'settings',
  'agent-detail', 'command-palette', 'approval-modal', 'trace-detail',
  'new-task', 'notifications', 'release-log',
  'cron', 'commitments', 'mobile-hub', 'mobile-chat', 'mobile-nav',
];

const BASE = 'http://localhost:4754';

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });

  const failures = [];
  let passed = 0;

  for (const id of SCREENS) {
    const page = await context.newPage();
    const errors = [];
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
    page.on('pageerror', err => errors.push(err.message));

    await page.goto(`${BASE}/?ui-preview=1&screen=${id}`, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForTimeout(600);

    const h1 = await page.$('h1');
    const h1Text = h1 ? await h1.innerText() : '(missing)';

    if (!h1 || h1Text === '(missing)' || errors.length > 0) {
      failures.push({ screen: id, h1Text, errors: [...errors] });
    } else {
      passed++;
      process.stdout.write(`  ✓ ${id}\n`);
    }

    await page.close();
  }

  await browser.close();

  console.log(`\n=== Playwright e2e results ===`);
  console.log(`Passed: ${passed}/${SCREENS.length}`);
  if (failures.length > 0) {
    console.log(`FAILED:`);
    failures.forEach(f => {
      console.log(`  ✗ ${f.screen}`);
      console.log(`    h1: "${f.h1Text}"`);
      if (f.errors.length) console.log(`    errors: ${f.errors.join(' | ')}`);
    });
    process.exit(1);
  } else {
    console.log(`ALL ${passed} screens verified — zero errors`);
  }
}

main().catch(e => { console.error(e); process.exit(1); });
