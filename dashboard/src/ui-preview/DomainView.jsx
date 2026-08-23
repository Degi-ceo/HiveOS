import './ui-preview.css';

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

function Header({ screen, onAction }) {
  return (
    <>
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
        <span>Static fixtures only · No backend calls</span>
      </div>
    </>
  );
}

function Tabs({ screen, screenId, activeTab, onTab }) {
  if (!screen.tabs.length) return null;
  return (
    <div className="ui-preview__tabs" role="tablist" aria-label={`${screen.title} views`}>
      {screen.tabs.map((tab, index) => (
        <button
          aria-controls={`${screenId}-domain-panel`}
          aria-selected={tab === activeTab}
          className={tab === activeTab ? 'is-active' : ''}
          id={`${screenId}-${slugify(tab)}-tab`}
          key={tab}
          role="tab"
          tabIndex={tab === activeTab ? 0 : -1}
          type="button"
          onClick={() => onTab(tab)}
          onKeyDown={(event) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === 'Home' ? 0
              : event.key === 'End' ? screen.tabs.length - 1
                : (index + (event.key === 'ArrowRight' ? 1 : -1) + screen.tabs.length) % screen.tabs.length;
            event.currentTarget.parentElement.querySelectorAll('[role="tab"]')[next].focus();
            onTab(screen.tabs[next]);
          }}
        >
          {tab}
        </button>
      ))}
    </div>
  );
}

function Relations({ relations, onRelation }) {
  return (
    <aside className="ui-preview__relations domain-view__relations">
      <h3>Related views</h3>
      {relations.map((relation) => <button key={relation} type="button" onClick={() => onRelation(relation)}>{relation}</button>)}
    </aside>
  );
}

function Inspector({ screen, selected }) {
  return (
    <aside className="domain-view__inspector">
      <header><h2>{selected?.[0] || screen.detailsTitle}</h2><span>{screen.statusLabel}</span></header>
      <dl>
        {(selected ? [`Context · ${selected[1]}`, `State · ${selected[2]}`, ...screen.details] : screen.details).map((detail) => {
          const [label, ...rest] = detail.split(' · ');
          return <div key={detail}><dt>{label}</dt><dd>{rest.join(' · ') || 'Available'}</dd></div>;
        })}
      </dl>
      <section className="domain-view__contract"><h3>Backend contract</h3>{screen.api.map((item) => <code key={item}>{item}</code>)}</section>
    </aside>
  );
}

function ChatBody({ screen, activeTab, onRow }) {
  if (activeTab === 'Run details') {
    return (
      <section className="domain-view__chat-run" aria-label="Run execution details">
        {screen.rows.slice(1).map((row, index) => (
          <button data-row-index={index + 1} key={row[0]} type="button" onClick={() => onRow(index + 1, screen.rowTargets[index + 1])}>
            <span>{String(index + 1).padStart(2, '0')}</span><strong>{row[0]}</strong><small>{row[1]}</small><em>{row[2]}</em>
          </button>
        ))}
      </section>
    );
  }
  return (
    <section className="domain-view__conversation" aria-label="Conversation messages">
      {screen.rows.map((row, index) => (
        <article className={index === 0 ? 'is-assistant' : 'is-system'} key={`${row[0]}-${index}`}>
          <span>{row[0]}</span><p>{row[1]}</p><small>{row[2]}</small>
        </article>
      ))}
      <div className="domain-view__composer"><span>Message Hive…</span><button type="button">Send</button></div>
    </section>
  );
}

function CardGrid({ screen, activeTab, onRow }) {
  let rows = screen.rows.map((row, rowIndex) => ({ row, rowIndex }));
  if (screen.kind === 'skills' && activeTab !== 'All') {
    rows = rows.filter(({ row }) => row[2].toLowerCase() === activeTab.toLowerCase() || activeTab === 'Pinned' && row[2] === 'Pinned');
  }
  if (screen.kind === 'agents' && activeTab === 'Active now') rows = rows.filter(({ row }) => row[2] === 'Running');
  if (screen.kind === 'channels' && activeTab !== 'Webhooks') rows = rows.filter(({ row }) => row[0] === activeTab);
  if (screen.kind === 'channels' && activeTab === 'Webhooks') rows = [{ row: ['Deployment callback', 'Signed webhook', 'Last delivery 2m ago', 'Manage'], rowIndex: 0 }];
  if (screen.kind === 'sessions' && activeTab === 'Errors') rows = [{ row: ['Failed gateway diagnosis', 'GPT-5.6', 'Timeout · 3.1k tokens', 'Open'], rowIndex: 0 }];

  return (
    <section className={`domain-view__cards domain-view__cards--${screen.kind}`} aria-label={`${activeTab || screen.section} items`}>
      {rows.map(({ row, rowIndex }) => (
        <button data-row-index={rowIndex} key={`${row[0]}-${rowIndex}`} type="button" onClick={() => onRow(rowIndex, screen.rowTargets[rowIndex])}>
          <span className="domain-view__card-kicker">{row[2]}</span>
          <strong>{row[0]}</strong>
          <small>{row[1]}</small>
          <em>{row[3] || 'Inspect'}</em>
        </button>
      ))}
    </section>
  );
}

