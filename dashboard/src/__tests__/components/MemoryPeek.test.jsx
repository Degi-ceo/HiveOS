import { render, waitFor, fireEvent, cleanup } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { MemoryPeek } from '../../components/MemoryPeek';

describe('MemoryPeek', () => {
  let fetchMock;
  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
  });
  afterEach(() => {
    cleanup();
  });

  it('renders empty state when fetch returns empty topics', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ topics: [] }) });
    const { container } = render(<MemoryPeek token="t" />);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="memory-empty"]')).toBeInTheDocument();
    });
  });

  it('renders ≤8 topics after fetch', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        topics: [
          { name: 'a', count: 1 }, { name: 'b', count: 2 }, { name: 'c', count: 3 },
          { name: 'd', count: 4 }, { name: 'e', count: 5 }, { name: 'f', count: 6 },
          { name: 'g', count: 7 }, { name: 'h', count: 8 }, { name: 'i', count: 9 },
          { name: 'j', count: 10 },
        ],
      }),
    });
    const { container } = render(<MemoryPeek token="t" />);
    await waitFor(() => {
      const items = container.querySelectorAll('[data-testid="memory-topic"]');
      expect(items).toHaveLength(8);
    });
  });

  it('falls back to empty list on missing topics key', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const { container } = render(<MemoryPeek token="t" />);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="memory-empty"]')).toBeInTheDocument();
    });
  });

  it('falls back to empty list on fetch failure', async () => {
    fetchMock.mockRejectedValueOnce(new Error('boom'));
    const { container } = render(<MemoryPeek token="t" />);
    await waitFor(() => {
      expect(container.querySelector('[data-testid="memory-empty"]')).toBeInTheDocument();
    });
  });

  it('renders count from each topic', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ topics: [{ name: 'a', count: 42 }, { name: 'b', count: 7 }] }),
    });
    const { findAllByTestId } = render(<MemoryPeek token="t" />);
    const items = await findAllByTestId('memory-topic');
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain('a');
    expect(items[0].textContent).toContain('42');
    expect(items[1].textContent).toContain('b');
    expect(items[1].textContent).toContain('7');
  });

  it('omits close button when onClose is not provided', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ topics: [] }) });
    const { queryByTestId, container } = render(<MemoryPeek token="t" />);
    await waitFor(() => expect(container.querySelector('[data-testid="memory-empty"]')).toBeInTheDocument());
    expect(queryByTestId('memory-peek-close')).toBeNull();
  });

  it('renders close button when onClose provided; clicking calls it', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ topics: [] }) });
    const onClose = vi.fn();
    const { container, findByTestId } = render(<MemoryPeek token="t" onClose={onClose} />);
    const wrapper = await findByTestId('memory-peek');
    const closeBtn = wrapper.querySelector('button[data-testid="memory-peek-close"]');
    expect(closeBtn).not.toBeNull();
    expect(container.contains(closeBtn)).toBe(true);
    fireEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalled();
  });
});