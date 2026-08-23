import './ui-preview.css';

/**
 * TasksView — Kanban work management surface.
 *
 * Three tab views map to:
 *   - Kanban: column-based task board by status
 *   - Cron: schedule-focused list with cadence/health
 *   - Promises: recurring obligation tracker
 *
 * Tasks screen has `tabTargets` that route to dedicated screens (cron/commitments).
 * This component handles the default Kanban view; cron and promises redirect.
 */

const STATUS_COLUMNS = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'in-progress', label: 'In progress' },
  { id: 'review', label: 'Review' },
  { id: 'done', label: 'Done' },
];

function classifyStatus(s) {
  const l = (s || '').toLowerCase();
  if (l === 'backlog') return 'backlog';
  if (l === 'in progress' || l === 'running') return 'in-progress';
  if (l === 'review') return 'review';
  if (l === 'done' || l === 'completed') return 'done';
  return 'backlog';
}

export function TasksView({ screen, activeTab, selectedRow, onAction, onRelation, onRow, onTab }) {
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
    <main className="tasks-view" data-active-tab={activeTab} data-view="task-kanban">
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

      {/* ── Status counters (over the kanban) ────────── */}
      <section className="tasks-view__status-bar" aria-label="Queue status">
        {STATUS_COLUMNS.map((col) => {
          const count = screen.rows.filter((r) => classifyStatus(r[1]) === col.id).length;
          return (
            <div key={col.id} className={`tasks-view__status tasks-view__status--${col.id}`}>
              <span>{col.label}</span>
              <strong>{count}</strong>
            </div>
          );
        })}
      </section>

      {/* ── Tabs ───────────────────────────────────────── */}
      {screen.tabs.length > 0 && (
        <div className="ui-preview__tabs" role="tablist" aria-label="Tasks views">
          {screen.tabs.map((tab) => (
            <button
              aria-controls="tasks-panel"
              aria-selected={tab === activeTab}
              className={tab === activeTab ? 'is-active' : ''}
              id={`tasks-${tab.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-tab`}
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

      {/* ── Kanban board ──────────────────────────────── */}
      <section className="tasks-view__kanban" id="tasks-panel" role="tabpanel">
        <span className="ui-preview__view-state" hidden>{`· ${activeTab || 'Kanban'}`}</span>
        {STATUS_COLUMNS.map((col) => {
          const items = screen.rows
            .map((row, rowIndex) => ({ row, rowIndex }))
            .filter(({ row }) => classifyStatus(row[1]) === col.id);
          return (
            <div key={col.id} className={`tasks-view__column tasks-view__column--${col.id}`}>
              <div className="tasks-view__column-header">
                <h3>{col.label}</h3>
                <span>{items.length}</span>
              </div>
              <div className="tasks-view__cards">
                {items.map(({ row, rowIndex }) => (
                  <button
                    aria-pressed={rowIndex === selectedRow}
                    data-row-index={rowIndex}
                    type="button"
                    className={`tasks-view__card ${rowIndex === selectedRow ? 'is-selected' : ''}`}
                    key={`${row[0]}-${rowIndex}`}
                    onClick={() => onRow(rowIndex, screen.rowTargets[rowIndex])}
                  >
                    <strong>{row[0]}</strong>
                    <small>{row[2]}</small>
                    {row[3] && <span className="tasks-view__card-action">{row[3]}</span>}
                  </button>
                ))}
              </div>
            </div>
          );
        })}
      </section>

      <aside className="tasks-view__selection" aria-live="polite">
        <span>Selected task</span>
        <strong>{selectedRow >= 0 ? screen.rows[selectedRow]?.[0] : 'Choose a task card'}</strong>
        <small>{selectedRow >= 0 ? screen.rows[selectedRow]?.[2] : 'No selection'}</small>
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

export default TasksView;
