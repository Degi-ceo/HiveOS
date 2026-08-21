import { useCallback } from 'react';

/**
 * useHaptic — triggers a short haptic pulse via navigator.vibrate.
 * Gracefully no-ops on platforms that don't support Vibration API.
 */
export function useHaptic() {
  const vibrate = useCallback((duration = 10) => {
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      navigator.vibrate(duration);
    }
  }, []);

  return { vibrate };
}
