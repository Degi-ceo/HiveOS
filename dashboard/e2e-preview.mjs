#!/usr/bin/env node
import { chromium } from 'playwright';
import { screens } from './src/ui-preview/screenCatalog.js';
import { assertLayout, assertScreen, observePage, startPreviewServer } from './preview-test-helpers.mjs';

async function main() {
  const server = await startPreviewServer();
  const browser = await chromium.launch({ headless: true });
  const failures = [];
  let passed = 0;

  try {
    for (const [screenId, screen] of Object.entries(screens)) {
      const viewport = screen.presentation === 'mobile'
        ? { width: 390, height: 844 }
        : { width: 1440, height: 900 };
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const observer = observePage(page);

      try {
        await page.goto(`${server.baseUrl}/?ui-preview=1&screen=${screenId}`, {
          waitUntil: 'networkidle',
          timeout: 15_000,
        });
        await assertScreen(page, screenId, screen.title);
        await assertLayout(page, screenId);
        observer.assertClean(screenId);
        passed += 1;
        process.stdout.write(`  ✓ ${screenId}\n`);
      } catch (error) {
        failures.push({ screenId, message: error.message });
        process.stdout.write(`  ✗ ${screenId}: ${error.message}\n`);
      } finally {
        await context.close();
      }
    }
  } finally {
    await browser.close();
    await server.close();
  }

  console.log(`\nPreview smoke: ${passed}/${Object.keys(screens).length} screens passed`);
  if (failures.length) {
    throw new Error(failures.map(({ screenId, message }) => `${screenId}: ${message}`).join('\n'));
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
