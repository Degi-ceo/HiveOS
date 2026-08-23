import { useEffect, useMemo, useState } from 'react';
import { defaultScreenId, navigationGroups, screens } from './screenCatalog';
import { HubView } from './HubView';
import { MemoryView } from './MemoryView';
import { TasksView } from './TasksView';
import { ApprovalsView } from './ApprovalsView';
import { DomainView } from './DomainView';
import './ui-preview.css';

const overlayRoutes = ['Global overlay', 'Global panel'];
const domainViewKinds = new Set([
  'chat', 'skills', 'files', 'agents', 'channels', 'mcp', 'logs', 'activity',
  'sessions', 'self-improve', 'analytics', 'docs', 'settings', 'agent-detail',
]);

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

function isOverlayScreen(screen) {
  return screen.route.includes('overlay') || screen.route.includes('drawer') || overlayRoutes.includes(screen.route);
}

function tabFromQuery(screen, value) {
  if (!value) return screen.defaultTab || screen.tabs[0];
  return screen.tabs.find((tab) => slugify(tab) === value) || screen.defaultTab || screen.tabs[0];
}

function relationTarget(relation) {
  const value = relation.toLowerCase();
  const matches = [
    ['release-log', 'release-log'], ['approvals/:id', 'approval-modal'], ['/approval/', 'approval-modal'],
    ['/traces/', 'trace-detail'], ['/agents/', 'agent-detail'], ['/self-improve', 'self-improve'],
    ['/commitments', 'commitments'], ['/cron', 'cron'], ['/analytics', 'analytics'], ['/sessions', 'sessions'],
    ['/activity', 'activity'], ['/approvals', 'approvals'], ['/channels', 'channels'], ['/settings', 'settings'],
    ['/memory', 'memory'], ['/skills', 'skills'], ['/files', 'files'], ['/agents', 'agents'],
    ['/tasks', 'tasks'], ['/chat', 'chat'], ['/logs', 'logs'], ['/docs', 'docs'], ['/mcp', 'mcp'],
    ['/hub', 'hub'],
  ];
  return matches.find(([needle]) => value.includes(needle))?.[1] || null;
}

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
                aria-current={active === id ? 'page' : undefined}
                aria-label={screens[id].navLabel}
                className={`ui-preview__nav-item ${active === id ? 'is-active' : ''}`}
                key={id}
                title={screens[id].navLabel}
                type="button"
                onClick={() => onSelect(id)}
              >
                <span className="ui-preview__nav-copy">{screens[id].navLabel}</span>
                <span className="ui-preview__nav-short" aria-hidden="true">{screens[id].navLabel.slice(0, 2)}</span>
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

