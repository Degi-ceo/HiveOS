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

  it('keeps fixture feedback dismissible and mobile device navigation usable', () => {
    const { getByRole, queryByRole } = render(<UiPreview />);
    const sidebar = getByRole('navigation', { name: 'UI preview screens' });

    fireEvent.click(within(sidebar).getByRole('button', { name: 'Memory' }));
    fireEvent.click(within(getByRole('main')).getByRole('button', { name: 'Add memory' }));
    expect(getByRole('status')).toHaveTextContent('fixture-only');
    fireEvent.click(getByRole('button', { name: 'Dismiss message' }));
    expect(queryByRole('status')).not.toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole('button', { name: 'Mobile Hub' }));
    fireEvent.click(within(getByRole('navigation', { name: 'Mockup device navigation' })).getByRole('button', { name: 'Chat' }));
    expect(getByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument();
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

      const rowActionCount = screen.rows.filter((row) => row[3]).length;
      for (let index = 0; index < rowActionCount; index += 1) {
        openScreen();
        // Hub uses .hub__attention-list for row actions; other screens use .ui-preview__surface--main
        const mainSurface = container.querySelector('.hub__attention-list') ||
          container.querySelector('.ui-preview__surface--main');
        if (mainSurface) {
          fireEvent.click(within(mainSurface).getAllByRole('button')[index]);
          expect(getByRole('heading', { level: 1 })).toBeInTheDocument();
        }
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
        + screen.rows.filter((row) => row[3]).length + screen.relations.length,
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
