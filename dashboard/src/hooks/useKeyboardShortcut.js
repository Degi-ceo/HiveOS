import { useEffect } from 'react';

/**
 * useKeyboardShortcut — registers a keyboard event listener and calls `callback`
 * when the given `key` is pressed with the specified modifiers.
 *
 * @param {string} key - e.g. 'k', 'Enter', 'Escape'
 * @param {() => void} callback
 * @param {{ metaKey?: boolean, ctrlKey?: boolean, shiftKey?: boolean, allowRepeat?: boolean }} options
 */
export function useKeyboardShortcut(key, callback, options = {}) {
  const { metaKey = false, ctrlKey = false, shiftKey = false, allowRepeat = false } = options;

  useEffect(() => {
    function handler(e) {
      if (e.key !== key) return;
      if (metaKey && !e.metaKey) return;
      if (ctrlKey && !e.ctrlKey) return;
      if (shiftKey && !e.shiftKey) return;
      if (!allowRepeat && e.repeat) return;
      e.preventDefault();
      callback(e);
    }
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [key, callback, metaKey, ctrlKey, shiftKey, allowRepeat]);
}
