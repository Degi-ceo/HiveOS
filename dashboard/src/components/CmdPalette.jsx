import React, { useEffect, useRef } from 'react';
import { useIosKeyboard } from '../hooks/useIosKeyboard';
import { useHaptic } from '../hooks/useHaptic';

/**
 * CmdPalette — SH3 command palette overlay.
 *
 * Props:
 *   isOpen         — controlled open state
 *   onClose        — called when palette should close
 *   query          — controlled query string
 *   setQuery       — (q => void) update query
 *   results        — flat array of result items
 *   groupMap       — { [groupName]: resultItem[] }
 *   selectedIndex  — currently keyboard-selected index in flatResults
 *   setSelectedIndex — (i => void) update selection
 *   selectNext     — () => void  — move selection down
 *   selectPrev     — () => void  — move selection up
 *   onExecute      — (item => void) called when user picks a result
 */
export function CmdPalette({
  isOpen,
  onClose,
  query,
  setQuery,
  results,
  groupMap,
  selectedIndex,
  setSelectedIndex,
  selectNext,
  selectPrev,
  onExecute,
}) {
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const { isKeyboardOpen, keyboardHeight } = useIosKeyboard();
  const { trigger } = useHaptic();

  // Auto-focus input when opened
  useEffect(() => {
    if (isOpen) {
      // Defer to let the modal render first
      const t = setTimeout(() => inputRef.current?.focus(), 20);
      return () => clearTimeout(t);
    }
  }, [isOpen]);

  // Scroll selected item into view (guard for jsdom which lacks scrollIntoView)
  useEffect(() => {
    const el = listRef.current?.querySelector('[data-selected="true"]');
    if (el?.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  // Keyboard: ↑↓↵Esc
  useEffect(() => {
    if (!isOpen) return;
    function onKey(e) {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          trigger('light');
          selectNext();
          break;
        case 'ArrowUp':
          e.preventDefault();
          trigger('light');
          selectPrev();
          break;
        case 'Enter': {
          e.preventDefault();
          const item = results[selectedIndex];
          if (item) {
            trigger('medium');
            onExecute(item);
          }
          break;
        }
        case 'Escape':
          e.preventDefault();
          onClose();
          break;
        default:
          break;
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, results, selectedIndex, selectNext, selectPrev, onExecute, onClose, trigger]);

  if (!isOpen) return null;

  // Build a flat list while tracking group starts for section headers
  const groupEntries = Object.entries(groupMap);

  return (
    <div
      className="cmd-palette-backdrop"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className={`cmd-palette-modal${isKeyboardOpen ? ' cmd-palette-modal--keyboard' : ''}`}
        style={isKeyboardOpen ? { paddingBottom: `${keyboardHeight + 16}px` } : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Drag handle (mobile) */}
        <div className="cmd-palette-drag-handle" aria-hidden="true" />

        {/* Search input */}
        <div className="cmd-palette-search">
          <span className="cmd-palette-search-icon" aria-hidden="true">🔍</span>
          <input
            ref={inputRef}
            type="text"
            className="cmd-palette-input"
            placeholder="What do you need?"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search commands, skills, memory"
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
          />
        </div>

        {/* Results */}
        <div className="cmd-palette-results" ref={listRef} role="listbox">
          {results.length === 0 && (
            <div className="cmd-palette-empty">
              <span>no results</span>
            </div>
          )}
          {groupEntries.map(([groupName, items]) => (
            <div key={groupName} className="cmd-palette-group">
              <div className="cmd-palette-group-header" aria-label={`${groupName} section`}>
                {groupName}
              </div>
              {items.map((item) => {
                const flatIdx = results.indexOf(item);
                const isSelected = flatIdx === selectedIndex;
                return (
                  <div
                    key={item.id}
                    className={`cmd-palette-result${isSelected ? ' cmd-palette-result--selected' : ''}`}
                    role="option"
                    aria-selected={isSelected}
                    data-selected={isSelected}
                    onClick={() => {
                      trigger('medium');
                      onExecute(item);
                    }}
                    onMouseEnter={() => {
                      if (!isSelected) {
                        trigger('light');
                        setSelectedIndex(flatIdx);
                      }
                    }}
                  >
                    <span className="cmd-palette-result-icon" aria-hidden="true">
                      {item.icon}
                    </span>
                    <span className="cmd-palette-result-label">{item.label}</span>
                    {item.description && (
                      <span className="cmd-palette-result-desc">{item.description}</span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>

        {/* Footer hint */}
        <div className="cmd-palette-footer" aria-hidden="true">
          <span>↑↓ navigate</span>
          <span>↵ select</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}

export default CmdPalette;
