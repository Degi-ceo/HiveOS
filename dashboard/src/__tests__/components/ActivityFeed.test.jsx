import { render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ActivityFeed } from '../../components/ActivityFeed';

vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}));

import { useWebSocket } from '../../hooks/useWebSocket';

describe('ActivityFeed', () => {
  beforeEach(() => {
    useWebSocket.mockReturnValue({ messages: [], status: 'open' });
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('renders empty state when no tool events', () => {
    useWebSocket.mockReturnValue({ messages: [{ event_type: 'ping' }], status: 'open' });
    const { getByTestId } = render(<ActivityFeed token="t" />);
    expect(getByTestId('activity-empty')).toBeInTheDocument();
  });

  it('shows tool_call events filtered from messages', () => {
    const msgs = [
      { event_type: 'ping' },
      { event_type: 'tool_call', tool_name: 'web_search' },
      { event_type: 'chat.message' },
    ];
    useWebSocket.mockReturnValue({ messages: msgs, status: 'open' });
    const { getAllByTestId } = render(<ActivityFeed token="t" />);
    const items = getAllByTestId('activity-item');
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toContain('tool_call');
    expect(items[0].textContent).toContain('web_search');
  });

  it('shows A2A events (a2a.call.*) too', () => {
    const msgs = [
      { type: 'a2a.call.started', agent_name: 'planner' },
      { type: 'a2a.call.completed', agent_name: 'coder' },
      { type: 'a2a.call.failed', agent_name: 'reviewer' },
    ];
    useWebSocket.mockReturnValue({ messages: msgs, status: 'open' });
    const { getAllByTestId } = render(<ActivityFeed token="t" />);
    const items = getAllByTestId('activity-item');
    expect(items).toHaveLength(3);
  });

  it('caps at 10 items and shows most recent first', () => {
    const msgs = Array.from({ length: 12 }, (_, i) => ({
      event_type: 'tool_call',
      tool_name: `tool_${i}`,
    }));
    useWebSocket.mockReturnValue({ messages: msgs, status: 'open' });
    const { getAllByTestId } = render(<ActivityFeed token="t" />);
    const items = getAllByTestId('activity-item');
    expect(items).toHaveLength(10);
    expect(items[0].textContent).toContain('tool_11');
    expect(items[9].textContent).toContain('tool_2');
  });

  it('passes token to useWebSocket', () => {
    render(<ActivityFeed token="abc" />);
    expect(useWebSocket).toHaveBeenCalledWith('abc');
  });
});
