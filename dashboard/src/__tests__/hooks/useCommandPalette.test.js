import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useCommandPalette } from '../../hooks/useCommandPalette';

// Mock useGateway with synchronous resolves so no fake timers needed
vi.mock('../../hooks/useGateway', () => ({
  useGateway: () => ({
    get: vi.fn()
      .mockResolvedValueOnce({ pinned: ['skill-a', 'skill-b'] })
      .mockResolvedValueOnce({ recent: [{ id: 'm1', text: 'memory item one', type: 'fact' }] })
      .mockResolvedValueOnce([{ id: 1, summary: 'Approval one', agent: 'researcher' }]),
  }),
}));

describe('useCommandPalette', () => {
  it('starts closed', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    expect(result.current.isOpen).toBe(false);
  });

  it('opens on open() call', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    expect(result.current.isOpen).toBe(true);
  });

  it('closes on close() call', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => { result.current.open(); });
    act(() => result.current.close());
    expect(result.current.isOpen).toBe(false);
  });

  it('resets query and selection on close', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    act(() => result.current.setQuery('test'));
    act(() => result.current.close());
    expect(result.current.query).toBe('');
    expect(result.current.selectedIndex).toBe(0);
  });

  it('selectNext increments selectedIndex', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    act(() => result.current.selectNext());
    // Results are loaded from mock, so selectNext moves to 1 (0-based, capped at length-1)
    expect(result.current.selectedIndex).toBe(1);
  });

  it('selectPrev does not go below 0', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    act(() => result.current.selectPrev());
    expect(result.current.selectedIndex).toBe(0);
  });

  it('execute closes the palette', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    act(() => result.current.execute({ id: 'cmd:chat' }));
    expect(result.current.isOpen).toBe(false);
  });

  it('execute returns the item', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    const item = { id: 'cmd:chat', label: 'Open chat' };
    const returned = result.current.execute(item);
    expect(returned).toEqual(item);
  });

  it('open loads data from gateway', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    // Results become available after the async get calls resolve
    // With synchronous mock, the data is available synchronously
    expect(result.current.results.length).toBeGreaterThan(0);
  });

  it('groupMap is populated after open', () => {
    const { result } = renderHook(() => useCommandPalette('token'));
    act(() => result.current.open());
    expect(Object.keys(result.current.groupMap).length).toBeGreaterThan(0);
  });
});
