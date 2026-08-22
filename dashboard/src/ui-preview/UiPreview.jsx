import React, { useMemo, useState } from 'react';
import { defaultScreenId, navigationGroups, screens } from './screenCatalog';
import './ui-preview.css';

function Mark() {
  return <span className="ui-preview__mark" aria-hidden="true">H</span>;
}

function Sidebar({ active, onSelect }) {
  return (
    <aside className="ui-preview__sidebar">
      <div className="ui-preview__brand"><Mark /><strong>HiveOS</strong></div>
      <button className="ui-preview__new" type="button" onClick={() => onSelect('new-task')}>＋ New task</button>
      <nav aria-label="UI preview screens">
        {navigationGroups.map((group) => (
          <div className="ui-preview__nav-group" key={group.label || 'top'}>
            {group.label && <div className="ui-preview__nav-label">{group.label}</div>}
            {group.items.map((id) => (
              <button
                className={`ui-preview__nav-item ${active === id ? 'is-active' : ''}`}
                key={id}
                type="button"
                onClick={() => onSelect(id)}
              >
                <span>{screens[id].navLabel}</span>
                {id === 'approvals' && <span className="ui-preview__count">1</span>}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className="ui-preview__profile">
        <span className="ui-preview__online" />
        <div><strong>Hive online</strong><small>MiniMax M2.7</small></div>
      </div>
    </aside>
  );
}

function Metrics({ items }) {
  if (!items.length) return null;
  return (
    <div className={`ui-preview__metrics ${items.length === 2 ? 'is-two' : ''}`}>
      {items.map(([label, value, meta]) => (
        <article className="ui-preview__metric" key={label}>
          <span>{label}</span><strong>{value}</strong><small>{meta}</small>
        </article>
      ))}
    </div>
  );
}

function PreviewPage({ screen, activeTab, onTab }) {
  const isOverlay = screen.route.includes('overlay') || screen.route === 'Global panel' || screen.route === 'Global overlay';
  return (
    <main className="ui-preview__main">
      <header className="ui-preview__header">
        <div><h1>{screen.title}</h1><p>{screen.subtitle}</p></div>
        <div className="ui-preview__header-actions">
          <button type="button" className="ui-preview__search">Search or run a command <kbd>⌘K</kbd></button>
          {screen.action && <button type="button" className="ui-preview__primary">{screen.action}</button>}
        </div>
      </header>

      <div className="ui-preview__dev-note">
        <span>UI PLACEHOLDER</span>
        <code>{screen.route}</code>
        <span>No backend calls</span>
      </div>

      <Metrics items={screen.metrics} />

      {screen.tabs.length > 0 && (
        <div className="ui-preview__tabs" role="tablist">
          {screen.tabs.map((tab) => (
            <button className={tab === activeTab ? 'is-active' : ''} key={tab} type="button" onClick={() => onTab(tab)}>{tab}</button>
          ))}
        </div>
      )}

      <section className={`ui-preview__workspace ${isOverlay ? 'is-overlay' : ''}`}>
        <div className="ui-preview__surface ui-preview__surface--main">
          <div className="ui-preview__surface-title"><h2>{screen.section}</h2><span>{screen.rows.length} items</span></div>
          <div className="ui-preview__rows">
            {screen.rows.map((row, index) => (
              <div className={`ui-preview__row ${index === 0 ? 'is-selected' : ''}`} key={`${row[0]}-${index}`}>
                <span className="ui-preview__row-icon">{String(index + 1).padStart(2, '0')}</span>
                <div className="ui-preview__row-copy"><strong>{row[0]}</strong><small>{row[1]}</small></div>
                <span className="ui-preview__row-meta">{row[2]}</span>
                {row[3] && <button type="button">{row[3]}</button>}
              </div>
            ))}
          </div>
        </div>

        <aside className="ui-preview__surface ui-preview__surface--details">
          <div className="ui-preview__surface-title"><h2>{screen.detailsTitle}</h2><span className="ui-preview__healthy">Healthy</span></div>
          <div className="ui-preview__detail-list">
            {screen.details.map((detail) => <div key={detail}><span>{detail}</span><i /></div>)}
          </div>
          <div className="ui-preview__contract">
            <h3>Backend contract</h3>
            {screen.api.map((endpoint) => <code className={endpoint.startsWith('GAP') ? 'is-gap' : ''} key={endpoint}>{endpoint}</code>)}
          </div>
          <div className="ui-preview__relations">
            <h3>Related views</h3>
            {screen.relations.map((relation) => <span key={relation}>{relation}</span>)}
          </div>
        </aside>
      </section>
    </main>
  );
}

export function UiPreview() {
  const requested = new URLSearchParams(window.location.search).get('screen');
  const [screenId, setScreenId] = useState(screens[requested] ? requested : defaultScreenId);
  const screen = screens[screenId];
  const [tabByScreen, setTabByScreen] = useState({});
  const activeTab = useMemo(() => tabByScreen[screenId] || screen.tabs[0], [screen, screenId, tabByScreen]);

  const selectScreen = (id) => {
    setScreenId(id);
    const url = new URL(window.location.href);
    url.searchParams.set('ui-preview', '1');
    url.searchParams.set('screen', id);
    window.history.replaceState({}, '', url);
  };

  return (
    <div className="ui-preview" data-testid="ui-preview">
      <Sidebar active={screenId} onSelect={selectScreen} />
      <PreviewPage
        screen={screen}
        activeTab={activeTab}
        onTab={(tab) => setTabByScreen((current) => ({ ...current, [screenId]: tab }))}
      />
      <nav className="ui-preview__mobile-nav" aria-label="Mobile UI preview navigation">
        {['hub', 'chat', 'tasks', 'activity', 'settings'].map((id) => (
          <button className={screenId === id ? 'is-active' : ''} key={id} type="button" onClick={() => selectScreen(id)}>
            {screens[id].navLabel}
          </button>
        ))}
      </nav>
    </div>
  );
}

export default UiPreview;
