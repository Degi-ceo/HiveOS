import { useState, useEffect } from 'react';

/**
 * useIosKeyboard — detects when the iOS software keyboard opens via the
 * `visualViewport` API (used by iOS Safari).
 *
 * Returns { isKeyboardOpen, keyboardHeight }.
 * Both are false/0 on desktop and non-iOS browsers.
 */
export function useIosKeyboard() {
  const [isKeyboardOpen, setIsKeyboardOpen] = useState(false);
  const [keyboardHeight, setKeyboardHeight] = useState(0);

  useEffect(() => {
    const vp = window.visualViewport;
    if (!vp) return;

    function onResize() {
      const offsetBottom = vp.offsetTop + vp.height - window.innerHeight;
      if (offsetBottom > 0) {
        setIsKeyboardOpen(true);
        setKeyboardHeight(offsetBottom);
      } else {
        setIsKeyboardOpen(false);
        setKeyboardHeight(0);
      }
    }

    vp.addEventListener('resize', onResize);
    return () => vp.removeEventListener('resize', onResize);
  }, []);

  return { isKeyboardOpen, keyboardHeight };
}
