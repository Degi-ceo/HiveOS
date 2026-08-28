import { fireEvent, render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Centre } from '../Centre';

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}));

import { useWebSocket } from '../hooks/useWebSocket';

describe('Centre', () => {
  let fetchMock;
  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    useWebSocket.mockReturnValue({ messages: [], status: 'open' });
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.clearAllMocks();
  });

  it('mounts all primary regions', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    const { getByTestId } = render(<Centre token="t" sessionId="s1" />);
    expect(getByTestId('centre')).toBeInTheDocument();
    expect(getByTestId('centre-left')).toBeInTheDocument();
    expect(getByTestId('centre-center')).toBeInTheDocument();
    expect(getByTestId('centre-right')).toBeInTheDocument();
  });

  it('passes token and sessionId to children', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    render(<Centre token="tok" sessionId="sess" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // /skills?pinned=true (SkillLauncher) and /learning/history (SelfImprove)
    // and /health/summary (SurfaceBar) and /memory/topics (MemoryPeek) all
    // get the auth header.
    const auths = fetchMock.mock.calls
      .map((c) => c[1]?.headers?.['X-Hive-Token'])
      .filter(Boolean);
    expect(auths.length).toBeGreaterThan(0);
    expect(auths.every((a) => a === 'tok')).toBe(true);
  });

  it('opens the command palette with the hook-provided state', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({}) });
    const { getByRole } = render(<Centre token="tok" sessionId="sess" />);

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });

    await waitFor(() => expect(getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument());
  });
});