function FilesBody({ screen, activeTab, selectedRow, onRow }) {
  const safeIndex = selectedRow < 0 ? 0 : Math.min(selectedRow, screen.rows.length - 1);
  const selected = screen.rows[safeIndex];
  return (
    <section className="domain-view__files" aria-label={`${activeTab} files`}>
      <nav aria-label="Workspace folders"><strong>Workspace</strong>{['Config', 'docs', 'dashboard'].map((name) => <button key={name} type="button">▸ {name}</button>)}</nav>
      <div className="domain-view__file-list"><header><h2>{activeTab}</h2><span>{screen.rows.length} items</span></header>{screen.rows.map((row, index) => <button aria-pressed={index === selectedRow} data-row-index={index} key={row[0]} type="button" onClick={() => onRow(index)}><strong>{row[0]}</strong><small>{row[1]} · {row[2]}</small></button>)}</div>
      <article className="domain-view__file-preview"><span>READ-ONLY PREVIEW</span><h2>{selected[0]}</h2><p>{selected[1]} selected from the HiveOS workspace. Content remains fixture-only in this concept.</p><code>{screen.api[1]}</code></article>
    </section>
  );
}

function LogsBody({ screen, activeTab }) {
  return (
    <section className="domain-view__logs" aria-label={`${activeTab} log stream`}>
      <header><span className="is-live">● LIVE</span><strong>{activeTab}</strong><small>Level INFO+</small></header>
      {screen.rows.map((row, index) => <div key={`${row[0]}-${index}`}><time>{row[0]}</time><b className={row[1] === 'WARN' ? 'is-warning' : ''}>{row[1]}</b><span>{row[2]}</span><code>{row[3]}</code></div>)}
    </section>
  );
}

function ActivityBody({ screen, activeTab, onRow }) {
  return (
    <section className="domain-view__timeline" aria-label={`${activeTab} event timeline`}>
      {screen.rows.map((row, index) => <button data-row-index={index} key={`${row[0]}-${index}`} type="button" onClick={() => onRow(index, screen.rowTargets[index])}><i /><span><strong>{row[0]}</strong><small>{row[1]}</small></span><em>{row[2]}</em></button>)}
    </section>
  );
}

const ANALYTICS_SERIES = {
  Cost: [['MiniMax M2.7', 58], ['GPT-5.6', 31], ['Codex', 11]],
  Tokens: [['Input', 72], ['Output', 43], ['Cached', 28]],
  Sessions: [['Completed', 86], ['Active', 24], ['Failed', 6]],
  'Skill usage': [['system-health', 74], ['memory-recovery', 49], ['github-pr-review', 33]],
  Errors: [['Gateway', 8], ['Tools', 4], ['Models', 2]],
};

function AnalyticsBody({ activeTab }) {
  const series = ANALYTICS_SERIES[activeTab] || ANALYTICS_SERIES.Cost;
  return <section className="domain-view__analytics" aria-label={`${activeTab} analytics`}><article><span>Last 24 hours</span><h2>{activeTab}</h2>{series.map(([label, value]) => <div key={label}><strong>{label}</strong><i><span style={{ width: `${value}%` }} /></i><em>{value}</em></div>)}</article><aside><span>Budget status</span><strong>£4.82</strong><small>£1.38 remaining today</small></aside></section>;
}

function DocsBody({ screen, selectedRow, onRow }) {
  const safeIndex = selectedRow < 0 ? 0 : Math.min(selectedRow, screen.rows.length - 1);
  const selected = screen.rows[safeIndex];
  return <section className="domain-view__docs"><nav aria-label="Documentation files">{screen.rows.map((row, index) => <button aria-current={index === selectedRow ? 'page' : undefined} data-row-index={index} key={row[0]} type="button" onClick={() => onRow(index)}>{row[0]}</button>)}</nav><article><span>{selected[2]}</span><h2>{selected[0]}</h2><p>HiveOS documentation preview with readable line length, deep-linkable sections and explicit protection markers.</p><h3>{selected[1]}</h3><p>This concept keeps operational documentation close to the system surfaces it explains.</p></article></section>;
}

