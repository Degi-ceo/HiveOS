import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useNavState } from '../../hooks/useNavState';

describe('useNavState', () => {
  // Store listener refs so we can fire them from tests
  const listeners = {};
  beforeEach(() => {
    listeners.keydown = null;
    listeners.resize = null;
    vi.spyOn(document, 'addEventListener').mockImplementation((event, handler) => {
      if (event === 'keydown') listeners.keydown = handler;
      if (event === 'resize') listeners.resize = handler;
    });
    vi.spyOn(document, 'removeEventListener').mockImplementation(() => {
      listeners.keydown = null;
      listeners.resize = null;
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const triggerKeydown = (key) => {
    if (listeners.keydown) {
      act(() => { listeners.keydown({ key }); });
    }
  };

  const triggerResize = (width) => {
    Object.defineProperty(window, 'innerWidth', { value: width, writable: true });
    if (listeners.resize) {
      act(() => { listeners.resize(); });
    }
  };

  describe('initial state', () => {
    it('starts open when window.innerWidth >= 1024 (desktop)', () => {
      Object.defineProperty(window, 'innerWidth', { value: 1200, writable: true });
      const { result } = renderHook(() => useNavState());
      expect(result.current.isNavOpen).toBe(true);
    });

    it('starts closed when window.innerWidth < 1024 (mobile/tablet)', () => {
      Object.defineProperty(window, 'innerWidth', { value: 600, writable: true });
      const { result } = renderHook(() => useNavState());
      expect(result.current.isNavOpen).toBe(false);
    });
  });

  describe('openNav / closeNav', () => {
    it('openNav sets isNavOpen to true', () => {
      Object.defineProperty(window, 'innerWidth', { value: 600, writable: true });
      const { result } = renderHook(() => useNavState());
      act(() => { result.current.openNav(); });
      expect(result.current.isNavOpen).toBe(true);
    });

    it('closeNav sets isNavOpen to false', () => {
      Object.defineProperty(window, 'innerWidth', { value: 1200, writable: true });
      const { result } = renderHook(() => useNavState());
      act(() => { result.current.closeNav(); });
      expect(result.current.isNavOpen).toBe(false);
    });

    it('toggling works (open then close then open)', () => {
      Object.defineProperty(window, 'innerWidth', { value: 900, writable: true });
      const { result } = renderHook(() => useNavState());
      expect(result.current.isNavOpen).toBe(false);
      act(() => { result.current.openNav(); });
      expect(result.current.isNavOpen).toBe(true);
      act(() => { result.current.closeNav(); });
      expect(result.current.isNavOpen).toBe(false);
    });
  });

  describe('Escape key', () => {
    it('closes nav on Escape', () => {
      Object.defineProperty(window, 'innerWidth', { value: 1200, writable: true });
      const { result } = renderHook(() => useNavState());
      act(() => { result.current.openNav(); });
      expect(result.current.isNavOpen).toBe(true);
      triggerKeydown('Escape');
      expect(result.current.isNavOpen).toBe(false);
    });

    it('does not close on other keys', () => {
      Object.defineProperty(window, 'innerWidth', { value: 1200, writable: true });
      const { result } = renderHook(() => useNavState());
      act(() => { result.current.openNav(); });
      triggerKeydown('Enter');
      expect(result.current.isNavOpen).toBe(true);
    });
  });

  describe('resize', () => {
    it('opens nav when resizing from mobile to desktop', () => {
      Object.defineProperty(window, 'innerWidth', { value: 800, writable: true });
      renderHook(() => useNavState());
      triggerResize(1200);
      const { result } = renderHook(() => useNavState());
      expect(result.current.isNavOpen).toBe(true);
    });

    it('closes nav when resizing from desktop to mobile', () => {
      Object.defineProperty(window, 'innerWidth', { value: 1200, writable: true });
      renderHook(() => useNavState());
      triggerResize(600);
      const { result } = renderHook(() => useNavState());
      expect(result.current.isNavOpen).toBe(false);
    });
  });

  describe('event listener registration', () => {
    it('registers keydown listener on document and resize listener on window', () => {
      const docAddSpy = vi.spyOn(document, 'addEventListener');
      const winAddSpy = vi.spyOn(window, 'addEventListener');
      Object.defineProperty(window, 'innerWidth', { value: 1024, writable: true });
      renderHook(() => useNavState());
      const docEvents = docAddSpy.mock.calls.map(([e]) => e);
      const winEvents = winAddSpy.mock.calls.map(([e]) => e);
      expect(docEvents).toContain('keydown');
      expect(winEvents).toContain('resize');
    });

    it('removes keydown and resize listeners on unmount', () => {
      const docRemoveSpy = vi.spyOn(document, 'removeEventListener');
      const winRemoveSpy = vi.spyOn(window, 'removeEventListener');
      Object.defineProperty(window, 'innerWidth', { value: 1024, writable: true });
      const { unmount } = renderHook(() => useNavState());
      unmount();
      const docEvents = docRemoveSpy.mock.calls.map(([e]) => e);
      const winEvents = winRemoveSpy.mock.calls.map(([e]) => e);
      expect(docEvents).toContain('keydown');
      expect(winEvents).toContain('resize');
    });
  });
});
