import { useCallback } from 'react';

/** Map of haptic type → vibration pattern (ms). null = no-op */
const PATTERNS = {
  light:   10,
  medium:  20,
  success: [10, 30, 10],
  warning: [10, 50, 10],
  error:   [10, 80, 10],
};

/**
 * useHaptic — returns a `trigger(type)` function that calls navigator.vibrate.
 * No-op when vibrate is unavailable (SSR, desktop Safari, etc.).
 */
export function useHaptic() {
  const trigger = useCallback((type = 'light') => {
    const pattern = PATTERNS[type] ?? PATTERNS.light;
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      navigator.vibrate(pattern);
    }
  }, []);

  return { trigger };
}
