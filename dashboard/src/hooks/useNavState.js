import { useState, useEffect, useCallback } from 'react';

const DESKTOP_BREAKPOINT = 1024;

/**
 * useNavState — controls the sidebar/drawer open/close state.
 *
 * Handles:
 *  - openNav() / closeNav() toggles
 *  - Escape key closes on all viewports
 *  - Window resize closes on desktop (≥1024px), opens on mobile→desktop transition
 *  - Initial state: open on desktop, closed on mobile
 */
export function useNavState() {
  const [isNavOpen, setIsNavOpen] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth >= DESKTOP_BREAKPOINT : true
  );

  const openNav = useCallback(() => setIsNavOpen(true), []);
  const closeNav = useCallback(() => setIsNavOpen(false), []);

  // Escape key
  useEffect(() => {
    const handler = (e) => {
      if (e.key === 'Escape') closeNav();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [closeNav]);

  // Resize handler
  useEffect(() => {
    let prevWidth = window.innerWidth;

    const handler = () => {
      const currWidth = window.innerWidth;
      if (prevWidth < DESKTOP_BREAKPOINT && currWidth >= DESKTOP_BREAKPOINT) {
        // mobile → desktop: open
        setIsNavOpen(true);
      } else if (prevWidth >= DESKTOP_BREAKPOINT && currWidth < DESKTOP_BREAKPOINT) {
        // desktop → mobile: close
        setIsNavOpen(false);
      }
      prevWidth = currWidth;
    };

    window.addEventListener('resize', handler);
    return () => window.removeEventListener('resize', handler);
  }, []);

  return { isNavOpen, openNav, closeNav };
}
