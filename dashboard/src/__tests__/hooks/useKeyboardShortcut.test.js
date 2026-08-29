import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useKeyboardShortcut } from '../../hooks/useKeyboardShortcut';

describe('useKeyboardShortcut', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('fires callback on matching keydown', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('fires on Ctrl+K', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { ctrlKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('does NOT fire when modifier is missing', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: false }));
    expect(cb).not.toHaveBeenCalled();
  });

  it('does NOT fire on different key', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true }));
    expect(cb).not.toHaveBeenCalled();
  });

  it('does NOT fire repeat events by default', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true, repeat: true }));
    expect(cb).not.toHaveBeenCalled();
  });

  it('fires repeat events when allowRepeat=true', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true, allowRepeat: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true, repeat: true }));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('calls callback synchronously on matching key', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('cleans up listener on unmount', () => {
    const cb = vi.fn();
    const { unmount } = renderHook(() => useKeyboardShortcut('k', cb, { metaKey: true }));
    unmount();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }));
    expect(cb).not.toHaveBeenCalled();
  });

  it('handles Escape key', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('Escape', cb, {}));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('fires with shiftKey modifier', () => {
    const cb = vi.fn();
    renderHook(() => useKeyboardShortcut('ArrowUp', cb, { shiftKey: true }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', shiftKey: true }));
    expect(cb).toHaveBeenCalledTimes(1);
  });
});
