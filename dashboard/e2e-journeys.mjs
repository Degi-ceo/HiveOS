#!/usr/bin/env node
import { chromium } from 'playwright';
import { screens } from './src/ui-preview/screenCatalog.js';
import { assertLayout, assertScreen, observePage, startPreviewServer } from './preview-test-helpers.mjs';

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'compact', width: 1280, height: 800 },
  { name: 'tablet-landscape', width: 1024, height: 768 },
  { name: 'tablet', width: 768, height: 600 },
  { name: 'mobile', width: 390, height: 844 },
];

const actionPattern = /^(open|review|inspect|view|manage|configure|preview|run)/i;
const EXPECTED_PRIMARY_ACTIONS = 17;
const EXPECTED_ROW_ACTIONS = 82;
let browserInteractions = 0;
let responsiveChecks = 0;

function invariant(condition, message) {
  if (!condition) throw new Error(message);
}

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

async function openScreen(page, baseUrl, screenId, tab = null) {
  const params = new URLSearchParams({ 'ui-preview': '1', screen: screenId });
  if (tab) params.set('tab', slugify(tab));
  await page.goto(`${baseUrl}/?${params}`, { waitUntil: 'networkidle', timeout: 15_000 });
  await assertScreen(page, screenId, screens[screenId].title);
}

async function click(locator) {
  await locator.click();
  browserInteractions += 1;
}

async function exposeRowControl(page, screen, index) {
  const rowView = screen.kind === 'chat' ? 'Run details'
    : screen.kind === 'skills' ? 'All'
      : screen.kind === 'agents' ? 'All agents'
        : screen.kind === 'channels' ? screen.rows[index][0]
          : screen.kind === 'settings' ? screen.rows[index][1]
          : null;
  if (rowView) await click(page.getByRole('tab', { name: rowView, exact: true }));
}

async function withPage(browser, viewport, run) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const observer = observePage(page);
  try {
    await run(page);
    observer.assertClean('journey');
  } finally {
    await context.close();
  }
}