function PreviewPage({
  screen,
  screenId,
  activeTab,
  selectedRow,
  onAction,
  onClose,
  onRelation,
  onRow,
  onTab,
}) {
  const isOverlay = isOverlayScreen(screen);
  const section = activeTab
    ? screen.tabSections[activeTab] || `${screen.section} · ${activeTab}`
    : screen.section;
  const selected = screen.rows[selectedRow];
  const statusTone = /protected|review|unread|draft/i.test(screen.statusLabel) ? 'is-warning' : 'is-healthy';
  const details = selectedRow >= 0 && selected
    ? [`Selected · ${selected[0]}`, ...screen.details]
    : screen.details;

  return (
    <main
      className={`ui-preview__main ${screen.presentation === 'mobile' ? 'is-mobile-presentation' : ''}`}
      data-active-tab={activeTab || ''}
      data-view={`screen-${screenId}`}
    >
      <header className="ui-preview__header">
        <div className="ui-preview__title-block">
          <button className="ui-preview__menu" aria-label="Open mobile navigation" type="button" onClick={() => onAction('mobile-nav')}>☰</button>
          <div><h1>{screen.title}</h1><p>{screen.subtitle}</p></div>
        </div>
        <div className="ui-preview__header-actions">
          <button aria-label="Open notifications" type="button" className="ui-preview__icon-button" onClick={() => onAction('notifications')}>●</button>
          <button type="button" className="ui-preview__search" onClick={() => onAction('command-palette')}>Search or run a command <kbd>⌘K</kbd></button>
          {isOverlay && <button type="button" className="ui-preview__secondary" onClick={onClose}>Close</button>}
          {screen.action && <button type="button" className="ui-preview__primary" onClick={() => onAction(screen.primaryTarget)}>{screen.action}</button>}
        </div>
      </header>

      <div className="ui-preview__dev-note">
        <span>CONCEPT PREVIEW</span>
        <code>{screen.route}</code>
        {activeTab && <span className="ui-preview__view-state">View · {activeTab}</span>}
        <span>Static fixtures only · No backend calls</span>
      </div>

      <Metrics items={screen.metrics} />

      {screen.tabs.length > 0 && (
        <div className="ui-preview__tabs" role="tablist" aria-label={`${screen.title} views`}>
          {screen.tabs.map((tab) => (
            <button
              aria-controls={`${screenId}-panel`}
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
                const tabs = [...event.currentTarget.parentElement.querySelectorAll('[role="tab"]')];
                const current = tabs.indexOf(event.currentTarget);
                const next = event.key === 'Home' ? 0
                  : event.key === 'End' ? tabs.length - 1
                    : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
                tabs[next].focus();
                onTab(screen.tabs[next]);
              }}
            >
              {tab}
            </button>
          ))}
        </div>
      )}

      <section
        aria-labelledby={activeTab ? `${screenId}-${slugify(activeTab)}-tab` : undefined}
        className={`ui-preview__workspace ${isOverlay ? 'is-overlay' : ''}`}
        id={`${screenId}-panel`}
        role={activeTab ? 'tabpanel' : undefined}
      >
        <div className="ui-preview__surface ui-preview__surface--main">
          <div className="ui-preview__surface-title"><h2>{section}</h2><span>{screen.rows.length} items</span></div>
          <div className="ui-preview__rows">
            {screen.rows.map((row, index) => (
              <div
                aria-current={index === selectedRow ? 'true' : undefined}
                className={`ui-preview__row ${index === selectedRow ? 'is-selected' : ''}`}
                key={`${row[0]}-${index}`}
              >
                <span className="ui-preview__row-icon">{String(index + 1).padStart(2, '0')}</span>
                <div className="ui-preview__row-copy"><strong>{row[0]}</strong><small>{row[1]}</small></div>
                <span className="ui-preview__row-meta">{row[2]}</span>
                {row[3] && <button type="button" data-row-index={index} onClick={(event) => { event.stopPropagation(); onRow(index, screen.rowTargets[index]); }}>{row[3]}</button>}
              </div>
            ))}
          </div>
        </div>

        <aside className="ui-preview__surface ui-preview__surface--details">
          <div className="ui-preview__surface-title"><h2>{selectedRow >= 0 && selected ? selected[0] : screen.detailsTitle}</h2><span className={`ui-preview__status ${statusTone}`}>{screen.statusLabel}</span></div>
          <div className="ui-preview__detail-list">
            {details.map((detail) => <div key={detail}><span>{detail}</span><i /></div>)}
          </div>
          <div className="ui-preview__contract">
            <h3>Backend contract</h3>
            {screen.api.map((endpoint) => <code className={endpoint.startsWith('GAP') ? 'is-gap' : ''} key={endpoint}>{endpoint}</code>)}
          </div>
          <div className="ui-preview__relations">
            <h3>Related views</h3>
            {screen.relations.map((relation) => (
              <button key={relation} type="button" onClick={() => onRelation(relation)}>{relation}</button>
            ))}
          </div>
        </aside>
      </section>
      {screen.presentation === 'mobile' && (
        <nav className="ui-preview__device-nav" aria-label="Mockup device navigation">
          {['hub', 'chat', 'tasks', 'activity', 'settings'].map((id) => (
            <button key={id} type="button" onClick={() => onAction(id)}>{screens[id].navLabel}</button>
          ))}
        </nav>
      )}
    </main>
  );
}

