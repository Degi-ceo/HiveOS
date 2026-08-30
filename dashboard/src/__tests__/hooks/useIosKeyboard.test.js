import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useIosKeyboard } from '../../hooks/useIosKeyboard';

describe('useIosKeyboard', () => {
  const origVisualViewport = window.visualViewport;

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    // Restore original visualViewport
    Object.defineProperty(window, 'visualViewport', {
      value: origVisualViewport,
      writable: true,
      configurable: true,
    });
  });

  it('returns false/0 when visualViewport is absent', () => {
    Object.defineProperty(window, 'visualViewport', { value: undefined, writable: true, configurable: true });
    const { result } = renderHook(() => useIosKeyboard());
    expect(result.current.isKeyboardOpen).toBe(false);
    expect(result.current.keyboardHeight).toBe(0);
  });

  it('detects keyboard open via visualViewport offset', () => {
    const listeners = {};
    const mockVp = {
      offsetTop: 500,
      height: 300,
      addEventListener: vi.fn((event, cb) => { listeners[event] = cb; }),
      removeEventListener: vi.fn(),
    };
    Object.defineProperty(window, 'visualViewport', { value: mockVp, writable: true, configurable: true });

    const { result } = renderHook(() => useIosKeyboard());

    // Simulate viewport resize indicating keyboard open
    Object.defineProperty(window, 'innerHeight', { value: 600, writable: true, configurable: true });
    act(() => { listeners['resize']?.(); });

    expect(result.current.isKeyboardOpen).toBe(true);
    expect(result.current.keyboardHeight).toBe(200); // offsetBottom = 500+300-600 = 200
  });

  it('detects keyboard close', () => {
    const listeners = {};
    const mockVp = {
      offsetTop: 0,
      height: 800,
      addEventListener: vi.fn((event, cb) => { listeners[event] = cb; }),
      removeEventListener: vi.fn(),
    };
    Object.defineProperty(window, 'visualViewport', { value: mockVp, writable: true, configurable: true });

    const { result } = renderHook(() => useIosKeyboard());

    Object.defineProperty(window, 'innerHeight', { value: 800, writable: true, configurable: true });
    act(() => { listeners['resize']?.(); });

    expect(result.current.isKeyboardOpen).toBe(false);
    expect(result.current.keyboardHeight).toBe(0);
  });

  it('cleans up resize listener on unmount', () => {
    const removeSpy = vi.fn();
    const mockVp = {
      offsetTop: 0, height: 800,
      addEventListener: vi.fn(),
      removeEventListener: removeSpy,
    };
    Object.defineProperty(window, 'visualViewport', { value: mockVp, writable: true, configurable: true });

    const { unmount } = renderHook(() => useIosKeyboard());
    unmount();
    expect(removeSpy).toHaveBeenCalledWith('resize', expect.any(Function));
  });
});