function SettingsBody({ screen, activeTab, selectedRow, onRow }) {
  let rows = screen.rows.map((row, rowIndex) => ({ row, rowIndex }));
  if (activeTab === 'Personal') rows = rows.filter(({ row }) => row[1] === 'Personal');
  if (activeTab === 'System') rows = rows.filter(({ row }) => row[1] === 'System');
  if (activeTab === 'Account') rows = [{ row: ['Access and sessions', 'Account', 'Passkeys · 3 active sessions', 'Open'], rowIndex: 0 }];
  return <section className="domain-view__settings" aria-label={`${activeTab} settings`}>{rows.map(({ row, rowIndex }) => <article className={selectedRow === rowIndex ? 'is-selected' : ''} key={`${activeTab}-${row[0]}`}><div><strong>{row[0]}</strong><small>{row[2]}</small></div><button aria-pressed={selectedRow === rowIndex} data-row-index={rowIndex} type="button" onClick={() => onRow(rowIndex, screen.rowTargets[rowIndex])}>{row[3]}</button></article>)}<aside><strong>Protected configuration</strong><p>Secrets stay masked. SOUL.md and the approval gate remain read-only.</p></aside></section>;
}

function SelfImproveBody({ screen, activeTab, onRow }) {
  return <section className="domain-view__self-improve"><div className="domain-view__pipeline">{['Diagnose', 'Propose', 'Test', 'Review', 'Release'].map((step, index) => <span className={index < 3 ? 'is-complete' : ''} key={step}><i>{index + 1}</i>{step}</span>)}</div><CardGrid screen={screen} activeTab={activeTab} onRow={onRow} /></section>;
}

function AgentDetailBody({ screen, activeTab, onRow }) {
  return <section className="domain-view__agent-detail"><header><span>CO</span><div><h2>coder</h2><p>Running · Gateway retry refactor</p></div><em>98.2% success</em></header><CardGrid screen={screen} activeTab={activeTab} onRow={onRow} /></section>;
}

export function DomainView({ screen, screenId, activeTab, selectedRow, onAction, onRelation, onRow, onTab }) {
  const selected = screen.rows[Math.min(selectedRow, screen.rows.length - 1)];
  let body;
  if (screen.kind === 'chat') body = <ChatBody screen={screen} activeTab={activeTab} onRow={onRow} />;
  else if (screen.kind === 'files') body = <FilesBody screen={screen} activeTab={activeTab} selectedRow={selectedRow} onRow={onRow} />;
  else if (screen.kind === 'logs') body = <LogsBody screen={screen} activeTab={activeTab} />;
  else if (screen.kind === 'activity') body = <ActivityBody screen={screen} activeTab={activeTab} onRow={onRow} />;
  else if (screen.kind === 'analytics') body = <AnalyticsBody activeTab={activeTab} />;
  else if (screen.kind === 'docs') body = <DocsBody screen={screen} selectedRow={selectedRow} onRow={onRow} />;
  else if (screen.kind === 'settings') body = <SettingsBody screen={screen} activeTab={activeTab} selectedRow={selectedRow} onRow={onRow} />;
  else if (screen.kind === 'self-improve') body = <SelfImproveBody screen={screen} activeTab={activeTab} onRow={onRow} />;
  else if (screen.kind === 'agent-detail') body = <AgentDetailBody screen={screen} activeTab={activeTab} onRow={onRow} />;
  else body = <CardGrid screen={screen} activeTab={activeTab} onRow={onRow} />;

  return (
    <main className={`ui-preview__main domain-view domain-view--${screen.kind}`} data-active-tab={activeTab || ''} data-view={`${screen.kind}-workspace`}>
      <Header screen={screen} onAction={onAction} />
      <Tabs screen={screen} screenId={screenId} activeTab={activeTab} onTab={onTab} />
      <section aria-labelledby={activeTab ? `${screenId}-${slugify(activeTab)}-tab` : undefined} className="domain-view__body" id={`${screenId}-domain-panel`} role={activeTab ? 'tabpanel' : undefined}>
        {activeTab && <span className="ui-preview__view-state" hidden>{`· ${activeTab}`}</span>}
        {activeTab && <span className="domain-view__active-view">View · {activeTab}</span>}
        <div className="domain-view__primary">{body}</div>
        {!['files', 'analytics', 'docs', 'settings'].includes(screen.kind) && <Inspector screen={screen} selected={selectedRow >= 0 ? selected : null} />}
      </section>
      <Relations relations={screen.relations} onRelation={onRelation} />
      {screen.presentation === 'mobile' && (
        <nav className="ui-preview__device-nav" aria-label="Mockup device navigation">
          {['Hub', 'Chat', 'Tasks', 'Activity', 'Settings'].map((label) => (
            <button className={label.toLowerCase() === screen.kind ? 'is-active' : ''} key={label} type="button" onClick={() => onAction(label.toLowerCase())}>{label}</button>
          ))}
        </nav>
      )}
    </main>
  );
}

export default DomainView;
