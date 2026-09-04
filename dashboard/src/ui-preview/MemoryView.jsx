import './ui-preview.css';

/**
 * MemoryView — calm, low-density memory browser.
 *
 * Design intent:
 * - Two compact summary tiles max (already in screen.metrics)
 * - One primary memory list with importance + namespace badges
 * - One calm inspector with source/importance/namespace/retention
 * - Filter tabs for All/Important/Topics/Sessions
 *
 * Deliberately quiet. No graphs, no decorative widgets.
 */

const TOPIC_ROWS = [
  ['Core platform', 'Release and gateway knowledge', 'High', '42 memories'],
  ['Operations', 'Runtime and reliability knowledge', 'Medium', '28 memories'],
  ['Personal', 'Operator preferences', 'Medium', '14 memories'],
];

const SESSION_ROWS = [
  ['HiveOS UI architecture review', 'Session ses_8f912a', 'High', '7 memories'],
  ['Gateway retry diagnosis', 'Session ses_6a102c', 'High', '5 memories'],
  ['Memory recovery', 'Session ses_31c44d', 'Medium', '3 memories'],
];

function rowsForTab(screen, activeTab) {
  if (activeTab === 'Important') return screen.rows.filter((row) => row[2] === 'High');
  if (activeTab === 'Topics') return TOPIC_ROWS;
  if (activeTab === 'Sessions') return SESSION_ROWS;
  return screen.rows;
}

export function MemoryView({ screen, activeTab, selectedRow, onAction, onRelation, onRow, onTab }) {
  const [summaryLabel, summaryValue, summaryMeta] = screen.metrics[0] || ['', '', ''];
  const [indexLabel, indexValue, indexMeta] = screen.metrics[1] || ['', '', ''];

  const handleTabKeyDown = (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...event.currentTarget.parentElement.querySelectorAll('[role="tab"]')];
    const current = tabs.indexOf(event.currentTarget);
    const next = event.key === 'Home' ? 0
      : event.key === 'End' ? tabs.length - 1
        : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    onTab(screen.tabs[next]);
  };

  const visibleRows = rowsForTab(screen, activeTab);
  const selectedIndex = selectedRow < 0 ? 0 : Math.min(selectedRow, Math.max(visibleRows.length - 1, 0));
  const selectedMemory = visibleRows[selectedIndex] || screen.rows[0];

  return (
    <main className="ui-preview__main memory-view" data-active-tab={activeTab} data-view="memory-browser">
      {/* ── Header ──────────────────────────────────────── */}
      <header className="ui-preview__header">
        <div className="ui-preview__title-block">
          <button className="ui-preview__menu" aria-label="Open mobile navigation" type="button" onClick={() => onAction('mobile-nav')}>☰</button>
          <div><h1>{screen.title}</h1><p>{screen.subtitle}</p></div>
        </div>
        <div className="ui-preview__header-actions">
          <button aria-label="Open notifications" type="button" className="ui-preview__icon-button" onClick={() => onAction('notifications')}>●</button>
          <button type="button" className="ui-preview__search" onClick={() => onAction('command-palette')}>Search or run a command <kbd>⌘K</kbd></button>
          {screen.action && <button type="button" className="ui-preview__primary" onClick={() => onAction(screen.primaryTarget)}>{screen.action}</button>}
        </div>
      </header>

      <div className="ui-preview__dev-note">
        <span>CONCEPT PREVIEW</span>
        <code>{screen.route}</code>
        {activeTab && <span className="ui-preview__view-state">View · {activeTab}</span>}
        <span>Static fixtures only · No backend calls</span>
      </div>

      {/* ── Two compact summary tiles max ───────────────────── */}
      <section className="memory-view__summary" aria-label="Memory summary">
        <article className="memory-view__summary-tile">
          <span>{summaryLabel}</span>
          <strong>{summaryValue}</strong>
          <small>{summaryMeta}</small>
        </article>
        <article className="memory-view__summary-tile">
          <span>{indexLabel}</span>
          <strong>{indexValue}</strong>
          <small>{indexMeta}</small>
        </article>
      </section>

      {/* ── Filter tabs ────────────────────────────────── */}
      {screen.tabs.length > 0 && (
        <div className="ui-preview__tabs" role="tablist" aria-label="Memory views">
          {screen.tabs.map((tab) => (
            <button
              aria-controls="memory-panel"
              aria-selected={tab === activeTab}
              className={tab === activeTab ? 'is-active' : ''}
              id={`memory-${tab.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-tab`}
              key={tab}
              role="tab"
              tabIndex={tab === activeTab ? 0 : -1}
              type="button"
              onClick={() => onTab(tab)}
              onKeyDown={handleTabKeyDown}
            >
              {tab}
            </button>
          ))}
        </div>
      )}

      {/* ── Memory list + calm inspector ─────────────── */}
      <section className="memory-view__workspace" id="memory-panel" role="tabpanel">
        <span className="ui-preview__view-state" hidden>{`· ${activeTab || 'All'}`}</span>
        <div className="memory-view__list">
          <div className="memory-view__list-header">
            <h2>{activeTab === 'Important' ? 'Important memories' :
                 activeTab === 'Topics' ? 'Topics' :
                 activeTab === 'Sessions' ? 'Memory by session' : 'Recent memories'}</h2>
            <span>{visibleRows.length} items</span>
          </div>
          <div className="memory-view__rows">
            {visibleRows.map((row, index) => (
              <button
                aria-pressed={index === selectedIndex}
                className={`memory-view__row ${index === selectedIndex ? 'is-selected' : ''}`}
                data-row-index={index}
                key={`${row[0]}-${index}`}
                type="button"
                onClick={() => onRow(index)}
              >
                <div className="memory-view__row-main">
                  <strong>{row[0]}</strong>
                  <small>{row[1]}</small>
                </div>
                <span className={`memory-view__importance memory-view__importance--${row[2].toLowerCase()}`}>
                  {row[2]}
                </span>
                <small className="memory-view__row-time">{row[3]}</small>
              </button>
            ))}
          </div>
        </div>

        <aside className="memory-view__inspector">
          <div className="memory-view__inspector-header">
            <h2>{selectedMemory[0]}</h2>
          </div>
          <dl className="memory-view__details">
            {[
              `View · ${activeTab}`,
              `Source · ${selectedMemory[1]}`,
              `Importance · ${selectedMemory[2]}`,
              ...screen.details.filter((detail) => !detail.startsWith('Importance ·')),
            ].map((detail) => {
              const [k, v] = detail.split(' · ');
              return (
                <div key={detail}>
                  <dt>{k}</dt>
                  <dd>{v || '—'}</dd>
                </div>
              );
            })}
          </dl>
          <div className="ui-preview__relations">
            <h3>Related views</h3>
            {(screen.relations || []).map((relation) => (
              <button key={relation} type="button" onClick={() => onRelation(relation)}>{relation}</button>
            ))}
          </div>
        </aside>
      </section>
    </main>
  );
}

export default MemoryView;
