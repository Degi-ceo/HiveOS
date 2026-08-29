import { renderHook } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useLongPress } from '../../hooks/useLongPress';

describe('useLongPress', () => {
  let clearTimeoutSpy;

  beforeEach(() => {
    clearTimeoutSpy = vi.spyOn(global, 'clearTimeout');
  });
  afterEach(() => {
    clearTimeoutSpy.mockRestore();
    vi.restoreAllMocks();
  });

  it('does not fire before the delay', () => {
    const cb = vi.fn();
    const { result } = renderHook(() => useLongPress(cb, 450));

    result.current.onPointerDown({});
    expect(cb).not.toHaveBeenCalled();
  });

  it('fires after the delay on pointer down', async () => {
    vi.useFakeTimers();
    const cb = vi.fn();
    const { result } = renderHook(() => useLongPress(cb, 450));

    result.current.onPointerDown({});
    await vi.advanceTimersByTime(450);
    expect(cb).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it('cancels if pointer is released before delay', async () => {
    vi.useFakeTimers();
    const cb = vi.fn();
    const { result } = renderHook(() => useLongPress(cb, 450));

    result.current.onPointerDown({});
    await vi.advanceTimersByTime(200);
    result.current.onPointerUp();
    await vi.advanceTimersByTime(300);
    expect(cb).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('cancels on pointer leave before delay', async () => {
    vi.useFakeTimers();
    const cb = vi.fn();
    const { result } = renderHook(() => useLongPress(cb, 450));

    result.current.onPointerDown({});
    await vi.advanceTimersByTime(200);
    result.current.onPointerLeave();
    await vi.advanceTimersByTime(300);
    expect(cb).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('cancels on pointer cancel before delay', async () => {
    vi.useFakeTimers();
    const cb = vi.fn();
    const { result } = renderHook(() => useLongPress(cb, 450));

    result.current.onPointerDown({});
    await vi.advanceTimersByTime(100);
    result.current.onPointerCancel();
    await vi.advanceTimersByTime(400);
    expect(cb).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('returns pointer event handlers', () => {
    const cb = vi.fn();
    const { result } = renderHook(() => useLongPress(cb, 450));
    expect(typeof result.current.onPointerDown).toBe('function');
    expect(typeof result.current.onPointerUp).toBe('function');
    expect(typeof result.current.onPointerLeave).toBe('function');
    expect(typeof result.current.onPointerCancel).toBe('function');
  });
});
