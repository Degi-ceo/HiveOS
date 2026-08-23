import './ui-preview.css';

/**
 * ApprovalsView — Safety-focused review interface.
 *
 * Three key visual signals:
 * 1. Risk banner at the top (REVIEW / MANUAL / PROTECTED)
 * 2. Pending items grouped by risk tone (red REVIEW, amber MANUAL, red PROTECTED)
 * 3. Safety policy panel showing what is locked / open
 *
 * Deliberately distinguishes pending (actionable) from edits log (read-only history).
 */

function riskTone(s) {
  const l = (s || '').toLowerCase();
  if (l.includes('review')) return 'review';
  if (l.includes('manual')) return 'manual';
  if (l.includes('protected')) return 'protected';
  return 'safe';
}

const EDIT_LOG_ROWS = [
  ['Gateway restart rejected', 'PROTECTED', 'Kamil · 34m ago', ''],
  ['Memory export approved', 'REVIEW', 'Kamil · 2h ago', ''],
  ['Deployment config cancelled', 'MANUAL', 'Hive · yesterday', ''],
];

export function ApprovalsView({ screen, activeTab, onAction, onRelation, onRow, onTab }) {
  const isEditsLog = activeTab === 'Edits log';
  const visibleRows = isEditsLog ? EDIT_LOG_ROWS : screen.rows;

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

  return (
    <main className="approvals-view" data-active-tab={activeTab} data-view="approval-safety">
      {/* ── Header ─────────────────────────────────────── */}
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

      {/* ── Risk banner ──────────────────────────────── */}
      <div className="approvals-view__risk-banner" role={isEditsLog ? 'status' : 'alert'}>
        <div className="approvals-view__risk-icon" aria-hidden="true">!</div>
        <div>
          <strong>{isEditsLog ? 'Immutable decision history' : screen.details.filter(d => d.startsWith('Pending'))[0] || '3 pending'}</strong>
          <small>{isEditsLog ? 'Every approval, rejection and cancellation remains auditable' : `${screen.details.filter(d => d.startsWith('Oldest'))[0] || 'Oldest 21m'} · Review every protected or manual operation before approval`}</small>
        </div>
      </div>

      {/* ── Tabs ────────────────────────────────────── */}
      {screen.tabs.length > 0 && (
        <div className="ui-preview__tabs" role="tablist" aria-label="Approvals views">
          {screen.tabs.map((tab) => (
            <button
              aria-controls="approvals-panel"
              aria-selected={tab === activeTab}
              className={tab === activeTab ? 'is-active' : ''}
              id={`approvals-${tab.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-tab`}
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

      {/* ── Pending items ────────────────────────────── */}
      <section className="approvals-view__list" id="approvals-panel" role="tabpanel">
        <span className="ui-preview__view-state" hidden>{`· ${activeTab || 'Pending'}`}</span>
        <div className="approvals-view__list-header">
          <h2>{isEditsLog ? 'Decision history' : 'Waiting for review'}</h2>
          <span>{visibleRows.length} items</span>
        </div>
        <div className="approvals-view__rows">
          {visibleRows.map((row, index) => {
            const tone = riskTone(row[1]);
            return (
              <article key={`${row[0]}-${index}`} className={`approvals-view__row approvals-view__row--${tone}`}>
                <div className="approvals-view__row-main">
                  <strong>{row[0]}</strong>
                  <small>{row[2]}</small>
                </div>
                <span className={`approvals-view__risk approvals-view__risk--${tone}`}>{row[1]}</span>
                {row[3] && (
                  <button type="button" className="approvals-view__action" data-row-index={index} onClick={() => onRow(index, screen.rowTargets[index])}>
                    {row[3]}
                  </button>
                )}
              </article>
            );
          })}
        </div>
      </section>

      {/* ── Safety policy panel ──────────────────────── */}
      <aside className="approvals-view__policy">
        <h3>Safety policy</h3>
        <ul>
          {screen.details.map((detail) => (
            <li key={detail}>
              <span>{detail.split(' · ')[0]}</span>
              <span>{detail.split(' · ')[1] || '—'}</span>
            </li>
          ))}
        </ul>
      </aside>

      <div className="ui-preview__relations">
        <h3>Related views</h3>
        {(screen.relations || []).map((relation) => (
          <button key={relation} type="button" onClick={() => onRelation(relation)}>{relation}</button>
        ))}
      </div>
    </main>
  );
}

export default ApprovalsView;
