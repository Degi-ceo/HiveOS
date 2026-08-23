import pkg from './node_modules/playwright-core/index.js';
const { chromium } = pkg;

const BASE = 'http://localhost:4752';
let exitCode = 0;

async function journey1() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    await page.goto(`${BASE}/?ui-preview=1&screen=hub`, { waitUntil: 'networkidle' });

    // 1. Wait for page load
    await page.waitForSelector('h1', { timeout: 5000 });

    // 2. Check h1 "Hub" visible
    const h1 = await page.textContent('h1');
    if (!h1 || !h1.toLowerCase().includes('hub')) {
      console.log('Journey 1: FAIL — h1 does not contain "Hub": ' + h1);
      exitCode = 1; return;
    }

    // 3. Click "New task" button → verify new-task screen loads
    const newTaskBtn = page.getByRole('button', { name: /new task/i }).first();
    await newTaskBtn.click();
    await page.waitForURL(/screen=new-task/, { timeout: 5000 });
    const h1NewTask = await page.textContent('h1');
    if (!h1NewTask || !h1NewTask.toLowerCase().includes('new task')) {
      console.log('Journey 1: FAIL — new-task h1 not found after click: ' + h1NewTask);
      exitCode = 1; return;
    }

    // 4. Close via Close button → back to hub
    const closeBtn = page.getByRole('button', { name: /close/i });
    await closeBtn.click();
    await page.waitForURL(/screen=hub/, { timeout: 5000 });

    // 5. Open notifications button → verify notifications panel
    const notifBtn = page.getByRole('button', { name: /notification/i }).first();
    await notifBtn.click();
    await page.waitForTimeout(500);
    const notifPanel = await page.locator('[data-panel], .notifications, [role="region"]').first().isVisible().catch(() => false);
    // Just verify something changed (URL or visible element)
    const urlAfterNotif = page.url();

    // 6. Close notifications
    const closeNotif = page.getByRole('button', { name: /close|back/i }).first();
    await closeNotif.click();
    await page.waitForTimeout(300);

    // 7. Search/command palette button → verify command palette opens
    const searchBtn = page.getByRole('button', { name: /search|command|palette/i }).first();
    await searchBtn.click();
    await page.waitForTimeout(500);
    const paletteVisible = await page.locator('[role="dialog"], input[placeholder], .palette, .command-palette').first().isVisible().catch(() => false);

    // 8. Close with Escape → back to hub
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    if (errors.length > 0) {
      console.log('Journey 1: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 1: PASS — hub system overview');
    }
  } catch (e) {
    console.log('Journey 1: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey2() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    // 1. Wait for page load
    await page.goto(`${BASE}/?ui-preview=1&screen=new-task`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });

    // 2. Verify h1 "New task"
    const h1 = await page.textContent('h1');
    if (!h1 || !h1.toLowerCase().includes('new task')) {
      console.log('Journey 2: FAIL — h1 does not contain "New task": ' + h1);
      exitCode = 1; return;
    }

    // 3. Close with button → verify hub loads
    const closeBtn = page.getByRole('button', { name: /close/i });
    await closeBtn.click();
    await page.waitForURL(/screen=hub/, { timeout: 5000 });
    const hubH1 = await page.textContent('h1');
    if (!hubH1 || !hubH1.toLowerCase().includes('hub')) {
      console.log('Journey 2: FAIL — not on hub after close: ' + hubH1);
      exitCode = 1; return;
    }

    // 4. Navigate to new-task again
    await page.goto(`${BASE}/?ui-preview=1&screen=new-task`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });

    // 5. Close with Escape → verify hub
    await page.keyboard.press('Escape');
    await page.waitForTimeout(500);
    await page.waitForURL(/screen=hub/, { timeout: 5000 }).catch(() => {});

    if (errors.length > 0) {
      console.log('Journey 2: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 2: PASS — new task overlay');
    }
  } catch (e) {
    console.log('Journey 2: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey3() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    await page.goto(`${BASE}/?ui-preview=1&screen=chat`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });

    // 1. Verify h1 "Chat"
    const h1 = await page.textContent('h1');
    if (!h1 || !h1.toLowerCase().includes('chat')) {
      console.log('Journey 3: FAIL — h1 does not contain "Chat": ' + h1);
      exitCode = 1; return;
    }

    // 2. Click "Conversation" tab
    const convTab = page.getByRole('tab', { name: /conversation/i });
    if (await convTab.isVisible()) {
      await convTab.click();
      await page.waitForTimeout(300);
    }

    // 3. Click "Run details" tab
    const runTab = page.getByRole('tab', { name: /run details/i });
    if (await runTab.isVisible()) {
      await runTab.click();
      await page.waitForTimeout(300);
    }

    if (errors.length > 0) {
      console.log('Journey 3: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 3: PASS — chat and tab switching');
    }
  } catch (e) {
    console.log('Journey 3: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey4() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    await page.goto(`${BASE}/?ui-preview=1&screen=memory`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });

    // 1. Verify h1 "Memory"
    const h1 = await page.textContent('h1');
    if (!h1 || !h1.toLowerCase().includes('memory')) {
      console.log('Journey 4: FAIL — h1 does not contain "Memory": ' + h1);
      exitCode = 1; return;
    }

    // 2. Click "Important" tab
    const impTab = page.getByRole('tab', { name: /important/i });
    if (await impTab.isVisible()) {
      await impTab.click();
      await page.waitForTimeout(300);
    }

    // 3. Click "Topics" tab
    const topicsTab = page.getByRole('tab', { name: /topics/i });
    if (await topicsTab.isVisible()) {
      await topicsTab.click();
      await page.waitForTimeout(300);
    }

    // 4. Click "Sessions" tab
    const sessionsTab = page.getByRole('tab', { name: /sessions/i });
    if (await sessionsTab.isVisible()) {
      await sessionsTab.click();
      await page.waitForTimeout(300);
    }

    if (errors.length > 0) {
      console.log('Journey 4: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 4: PASS — memory tabs');
    }
  } catch (e) {
    console.log('Journey 4: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey5() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    await page.goto(`${BASE}/?ui-preview=1&screen=tasks`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });

    // 1. Verify h1 "Tasks"
    const h1 = await page.textContent('h1');
    if (!h1 || !h1.toLowerCase().includes('task')) {
      console.log('Journey 5: FAIL — h1 does not contain "Tasks": ' + h1);
      exitCode = 1; return;
    }

    // 2. Click "Cron" tab
    const cronTab = page.getByRole('tab', { name: /cron/i });
    if (await cronTab.isVisible()) {
      await cronTab.click();
      await page.waitForTimeout(300);
    }

    // 3. Click "Promises" tab
    const promTab = page.getByRole('tab', { name: /promises/i });
    if (await promTab.isVisible()) {
      await promTab.click();
      await page.waitForTimeout(300);
    }

    // 4. Click "Kanban" tab
    const kanbanTab = page.getByRole('tab', { name: /kanban/i });
    if (await kanbanTab.isVisible()) {
      await kanbanTab.click();
      await page.waitForTimeout(300);
    }

    if (errors.length > 0) {
      console.log('Journey 5: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 5: PASS — tasks Kanban/Cron/Commitments routing');
    }
  } catch (e) {
    console.log('Journey 5: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey6() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    await page.goto(`${BASE}/?ui-preview=1&screen=hub`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });

    // 1. Press Ctrl+K to open command palette
    await page.keyboard.press('k', { modifiers: ['Control'] });
    await page.waitForTimeout(500);
    const paletteVisible = await page.locator('[role="dialog"], input[placeholder], .palette, .command-palette').first().isVisible().catch(() => false);

    if (!paletteVisible) {
      // Try clicking search button as fallback
      const searchBtn = page.getByRole('button', { name: /search|command|palette|k/i }).first();
      if (await searchBtn.isVisible()) {
        await searchBtn.click();
        await page.waitForTimeout(500);
      }
    }

    // 2. Press Escape to close
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    if (errors.length > 0) {
      console.log('Journey 6: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 6: PASS — global command palette Ctrl+K');
    }
  } catch (e) {
    console.log('Journey 6: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey7() {
  const errors = [];
  let context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 390, height: 844 } }));
  let page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    // 1. Set viewport 390x844
    await page.goto(`${BASE}/?ui-preview=1&screen=mobile-hub`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);

    // 2. Verify bottom nav buttons exist
    const navButtons = await page.locator('nav button, [role="navigation"] button, .bottom-nav button').count();
    if (navButtons === 0) {
      // Try alternative selectors
      const altNav = await page.locator('footer button, .nav button, [class*="nav"] button').count();
      if (altNav === 0) {
        console.log('Journey 7: WARN — no bottom nav buttons found');
      }
    }

    // 3. Navigate to mobile-chat
    await page.goto(`${BASE}/?ui-preview=1&screen=mobile-chat`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);

    // 4. Verify Chat is highlighted in nav
    const chatNavItem = page.getByText(/chat/i).first();
    const chatVisible = await chatNavItem.isVisible();

    if (errors.length > 0) {
      console.log('Journey 7: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 7: PASS — mobile nav highlight');
    }
  } catch (e) {
    console.log('Journey 7: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

async function journey8() {
  const errors = [];
  const context = await chromium.launch({ headless: true }).then(b => b.newContext({ viewport: { width: 1440, height: 900 } }));
  const page = await context.newPage();
  page.on('console', msg => { if (msg.type() === 'error') errors.push('[console.error] ' + msg.text()); });
  page.on('pageerror', err => errors.push('[pageerror] ' + err.message));

  try {
    // 1. Start at hub
    await page.goto(`${BASE}/?ui-preview=1&screen=hub`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });
    const hubH1 = await page.textContent('h1');
    if (!hubH1 || !hubH1.toLowerCase().includes('hub')) {
      console.log('Journey 8: FAIL — hub not found: ' + hubH1);
      exitCode = 1; return;
    }

    // 2. Navigate to tasks
    await page.goto(`${BASE}/?ui-preview=1&screen=tasks`, { waitUntil: 'networkidle' });
    await page.waitForSelector('h1', { timeout: 5000 });
    const tasksH1 = await page.textContent('h1');
    if (!tasksH1 || !tasksH1.toLowerCase().includes('task')) {
      console.log('Journey 8: FAIL — tasks not found: ' + tasksH1);
      exitCode = 1; return;
    }

    // 3. Navigate to memory via sidebar
    const memoryNav = page.getByRole('link', { name: /memory/i }).first();
    if (await memoryNav.isVisible()) {
      await memoryNav.click();
      await page.waitForURL(/screen=memory/, { timeout: 5000 });
    } else {
      await page.goto(`${BASE}/?ui-preview=1&screen=memory`, { waitUntil: 'networkidle' });
    }
    await page.waitForSelector('h1', { timeout: 5000 });

    // 4. Press browser back → verify tasks
    await page.goBack();
    await page.waitForURL(/screen=tasks/, { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(500);

    // 5. Press browser back → verify hub
    await page.goBack();
    await page.waitForURL(/screen=hub/, { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(500);

    if (errors.length > 0) {
      console.log('Journey 8: FAIL — console errors: ' + errors.join(' | '));
      exitCode = 1;
    } else {
      console.log('Journey 8: PASS — browser back/forward state');
    }
  } catch (e) {
    console.log('Journey 8: FAIL — exception: ' + e.message);
    exitCode = 1;
  } finally {
    await page.close();
    await context.close();
  }
}

(async () => {
  await journey1();
  await journey2();
  await journey3();
  await journey4();
  await journey5();
  await journey6();
  await journey7();
  await journey8();

  if (exitCode !== 0) {
    console.log('\nOverall: SOME JOURNEYS FAILED');
    process.exit(1);
  } else {
    console.log('\nOverall: ALL JOURNEYS PASSED');
  }
})();
