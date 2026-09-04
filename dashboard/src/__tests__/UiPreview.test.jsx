import { fireEvent, render, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiPreview } from '../ui-preview/UiPreview';
import { navigationGroups, screens } from '../ui-preview/screenCatalog';

const canonicalPages = [
  'hub', 'chat', 'memory', 'skills', 'files', 'agents', 'tasks', 'channels',
  'mcp', 'logs', 'activity', 'sessions', 'approvals', 'self-improve',
  'analytics', 'docs', 'settings',
];

const mockupStates = [
  'agent-detail', 'command-palette', 'approval-modal', 'trace-detail', 'new-task',
  'notifications', 'release-log', 'cron', 'commitments', 'mobile-hub',
  'mobile-chat', 'mobile-nav',
];

const actionPattern = /^(open|review|inspect|view|manage|configure|preview|run)/i;

describe('UiPreview', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/?ui-preview=1');
  });

  it('registers all 29 approved mockups with complete fixture contracts', () => {
    const required = [...canonicalPages, ...mockupStates];
    const navigationIds = navigationGroups.flatMap((group) => group.items);

    expect(Object.keys(screens)).toHaveLength(29);
    expect(new Set(navigationIds)).toEqual(new Set(required));

    for (const id of required) {
      const screen = screens[id];
      expect(screen, id).toBeDefined();
      expect(screen.title, `${id}.title`).toBeTruthy();
      expect(screen.navLabel, `${id}.navLabel`).toBeTruthy();
      expect(screen.route, `${id}.route`).toBeTruthy();
      expect(screen.subtitle, `${id}.subtitle`).toBeTruthy();
      expect(screen.section, `${id}.section`).toBeTruthy();
      expect(screen.detailsTitle, `${id}.detailsTitle`).toBeTruthy();
      expect(screen.rows.length, `${id}.rows`).toBeGreaterThan(0);
      expect(screen.details.length, `${id}.details`).toBeGreaterThan(0);
      expect(screen.api.length, `${id}.api`).toBeGreaterThan(0);
      expect(screen.relations.length, `${id}.relations`).toBeGreaterThan(0);
      if (screen.defaultTab) expect(screen.tabs).toContain(screen.defaultTab);
    }
  });

  it('clicks through every standalone screen and keeps URL state in sync', () => {
    const { getByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });

    for (const [id, screen] of Object.entries(screens)) {
      fireEvent.click(within(sidebar).getByRole('button', { name: screen.navLabel }));
      expect(getByRole('heading', { level: 1, name: screen.title })).toBeInTheDocument();
      expect(new URLSearchParams(window.location.search).get('screen')).toBe(id);
    }
  });

  it('clicks every tab, exposes distinct panel state and deep-links it', () => {
    const { getByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });

    for (const [id, screen] of Object.entries(screens)) {
      for (const tab of screen.tabs) {
        fireEvent.click(within(sidebar).getByRole('button', { name: screen.navLabel }));
        const tablist = getByRole('tablist', { name: `${screen.title} views` });
        fireEvent.click(within(tablist).getByRole('tab', { name: tab }));

        const target = screen.tabTargets[tab];
        if (target) {
          expect(new URLSearchParams(window.location.search).get('screen')).toBe(target);
          expect(getByRole('heading', { level: 1, name: screens[target].title })).toBeInTheDocument();
        } else {
          expect(within(tablist).getByRole('tab', { name: tab })).toHaveAttribute('aria-selected', 'true');
          expect(new URLSearchParams(window.location.search).get('tab')).toBe(
            tab.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, ''),
          );
          expect(getByRole('tabpanel')).toHaveTextContent(`· ${tab}`);
        }
      }
    }
  });

  it('opens and closes global states from real controls and keyboard shortcuts', () => {
    const { getByRole } = render(<UiPreview />);

    fireEvent.click(getByRole('button', { name: 'Open notifications' }));
    expect(getByRole('heading', { level: 1, name: 'Notifications' })).toBeInTheDocument();
    fireEvent.click(getByRole('button', { name: 'Close' }));
    expect(getByRole('heading', { level: 1, name: 'Hub' })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(getByRole('heading', { level: 1, name: 'Command palette' })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(getByRole('heading', { level: 1, name: 'Hub' })).toBeInTheDocument();
  });

  it('supports arrow-key navigation across tabs', () => {
    window.history.replaceState({}, '', '/?ui-preview=1&screen=memory');
    const { getByRole } = render(<UiPreview />);
    const allTab = getByRole('tab', { name: 'All' });

    allTab.focus();
    fireEvent.keyDown(allTab, { key: 'ArrowRight' });
    expect(getByRole('tab', { name: 'Important' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(getByRole('tab', { name: 'Important' }), { key: 'End' });
    expect(getByRole('tab', { name: 'Sessions' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(getByRole('tab', { name: 'Sessions' }), { key: 'Home' });
    expect(getByRole('tab', { name: 'All' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(getByRole('tab', { name: 'All' }), { key: 'ArrowLeft' });
    expect(getByRole('tab', { name: 'Sessions' })).toHaveAttribute('aria-selected', 'true');
  });

  it('renders a page-specific layout for every canonical desktop page', () => {
    const { getByRole, getByTestId } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });

    for (const id of canonicalPages) {
      const screen = screens[id];
      fireEvent.click(within(sidebar).getByRole('button', { name: screen.navLabel }));
      expect(getByTestId('ui-preview')).toHaveAttribute('data-screen', id);
      expect(getByRole('main')).toHaveAttribute('data-view');
      expect(getByRole('main')).toHaveClass('ui-preview__main');
      expect(getByRole('main').matches('.hub, .memory-view, .tasks-view, .approvals-view, .domain-view')).toBe(true);
    }
  });

  it('exposes meaningful selection and filter state in Memory, Tasks and Approvals', () => {
    window.history.replaceState({}, '', '/?ui-preview=1&screen=memory');
    const { container, getByRole, getByText, queryByRole } = render(<UiPreview />);

    fireEvent.click(getByRole('tab', { name: 'Important' }));
    expect(getByRole('heading', { name: 'Important memories' })).toBeInTheDocument();
    expect(container.querySelectorAll('.memory-view__row')).toHaveLength(2);
    fireEvent.click(container.querySelector('[data-row-index="1"]'));
    expect(getByRole('heading', { name: 'Webhook retry failure analysis' })).toBeInTheDocument();
    fireEvent.click(getByRole('tab', { name: 'Topics' }));
    expect(getByText('Release and gateway knowledge')).toBeInTheDocument();
    fireEvent.click(getByRole('tab', { name: 'Sessions' }));
    expect(getByText('Session ses_8f912a')).toBeInTheDocument();

    const sidebar = getByRole('navigation', { name: 'UI preview screens' });
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Tasks' }));
    fireEvent.click(container.querySelector('.tasks-view__card[data-row-index="1"]'));
    expect(container.querySelector('.tasks-view__card[data-row-index="1"]')).toHaveAttribute('aria-pressed', 'true');
    expect(getByText('Selected task')).toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole('button', { name: 'Approvals' }));
    fireEvent.click(getByRole('tab', { name: 'Edits log' }));
    expect(getByRole('heading', { name: 'Decision history' })).toBeInTheDocument();
    expect(queryByRole('button', { name: 'Review' })).not.toBeInTheDocument();
  });

  it('uses native keyboard-operable controls for Hub drill-down tiles', () => {
    const { getByRole, getByTestId } = render(<UiPreview />);
    const gateway = getByRole('button', { name: /Gateway Healthy/i });
    expect(gateway.tagName).toBe('BUTTON');
    gateway.focus();
    fireEvent.keyDown(gateway, { key: 'Enter' });
    fireEvent.click(gateway);
    expect(getByTestId('ui-preview')).toHaveAttribute('data-screen', 'logs');
  });

  it('wires every dedicated header, tab keyboard handler and Hub drill-down', () => {
    const { getByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });
    const open = (name) => fireEvent.click(within(sidebar).getByRole('button', { name }));

    for (const name of ['Memory', 'Tasks', 'Approvals', 'Chat', 'Automations · Cron']) {
      open(name);
      fireEvent.click(getByRole('button', { name: 'Open mobile navigation' }));
      fireEvent.click(getByRole('button', { name: 'Close' }));
      fireEvent.click(getByRole('button', { name: 'Open notifications' }));
      fireEvent.click(getByRole('button', { name: 'Close' }));
      fireEvent.click(getByRole('button', { name: /Search or run a command/ }));
      fireEvent.keyDown(window, { key: 'Escape' });

      const tab = getByRole('tablist').querySelector('[role="tab"]');
      tab.focus();
      fireEvent.keyDown(tab, { key: 'ArrowRight' });
    }

    for (const [screenName, tabName] of [
      ['Tasks', 'Kanban'],
      ['Approvals', 'Pending'],
      ['Chat', 'Conversation'],
      ['Release log', 'Latest'],
    ]) {
      for (const key of ['Home', 'End', 'ArrowLeft', 'ArrowRight']) {
        open(screenName);
        const tab = getByRole('tab', { name: tabName });
        tab.focus();
        fireEvent.keyDown(tab, { key });
      }
    }

    const drillDowns = [
      [/Active agents 3/i, 'Agents'],
      [/Memory 12\.4k/i, 'Memory'],
      [/Approvals 1/i, 'Approvals'],
      [/Recent activity/i, 'Activity'],
      [/Quick actions/i, 'New task'],
      [/Connected services/i, 'Channels'],
      [/Model budget/i, 'Analytics'],
      [/Sessions today/i, 'Sessions'],
      [/Gateway retry refactor/i, 'Agents'],
    ];
    for (const [buttonName, heading] of drillDowns) {
      open('Hub');
      const matches = within(getByRole('main')).getAllByRole('button', { name: buttonName });
      fireEvent.click(matches[matches.length - 1]);
      expect(getByRole('heading', { level: 1, name: heading })).toBeInTheDocument();
    }
  });

  it('keeps fixture feedback dismissible and mobile device navigation usable', () => {
    const { getByRole, queryByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });

    fireEvent.click(within(sidebar).getByRole('button', { name: 'Memory' }));
    fireEvent.click(within(getByRole('main')).getByRole('button', { name: 'Add memory' }));
    expect(getByRole('status')).toHaveTextContent('fixture-only');
    fireEvent.click(getByRole('button', { name: 'Dismiss message' }));
    expect(queryByRole('status')).not.toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole('button', { name: 'Mobile Hub' }));
    expect(getByRole('main')).toHaveClass('ui-preview__main', 'hub');
    fireEvent.click(within(getByRole('navigation', { name: 'Mockup device navigation' })).getByRole('button', { name: 'Chat' }));
    expect(getByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
    expect(getByRole('main')).toHaveClass('ui-preview__main', 'domain-view');
  });

  it('wires every global navigation entry point', () => {
    const { getByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });

    fireEvent.click(within(sidebar.parentElement).getByRole('button', { name: '＋ New task' }));
    expect(getByRole('heading', { level: 1, name: 'New task' })).toBeInTheDocument();
    fireEvent.click(getByRole('button', { name: 'Close' }));

    fireEvent.click(getByRole('button', { name: 'Open mobile navigation' }));
    expect(getByRole('heading', { level: 1, name: 'Navigation' })).toBeInTheDocument();
    fireEvent.click(getByRole('button', { name: 'Close' }));
    expect(getByRole('heading', { level: 1, name: 'Hub' })).toBeInTheDocument();

    fireEvent.click(getByRole('button', { name: /Search or run a command/ }));
    expect(getByRole('heading', { level: 1, name: 'Command palette' })).toBeInTheDocument();

    const mobileBar = getByRole('navigation', { name: 'Mobile UI preview navigation' });
    fireEvent.click(within(mobileBar).getByRole('button', { name: 'Chat' }));
    expect(getByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
  });

  it('routes primary, row and relationship actions without backend calls', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const { getByRole, getByText, container } = render(<UiPreview />);

    fireEvent.click(getByRole('button', { name: 'New task' }));
    expect(getByRole('heading', { level: 1, name: 'New task' })).toBeInTheDocument();
    fireEvent.click(getByRole('button', { name: 'Close' }));

    // Hub uses .hub__attention-action instead of .ui-preview__surface for the review button
    const hubSurface = container.querySelector('.hub__attention-section');
    if (hubSurface) {
      fireEvent.click(within(hubSurface).getByRole('button', { name: 'Review' }));
    } else {
      const needsAttention = getByText('Needs attention').closest('.ui-preview__surface');
      fireEvent.click(within(needsAttention).getByRole('button', { name: 'Review' }));
    }
    expect(getByRole('heading', { level: 1, name: 'Approval review' })).toBeInTheDocument();
    fireEvent.click(getByRole('button', { name: 'Close' }));

    fireEvent.click(getByRole('button', { name: '/tasks' }));
    expect(getByRole('heading', { level: 1, name: 'Tasks' })).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it('exercises every primary action, row action and documented relationship safely', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const { container, getByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });
    let exercised = 0;

    for (const screen of Object.values(screens)) {
      const openScreen = () => fireEvent.click(within(sidebar).getByRole('button', { name: screen.navLabel }));

      if (screen.action) {
        openScreen();
        fireEvent.click(within(getByRole('main')).getByRole('button', { name: screen.action }));
        expect(getByRole('heading', { level: 1 })).toBeInTheDocument();
        exercised += 1;
      }

      for (let index = 0; index < screen.rows.length; index += 1) {
        if (!screen.rowTargets[index] && !actionPattern.test(screen.rows[index][3] || '')) continue;
        openScreen();
        const rowView = screen.kind === 'chat' ? 'Run details'
          : screen.kind === 'skills' ? 'All'
            : screen.kind === 'agents' ? 'All agents'
              : screen.kind === 'channels' ? screen.rows[index][0]
                : screen.kind === 'settings' ? screen.rows[index][1]
                : null;
        if (rowView) fireEvent.click(getByRole('tab', { name: rowView }));
        const control = container.querySelector(`[data-row-index="${index}"]`);
        expect(control, `${screen.id || screen.navLabel} row ${index}`).not.toBeNull();
        fireEvent.click(control);
        expect(getByRole('heading', { level: 1 })).toBeInTheDocument();
        exercised += 1;
      }

      for (let index = 0; index < screen.relations.length; index += 1) {
        openScreen();
        const relations = container.querySelector('.ui-preview__relations');
        fireEvent.click(within(relations).getAllByRole('button')[index]);
        expect(getByRole('heading', { level: 1 })).toBeInTheDocument();
        exercised += 1;
      }
    }

    const expected = Object.values(screens).reduce(
      (total, screen) => total + (screen.action ? 1 : 0)
        + screen.rows.filter((row, index) => screen.rowTargets[index] || actionPattern.test(row[3] || '')).length
        + screen.relations.length,
      0,
    );
    expect(exercised).toBe(expected);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it('restores direct screen and tab URLs and handles browser history events', () => {
    window.history.replaceState({}, '', '/?ui-preview=1&screen=memory&tab=important');
    const { getByRole } = render(<UiPreview />);
    expect(getByRole('heading', { level: 1, name: 'Memory' })).toBeInTheDocument();
    expect(getByRole('tab', { name: 'Important' })).toHaveAttribute('aria-selected', 'true');

    window.history.pushState({}, '', '/?ui-preview=1&screen=analytics&tab=errors');
    fireEvent.popState(window);
    expect(getByRole('heading', { level: 1, name: 'Analytics' })).toBeInTheDocument();
    expect(getByRole('tab', { name: 'Errors' })).toHaveAttribute('aria-selected', 'true');
  });

  it('falls back safely when a direct screen or tab is unknown', () => {
    window.history.replaceState({}, '', '/?ui-preview=1&screen=missing&tab=unknown');
    const { getByRole } = render(<UiPreview />);
    expect(getByRole('heading', { level: 1, name: 'Hub' })).toBeInTheDocument();

    window.history.pushState({}, '', '/?ui-preview=1&screen=memory&tab=missing');
    fireEvent.popState(window);
    expect(getByRole('heading', { level: 1, name: 'Memory' })).toBeInTheDocument();
    expect(getByRole('tab', { name: 'All' })).toHaveAttribute('aria-selected', 'true');
  });
});
