import { render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { SelfImprovementFeed } from '../../components/SelfImprovementFeed';

describe('SelfImprovementFeed', () => {
  let fetchMock;
  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
  });
  afterEach(() => {
    fetchMock.mockReset();
  });

  it('renders empty state initially', () => {
    fetchMock.mockReturnValueOnce(new Promise(() => {})); // never resolves
    const { getByTestId } = render(<SelfImprovementFeed token="t" />);
    expect(getByTestId('self-improve-empty')).toBeInTheDocument();
  });

  it('shows entries with verdict, capped at 5, most recent first', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        loops: [
          { id: 'a' },
          { id: 'b', verdict: 'rejected' },
          { id: 'c', verdict: 'accepted' },
          { id: 'd', verdict: 'accepted' },
          { id: 'e' },
          { id: 'f', verdict: 'rejected' },
          { id: 'g', verdict: 'accepted' },
        ],
      }),
    });
    const { container } = render(<SelfImprovementFeed token="t" />);
    await waitFor(() => {
      const items = container.querySelectorAll('[data-testid="self-improve-item"]');
      expect(items).toHaveLength(5);
    }, { timeout: 3000 });
    const items = container.querySelectorAll('[data-testid="self-improve-item"]');
    expect(items[0].getAttribute('data-verdict')).toBe('accepted');
    expect(items[0].textContent).toContain('g');
  });

  it('falls back to entries key when loops missing', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        entries: [{ id: 'x', verdict: 'accepted' }],
      }),
    });
    const { container } = render(<SelfImprovementFeed token="t" />);
    await waitFor(() => {
      const items = container.querySelectorAll('[data-testid="self-improve-item"]');
      expect(items).toHaveLength(1);
    });
  });

  it('renders empty state on fetch failure', async () => {
    fetchMock.mockRejectedValueOnce(new Error('boom'));
    const { getByTestId } = render(<SelfImprovementFeed token="t" />);
    await waitFor(() => {
      expect(getByTestId('self-improve-empty')).toBeInTheDocument();
    });
  });

  it('requests /learning/history with auth header', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    render(<SelfImprovementFeed token="tok" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = fetchMock.mock.calls[0][0];
    expect(url).toContain('/learning/history');
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers['X-Hive-Token']).toBe('tok');
  });
});
