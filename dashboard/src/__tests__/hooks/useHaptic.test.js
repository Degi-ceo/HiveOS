import { renderHook } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useHaptic } from '../../hooks/useHaptic';

describe('useHaptic', () => {
  beforeEach(() => {
    vi.stubGlobal('navigator', { vibrate: vi.fn() });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns a trigger function', () => {
    const { result } = renderHook(() => useHaptic());
    expect(typeof result.current.trigger).toBe('function');
  });

  it('calls navigator.vibrate with 10ms for light type', () => {
    const { result } = renderHook(() => useHaptic());
    result.current.trigger('light');
    expect(navigator.vibrate).toHaveBeenCalledWith(10);
  });

  it('calls navigator.vibrate with 20ms for medium type', () => {
    const { result } = renderHook(() => useHaptic());
    result.current.trigger('medium');
    expect(navigator.vibrate).toHaveBeenCalledWith(20);
  });

  it('calls navigator.vibrate with array pattern for success', () => {
    const { result } = renderHook(() => useHaptic());
    result.current.trigger('success');
    expect(navigator.vibrate).toHaveBeenCalledWith([10, 30, 10]);
  });

  it('falls back to 10ms for unknown type', () => {
    const { result } = renderHook(() => useHaptic());
    result.current.trigger('unknown');
    expect(navigator.vibrate).toHaveBeenCalledWith(10);
  });

  it('is no-op when navigator.vibrate is unavailable', () => {
    vi.stubGlobal('navigator', {});
    const { result } = renderHook(() => useHaptic());
    expect(() => result.current.trigger('light')).not.toThrow();
  });
});
