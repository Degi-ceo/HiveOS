import { useRef, useCallback } from 'react';

/**
 * useLongPress — fires callback after a held pointer is held for `delay` ms.
 * Cancels if the pointer is released before the delay elapses.
 *
 * @param {Function} callback  — called with no args on successful long-press
 * @param {number}   delay    — ms to wait before firing (default 450)
 * @returns {{ onPointerDown, onPointerUp, onPointerLeave, onPointerCancel }}
 */
export function useLongPress(callback, delay = 450) {
  const timer = useRef(null);
  const fired = useRef(false);

  const clear = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    fired.current = false;
  }, []);

  const onPointerDown = useCallback(
    (_e) => {
      clear();
      fired.current = false;
      timer.current = setTimeout(() => {
        fired.current = true;
        callback();
      }, delay);
    },
    [callback, delay, clear]
  );

  const onPointerUp = useCallback(() => clear(), [clear]);
  const onPointerLeave = useCallback(() => clear(), [clear]);
  const onPointerCancel = useCallback(() => clear(), [clear]);

  return { onPointerDown, onPointerUp, onPointerLeave, onPointerCancel };
}