async function main() {
  const server = await startPreviewServer();
  const browser = await chromium.launch({ headless: true });
  const results = [];

  async function test(name, run) {
    try {
      await run();
      results.push({ name, status: 'pass' });
      console.log(`  ✓ ${name}`);
    } catch (error) {
      results.push({ name, status: 'fail', error: error.message });
      console.log(`  ✗ ${name}: ${error.message}`);
    }
  }

  try {
    await test('desktop user can reach all 29 screens through navigation', () => withPage(browser, VIEWPORTS[0], async (page) => {
      await openScreen(page, server.baseUrl, 'hub');
      for (const [screenId, screen] of Object.entries(screens)) {
        const sidebar = page.getByRole('navigation', { name: 'UI preview screens' });
        await click(sidebar.getByRole('button', { name: screen.navLabel, exact: true }));
        await assertScreen(page, screenId, screen.title);
      }
    }));

    await test('all 70 tabs update selected state, content, URL or routed destination', () => withPage(browser, VIEWPORTS[0], async (page) => {
      let testedTabs = 0;
      for (const [screenId, screen] of Object.entries(screens)) {
        for (const tabName of screen.tabs) {
          await openScreen(page, server.baseUrl, screenId);
          const panel = page.getByRole('tabpanel');
          const before = await panel.innerText();
          const tab = page.getByRole('tab', { name: tabName, exact: true });
          await tab.waitFor({ state: 'visible' });
          await click(tab);
          const target = screen.tabTargets[tabName];
          if (target) {
            await assertScreen(page, target, screens[target].title);
          } else {
            invariant(await tab.getAttribute('aria-selected') === 'true', `${screenId}/${tabName}: aria-selected is not true`);
            invariant(new URL(page.url()).searchParams.get('tab') === slugify(tabName), `${screenId}/${tabName}: URL tab is incorrect`);
            invariant(await page.locator('main').getAttribute('data-active-tab') === tabName, `${screenId}/${tabName}: active view state is incorrect`);
            if (tabName !== (screen.defaultTab || screen.tabs[0])) {
              const after = await page.getByRole('tabpanel').innerText();
              invariant(after !== before, `${screenId}/${tabName}: visible panel content did not change`);
            }
          }
          testedTabs += 1;
        }
      }
      invariant(testedTabs === 70, `Expected 70 tabs, exercised ${testedTabs}`);
    }));

    await test('Hub supports real drill-down, overlays and history return', () => withPage(browser, VIEWPORTS[0], async (page) => {
      await openScreen(page, server.baseUrl, 'hub');
      await click(page.getByRole('button', { name: /Gateway Healthy/i }));
      await assertScreen(page, 'logs', 'Logs');
      await page.goBack();
      browserInteractions += 1;
      await assertScreen(page, 'hub', 'Hub');

      await click(page.getByRole('main').getByRole('button', { name: 'New task', exact: true }));
      await assertScreen(page, 'new-task', 'New task');
      await click(page.getByRole('button', { name: 'Close', exact: true }));
      await assertScreen(page, 'hub', 'Hub');

      await click(page.getByRole('button', { name: 'Open notifications', exact: true }));
      await assertScreen(page, 'notifications', 'Notifications');
      await page.keyboard.press('Escape');
      browserInteractions += 1;
      await assertScreen(page, 'hub', 'Hub');

      await page.keyboard.press('Control+k');
      browserInteractions += 1;
      await assertScreen(page, 'command-palette', 'Command palette');
      await page.keyboard.press('Escape');
      browserInteractions += 1;
      await assertScreen(page, 'hub', 'Hub');
    }));

    await test('Memory, Tasks and Approvals produce meaningful visible state changes', () => withPage(browser, VIEWPORTS[0], async (page) => {
      await openScreen(page, server.baseUrl, 'memory');
      await click(page.getByRole('tab', { name: 'Important', exact: true }));
      await page.getByRole('heading', { name: 'Important memories', exact: true }).waitFor();
      invariant(await page.locator('.memory-view__row').count() === 2, 'Important memory filter did not reduce the list');
      await click(page.locator('.memory-view__row').nth(1));
      await page.getByRole('heading', { name: 'Webhook retry failure analysis', exact: true }).waitFor();
      await click(page.getByRole('tab', { name: 'Topics', exact: true }));
      await page.getByText('Release and gateway knowledge', { exact: true }).waitFor();
      await click(page.getByRole('tab', { name: 'Sessions', exact: true }));
      await page.getByText('Session ses_8f912a', { exact: true }).waitFor();

      await openScreen(page, server.baseUrl, 'tasks');
      await click(page.locator('.tasks-view__card[data-row-index="1"]'));
      await page.getByText('Review UI contract', { exact: true }).last().waitFor();
      invariant(await page.locator('.tasks-view__card[data-row-index="1"]').getAttribute('aria-pressed') === 'true', 'Task selection was not exposed');
      await click(page.getByRole('tab', { name: 'Cron', exact: true }));
      await assertScreen(page, 'cron', 'Automations');
      await click(page.getByRole('tab', { name: 'Promises', exact: true }));
      await assertScreen(page, 'commitments', 'Commitments');

      await openScreen(page, server.baseUrl, 'approvals');
      await click(page.getByRole('tab', { name: 'Edits log', exact: true }));
      await page.getByRole('heading', { name: 'Decision history', exact: true }).waitFor();
      invariant(await page.locator('.approvals-view__rows button').count() === 0, 'Edits log incorrectly exposes review actions');
      await click(page.getByRole('tab', { name: 'Pending', exact: true }));
      await click(page.locator('.approvals-view__action').first());
      await assertScreen(page, 'approval-modal', 'Approval review');
    }));

    await test('all primary actions and actionable rows produce a visible outcome', () => withPage(browser, VIEWPORTS[0], async (page) => {
      let primaryActions = 0;
      let rowActions = 0;
      for (const [screenId, screen] of Object.entries(screens)) {
        if (screen.action) {
          await openScreen(page, server.baseUrl, screenId);
          const action = page.getByRole('main').getByRole('button', { name: screen.action, exact: true });
          await action.waitFor({ state: 'visible' });
          await click(action);
          if (screen.primaryTarget) await assertScreen(page, screen.primaryTarget, screens[screen.primaryTarget].title);
          else await page.getByRole('status').waitFor({ state: 'visible' });
          primaryActions += 1;
        }

        for (let index = 0; index < screen.rows.length; index += 1) {
          const row = screen.rows[index];
          if (!screen.rowTargets[index] && !actionPattern.test(row[3] || '')) continue;
          await openScreen(page, server.baseUrl, screenId);
          await exposeRowControl(page, screen, index);
          const control = page.getByRole('main').locator(`[data-row-index="${index}"]`).first();
          await control.waitFor({ state: 'visible' });
          await click(control);
          if (screen.rowTargets[index]) await assertScreen(page, screen.rowTargets[index], screens[screen.rowTargets[index]].title);
          else {
            const current = await page.getByTestId('ui-preview').getAttribute('data-screen');
            invariant(current === screenId, `${screenId} row ${index}: unexpected destination ${current}`);
          }
          rowActions += 1;
        }
      }
      invariant(primaryActions === EXPECTED_PRIMARY_ACTIONS, `Expected ${EXPECTED_PRIMARY_ACTIONS} primary actions, exercised ${primaryActions}`);
      invariant(rowActions === EXPECTED_ROW_ACTIONS, `Expected ${EXPECTED_ROW_ACTIONS} row actions, exercised ${rowActions}`);
    }));

    await test('all 93 related-view controls navigate or provide explicit fixture feedback', () => withPage(browser, VIEWPORTS[0], async (page) => {
      let relationships = 0;
      for (const [screenId, screen] of Object.entries(screens)) {
        for (let index = 0; index < screen.relations.length; index += 1) {
          await openScreen(page, server.baseUrl, screenId);
          const relations = page.getByRole('main').locator('.ui-preview__relations button');
          const relationCount = await relations.count();
          invariant(
            relationCount === screen.relations.length,
            `${screenId}: expected ${screen.relations.length} relationships in the active page, found ${relationCount}`,
          );
          const relation = relations.nth(index);
          try {
            await relation.scrollIntoViewIfNeeded();
            await relation.waitFor({ state: 'visible' });
          } catch (error) {
            throw new Error(`${screenId} relationship ${index} (${screen.relations[index]}): ${error.message}`);
          }
          await click(relation);
          const destination = await page.getByTestId('ui-preview').getAttribute('data-screen');
          const noticeVisible = await page.locator('.ui-preview__notice[role="status"]').isVisible().catch(() => false);
          invariant(destination !== screenId || noticeVisible, `${screenId} relationship ${index} produced no visible outcome`);
          relationships += 1;
        }
      }
      invariant(relationships === 93, `Expected 93 relationships, exercised ${relationships}`);
    }));

    await test('mobile user navigates through real controls and retains safe-area access', () => withPage(browser, VIEWPORTS[4], async (page) => {
      await openScreen(page, server.baseUrl, 'mobile-hub');
      const mobileNav = page.getByRole('navigation', { name: 'Mobile UI preview navigation' });
      invariant(await mobileNav.getByRole('button', { name: 'Hub', exact: true }).getAttribute('aria-current') === 'page', 'Hub is not active in mobile navigation');
      await click(mobileNav.getByRole('button', { name: 'Chat', exact: true }));
      await assertScreen(page, 'chat', 'Chat');
      invariant(await mobileNav.getByRole('button', { name: 'Chat', exact: true }).getAttribute('aria-current') === 'page', 'Chat is not active after mobile navigation');
      await click(page.getByRole('button', { name: 'Open mobile navigation', exact: true }));
      await assertScreen(page, 'mobile-nav', 'Navigation');
      await click(page.locator('.ui-preview__relations button').filter({ hasText: '/settings' }));
      await assertScreen(page, 'settings', 'Settings');
      await assertLayout(page, 'mobile settings');
    }));

    await test('all screens pass layout assertions at five user viewports', async () => {
      for (const viewport of VIEWPORTS) {
        await withPage(browser, viewport, async (page) => {
          for (const [screenId, screen] of Object.entries(screens)) {
            await openScreen(page, server.baseUrl, screenId);
            await assertLayout(page, `${viewport.name}/${screenId}`);
            responsiveChecks += 1;
          }
        });
      }
      invariant(responsiveChecks === Object.keys(screens).length * VIEWPORTS.length, `Expected 145 responsive checks, ran ${responsiveChecks}`);
    });
  } finally {
    await browser.close();
    await server.close();
  }

  const failed = results.filter((result) => result.status === 'fail');
  console.log(`\nUser journeys: ${results.length - failed.length}/${results.length} passed`);
  console.log(`Browser interactions: ${browserInteractions}`);
  console.log(`Responsive screen checks: ${responsiveChecks}`);
  if (failed.length) throw new Error(failed.map(({ name, error }) => `${name}: ${error}`).join('\n'));
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
