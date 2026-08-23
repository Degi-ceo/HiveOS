#!/usr/bin/env node
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { screens } from './src/ui-preview/screenCatalog.js';
import {
  DASHBOARD_DIR,
  assertLayout,
  assertScreen,
  observePage,
  startPreviewServer,
} from './preview-test-helpers.mjs';

const VERSION = '0.8.5';
const OUTPUT_DIR = resolve(DASHBOARD_DIR, 'screenshots-output');
const ZIP_PATH = resolve(DASHBOARD_DIR, '..', `HiveOS_UI_v${VERSION}.zip`);
const EXPECTED_SCREENS = 29;

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

function defaultTab(screen) {
  return screen.defaultTab || screen.tabs[0] || null;
}

function captureCases() {
  const cases = [];
  for (const [screenId, screen] of Object.entries(screens)) {
    cases.push({ sourceScreen: screenId, sourceTab: defaultTab(screen), targetScreen: screenId, targetTab: defaultTab(screen), isDefault: true });
    for (const tab of screen.tabs) {
      if (tab === defaultTab(screen)) continue;
      const targetScreen = screen.tabTargets[tab] || screenId;
      const target = screens[targetScreen];
      cases.push({
        sourceScreen: screenId,
        sourceTab: tab,
        targetScreen,
        targetTab: targetScreen === screenId ? tab : defaultTab(target),
        isDefault: false,
      });
    }
  }
  return cases;
}

function caseUrl(baseUrl, item) {
  const params = new URLSearchParams({ 'ui-preview': '1', screen: item.targetScreen });
  if (item.targetTab) params.set('tab', slugify(item.targetTab));
  return `${baseUrl}/?${params}`;
}

function filenameFor(item) {
  const parts = [String(Object.keys(screens).indexOf(item.sourceScreen) + 1).padStart(2, '0'), item.sourceScreen];
  if (!item.isDefault) parts.push(slugify(item.sourceTab));
  return `${parts.join('--')}.png`;
}

function viewportFor(item) {
  const mobile = screens[item.targetScreen].presentation === 'mobile';
  return mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 };
}

function cleanOutput() {
  if (!OUTPUT_DIR.endsWith('/dashboard/screenshots-output')) {
    throw new Error(`Refusing to clean unexpected output path: ${OUTPUT_DIR}`);
  }
  rmSync(OUTPUT_DIR, { force: true, recursive: true });
  mkdirSync(OUTPUT_DIR, { recursive: true });
  rmSync(ZIP_PATH, { force: true });
}

async function main() {
  if (Object.keys(screens).length !== EXPECTED_SCREENS) {
    throw new Error(`Expected ${EXPECTED_SCREENS} screens, found ${Object.keys(screens).length}`);
  }
  cleanOutput();

  const cases = captureCases();
  const server = await startPreviewServer();
  const browser = await chromium.launch({ headless: true });
  const manifest = {
    version: VERSION,
    generatedAt: new Date().toISOString(),
    deviceScaleFactor: 2,
    defaultScreens: Object.keys(screens).length,
    additionalSubviewCaptures: cases.filter((item) => !item.isDefault).length,
    captures: [],
  };
  const errors = [];
  const hashes = new Map();

  try {
    for (const item of cases) {
      const viewport = viewportFor(item);
      const context = await browser.newContext({ deviceScaleFactor: 2, viewport });
      const page = await context.newPage();
      const observer = observePage(page);
      const filename = filenameFor(item);
      const filePath = resolve(OUTPUT_DIR, filename);
      const url = caseUrl(server.baseUrl, item);

      try {
        await page.goto(url, { waitUntil: 'networkidle', timeout: 15_000 });
        await assertScreen(page, item.targetScreen, screens[item.targetScreen].title);
        if (item.targetTab) {
          const tab = page.getByRole('tab', { name: item.targetTab, exact: true });
          await tab.waitFor({ state: 'visible' });
          if (await tab.getAttribute('aria-selected') !== 'true') {
            throw new Error(`Tab ${item.targetTab} is not selected`);
          }
        }
        await assertLayout(page, `${item.sourceScreen}/${item.sourceTab || 'default'}`);
        observer.assertClean(`${item.sourceScreen}/${item.sourceTab || 'default'}`);
        await page.screenshot({ path: filePath, fullPage: false });

        const size = statSync(filePath).size;
        if (size < 10_000) throw new Error(`Suspiciously small screenshot: ${size} bytes`);
        const hash = createHash('sha256').update(readFileSync(filePath)).digest('hex');
        const duplicate = hashes.get(hash) || null;
        hashes.set(hash, filename);
        manifest.captures.push({
          sourceScreen: item.sourceScreen,
          sourceTab: item.sourceTab,
          targetScreen: item.targetScreen,
          targetTab: item.targetTab,
          route: new URL(url).pathname + new URL(url).search,
          filename,
          cssViewport: `${viewport.width}x${viewport.height}`,
          outputPixels: `${viewport.width * 2}x${viewport.height * 2}`,
          bytes: size,
          sha256: hash,
          duplicateOf: duplicate,
          status: 'pass',
        });
        process.stdout.write(`  ✓ ${filename}\n`);
      } catch (error) {
        errors.push(`${filename}: ${error.message}`);
        manifest.captures.push({
          sourceScreen: item.sourceScreen,
          sourceTab: item.sourceTab,
          targetScreen: item.targetScreen,
          targetTab: item.targetTab,
          route: new URL(url).pathname + new URL(url).search,
          filename,
          cssViewport: `${viewport.width}x${viewport.height}`,
          status: 'fail',
          error: error.message,
        });
        process.stdout.write(`  ✗ ${filename}: ${error.message}\n`);
      } finally {
        await context.close();
      }
    }
  } finally {
    await browser.close();
    await server.close();
  }

  const defaultIds = new Set(manifest.captures.filter((item) => item.status === 'pass' && item.sourceTab === defaultTab(screens[item.sourceScreen])).map((item) => item.sourceScreen));
  if (defaultIds.size !== EXPECTED_SCREENS) errors.push(`Only ${defaultIds.size}/${EXPECTED_SCREENS} default screens were captured`);
  if (manifest.captures.length !== cases.length) errors.push(`Expected ${cases.length} capture records, found ${manifest.captures.length}`);

  writeFileSync(resolve(OUTPUT_DIR, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  if (errors.length) throw new Error(errors.join('\n'));

  execFileSync('zip', ['-q', '-r', ZIP_PATH, '.'], { cwd: OUTPUT_DIR });
  const zipSize = statSync(ZIP_PATH).size;
  if (zipSize < 100_000) throw new Error(`Screenshot ZIP is unexpectedly small: ${zipSize} bytes`);

  console.log(`\nCaptured ${manifest.captures.length} verified views (${EXPECTED_SCREENS} defaults + ${manifest.additionalSubviewCaptures} additional subviews).`);
  console.log(`ZIP: ${ZIP_PATH} (${(zipSize / 1024 / 1024).toFixed(2)} MB)`);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