export function UiPreview() {
  const initialParams = new URLSearchParams(window.location.search);
  const requested = initialParams.get('screen');
  const initialScreenId = screens[requested] ? requested : defaultScreenId;
  const [screenId, setScreenId] = useState(initialScreenId);
  const [tabByScreen, setTabByScreen] = useState(() => ({
    [initialScreenId]: tabFromQuery(screens[initialScreenId], initialParams.get('tab')),
  }));
  const [selectedRowByScreen, setSelectedRowByScreen] = useState({});
  const [returnScreenId, setReturnScreenId] = useState(defaultScreenId);
  const [notice, setNotice] = useState('');
  const screen = screens[screenId];
  const activeTab = useMemo(() => tabByScreen[screenId] || screen.defaultTab || screen.tabs[0], [screen, screenId, tabByScreen]);
  const selectedRow = selectedRowByScreen[screenId] ?? -1;

  const updateUrl = (id, tab) => {
    const url = new URL(window.location.href);
    url.searchParams.set('ui-preview', '1');
    url.searchParams.set('screen', id);
    if (tab) url.searchParams.set('tab', slugify(tab));
    else url.searchParams.delete('tab');
    window.history.pushState({}, '', url);
  };

  const selectScreen = (id) => {
    if (!screens[id]) return;
    if (isOverlayScreen(screens[id]) && !isOverlayScreen(screen)) setReturnScreenId(screenId);
    setScreenId(id);
    setNotice('');
    updateUrl(id, tabByScreen[id] || screens[id].defaultTab || screens[id].tabs[0]);
  };

  const selectTab = (tab) => {
    const target = screen.tabTargets[tab];
    if (target) {
      selectScreen(target);
      return;
    }
    setTabByScreen((current) => ({ ...current, [screenId]: tab }));
    updateUrl(screenId, tab);
  };

  const performAction = (target) => {
    if (target && screens[target]) {
      selectScreen(target);
      return;
    }
    setNotice(`${screen.action || 'Action'} is intentionally fixture-only in this preview.`);
  };

  const selectRow = (index, target = null) => {
    setSelectedRowByScreen((current) => ({ ...current, [screenId]: index }));
    if (target) selectScreen(target);
  };

  useEffect(() => {
    const onPopState = () => {
      const params = new URLSearchParams(window.location.search);
      const nextId = screens[params.get('screen')] ? params.get('screen') : defaultScreenId;
      setScreenId(nextId);
      setTabByScreen((current) => ({ ...current, [nextId]: tabFromQuery(screens[nextId], params.get('tab')) }));
    };
    const onKeyDown = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        selectScreen('command-palette');
      } else if (event.key === 'Escape' && isOverlayScreen(screen)) {
        selectScreen(returnScreenId);
      }
    };
    window.addEventListener('popstate', onPopState);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('popstate', onPopState);
      window.removeEventListener('keydown', onKeyDown);
    };
  });

  return (
    <div
      className={`ui-preview ${screen.presentation === 'mobile' ? 'is-mobile-preview' : ''}`}
      data-screen={screenId}
      data-testid="ui-preview"
    >
      <Sidebar active={screenId} onSelect={selectScreen} />
      {screen.kind === 'hub' ? (
        <HubView
          screen={screen}
          onAction={performAction}
          onRow={selectRow}
          onRelation={(relation) => {
            const target = relationTarget(relation);
            if (target) selectScreen(target);
            else setNotice(`Relationship "${relation}" is documented but has no standalone mockup.`);
          }}
        />
      ) : screen.kind === 'memory' ? (
        <MemoryView
          screen={screen}
          activeTab={activeTab}
          selectedRow={selectedRow}
          onAction={performAction}
          onRelation={(relation) => {
            const target = relationTarget(relation);
            if (target) selectScreen(target);
            else setNotice(`Relationship "${relation}" is documented but has no standalone mockup.`);
          }}
          onRow={selectRow}
          onTab={selectTab}
        />
      ) : screen.kind === 'tasks' ? (
        <TasksView
          screen={screen}
          activeTab={activeTab}
          selectedRow={selectedRow}
          onAction={performAction}
          onRelation={(relation) => {
            const target = relationTarget(relation);
            if (target) selectScreen(target);
            else setNotice(`Relationship "${relation}" is documented but has no standalone mockup.`);
          }}
          onRow={selectRow}
          onTab={selectTab}
        />
      ) : screen.kind === 'approvals' ? (
        <ApprovalsView
          screen={screen}
          activeTab={activeTab}
          onAction={performAction}
          onRelation={(relation) => {
            const target = relationTarget(relation);
            if (target) selectScreen(target);
            else setNotice(`Relationship "${relation}" is documented but has no standalone mockup.`);
          }}
          onRow={selectRow}
          onTab={selectTab}
        />
      ) : domainViewKinds.has(screen.kind) ? (
        <DomainView
          screen={screen}
          screenId={screenId}
          activeTab={activeTab}
          selectedRow={selectedRow}
          onAction={performAction}
          onRelation={(relation) => {
            const target = relationTarget(relation);
            if (target) selectScreen(target);
            else setNotice(`Relationship "${relation}" is documented but has no standalone mockup.`);
          }}
          onRow={selectRow}
          onTab={selectTab}
        />
      ) : (
        <PreviewPage
          screen={screen}
          screenId={screenId}
          activeTab={activeTab}
          selectedRow={selectedRow}
          onAction={performAction}
          onClose={() => selectScreen(returnScreenId)}
          onRelation={(relation) => {
            const target = relationTarget(relation);
            if (target) selectScreen(target);
            else setNotice(`Relationship "${relation}" is documented but has no standalone mockup.`);
          }}
          onRow={selectRow}
          onTab={selectTab}
        />
      )}
      <nav className="ui-preview__mobile-nav" aria-label="Mobile UI preview navigation">
        {['hub', 'chat', 'tasks', 'activity', 'settings'].map((id) => {
          // Mobile variants map to their base screen id for active highlighting
          const isActive = screenId === id || screenId === `mobile-${id}`;
          return (
            <button aria-current={isActive ? 'page' : undefined} className={isActive ? 'is-active' : ''} key={id} type="button" onClick={() => selectScreen(id)}>
              {screens[id].navLabel}
            </button>
          );
        })}
      </nav>
      {notice && <div className="ui-preview__notice" role="status" aria-live="polite">{notice}<button type="button" aria-label="Dismiss message" onClick={() => setNotice('')}>×</button></div>}
    </div>
  );
}

export default UiPreview;
