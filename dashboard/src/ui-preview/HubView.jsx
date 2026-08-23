import './ui-preview.css';

/**
 * Status semantic colors using CSS custom properties
 * green  = healthy / good
 * amber  = attention / warning
 * red    = critical
 */
function statusClass(label) {
  const l = (label || '').toLowerCase();
  if (l.includes('healthy') || l.includes('good') || l.includes('connected') || l.includes('running') || l.includes('active')) return 'is-healthy';
  if (l.includes('warning') || l.includes('review') || l.includes('waiting') || l.includes('attention') || l.includes('degraded') || l.includes('pending')) return 'is-warning';
  if (l.includes('critical') || l.includes('error') || l.includes('failed') || l.includes('offline')) return 'is-critical';
  return 'is-neutral';
}

/**
 * Individual status dot
 */
function StatusDot({ tone }) {
  return <span className={`hub__dot hub__dot--${tone}`} aria-hidden="true" />;
}

/* ── Primary tile (large numeral) ─────────────────────────── */
function PrimaryTile({ label, value, meta, tone = 'is-healthy', onClick }) {
  const Component = onClick ? 'button' : 'article';
  return (
    <Component
      className={`hub__tile hub__tile--primary hub__tile--${tone}`}
      onClick={onClick}
      type={onClick ? 'button' : undefined}
    >
      <div className="hub__tile-header">
        <span className="hub__tile-label">{label}</span>
        <StatusDot tone={tone} />
      </div>
      <strong className="hub__tile-value">{value}</strong>
      <small className="hub__tile-meta">{meta}</small>
    </Component>
  );
}

/* ── Secondary tile (medium) ───────────────────────────────── */
function SecondaryTile({ label, value, meta, accent, onClick }) {
  const Component = onClick ? 'button' : 'article';
  return (
    <Component
      className={`hub__tile hub__tile--secondary ${accent ? 'hub__tile--accent' : ''}`}
      onClick={onClick}
      type={onClick ? 'button' : undefined}
    >
      <div className="hub__tile-header">
        <span className="hub__tile-label">{label}</span>
      </div>
      <strong className="hub__tile-value">{value}</strong>
      {meta && <small className="hub__tile-meta">{meta}</small>}
    </Component>
  );
}

/* ── Attention item (Needs attention row) ─────────────────── */
function AttentionItem({ title, type, status, action, onAction, rowIndex }) {
  const typeClass = type === 'Approval' ? 'hub__attention-item--approval'
    : type === 'Task' ? 'hub__attention-item--task'
    : 'hub__attention-item--usage';

  return (
    <div className={`hub__attention-item ${typeClass}`}>
      <div className="hub__attention-item-copy">
        <strong>{title}</strong>
        <span className="hub__attention-item-type">{type}</span>
      </div>
      <div className="hub__attention-item-status">
        <span className="hub__attention-item-status-text">{status}</span>
        {action && (
          <button type="button" className="hub__attention-action" data-row-index={rowIndex} onClick={onAction}>
            {action}
          </button>
        )}
      </div>
    </div>
  );
}

/* ── Active now card ──────────────────────────────────────── */
function ActiveNowCard({ title, index, onClick }) {
  return (
    <button className="hub__active-card" type="button" onClick={onClick}>
      <span className="hub__active-card-index">{String(index).padStart(2, '0')}</span>
      <strong className="hub__active-card-title">{title}</strong>
      <span className="hub__active-card-status">
        <StatusDot tone="is-healthy" />
        Running
      </span>
    </button>
  );
}

/* ── HubView ─────────────────────────────────────────────── */
export function HubView({ screen, onAction, onRow, onRelation }) {
  const { metrics, rows, details, section } = screen;

  // Parse primary metrics
  const [gatewayLabel, gatewayValue, gatewayMeta] = metrics[0] || [];
  const [agentsLabel, agentsValue, agentsMeta] = metrics[1] || [];
  const [memoryLabel, memoryValue, memoryMeta] = metrics[2] || [];
  const [approvalsLabel, approvalsValue, approvalsMeta] = metrics[3] || [];

  // Status tones for primary tiles
  const gatewayTone = statusClass(gatewayValue);
  const agentsTone = statusClass(agentsValue);
  const memoryTone = statusClass(memoryValue);
  const approvalsTone = statusClass(approvalsValue);

  // Secondary row tiles
  const recentActivityMeta = 'Last event 12s ago';
  const quickActionsMeta = '4 available';
  const connectedServicesMeta = '3 of 4 healthy';

  // Tertiary row
  const modelBudgetMeta = '78% of daily limit';
  const sessionCountMeta = '18 active today';

  return (
    <main className="hub" data-view="hub-dashboard">
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

      {/* ── Dev note ──────────────────────────────────── */}
      <div className="ui-preview__dev-note">
        <span>CONCEPT PREVIEW</span>
        <code>{screen.route}</code>
        <span>Static fixtures only · No backend calls</span>
      </div>

      {/* ── Primary row: 4 system tiles ───────────────────── */}
      <section className="hub__primary-row" aria-label="System overview">
        <PrimaryTile
          label={gatewayLabel}
          value={gatewayValue}
          meta={gatewayMeta}
          tone={gatewayTone}
          onClick={() => onAction && onAction('logs')}
        />
        <PrimaryTile
          label={agentsLabel}
          value={agentsValue}
          meta={agentsMeta}
          tone={agentsTone}
          onClick={() => onAction && onAction('agents')}
        />
        <PrimaryTile
          label={memoryLabel}
          value={memoryValue}
          meta={memoryMeta}
          tone={memoryTone}
          onClick={() => onAction && onAction('memory')}
        />
        <PrimaryTile
          label={approvalsLabel}
          value={approvalsValue}
          meta={approvalsMeta}
          tone={approvalsTone}
          onClick={() => onAction && onAction('approvals')}
        />
      </section>

      {/* ── Needs attention ───────────────────────────────── */}
      <section className="hub__attention-section" aria-labelledby="needs-attention-heading">
        <div className="hub__section-header">
          <h2 className="hub__section-title" id="needs-attention-heading">
            <span className="hub__attention-icon" aria-hidden="true">!</span>
            {section}
          </h2>
          <span className="hub__attention-count">{rows.length} items</span>
        </div>
        <div className="hub__attention-list">
          {rows.map((row, i) => (
            <AttentionItem
              key={`${row[0]}-${i}`}
              title={row[0]}
              type={row[1]}
              status={row[2]}
              action={row[3]}
              rowIndex={i}
              onAction={() => onRow && onRow(i, screen.rowTargets[i])}
            />
          ))}
        </div>
      </section>

      {/* ── Secondary row: 3 overview tiles ────────────────── */}
      <section className="hub__secondary-row" aria-label="Quick overview">
        <SecondaryTile
          label="Recent activity"
          value={details[0] || 'Gateway retry refactor'}
          meta={recentActivityMeta}
          accent
          onClick={() => onAction && onAction('activity')}
        />
        <SecondaryTile
          label="Quick actions"
          value={quickActionsMeta}
          meta="Tasks · Chat · New task"
          onClick={() => onAction && onAction('new-task')}
        />
        <SecondaryTile
          label="Connected services"
          value={connectedServicesMeta}
          meta="Telegram · Discord · Email"
          onClick={() => onAction && onAction('channels')}
        />
      </section>

      {/* ── Tertiary row ──────────────────────────────────── */}
      <section className="hub__tertiary-row" aria-label="Resource usage">
        <SecondaryTile
          label="Model budget"
          value={modelBudgetMeta}
          meta="MiniMax M2.7 · GPT-5.6"
          onClick={() => onAction && onAction('analytics')}
        />
        <SecondaryTile
          label="Sessions today"
          value={sessionCountMeta}
          meta="248k tokens processed"
          onClick={() => onAction && onAction('sessions')}
        />
      </section>

      {/* ── Active now ────────────────────────────────── */}
      <section className="hub__active-section" aria-labelledby="active-now-heading">
        <div className="hub__section-header">
          <h2 className="hub__section-title" id="active-now-heading">{screen.detailsTitle}</h2>
        </div>
        <div className="hub__active-grid">
          {(details || []).map((title, i) => (
            <ActiveNowCard key={title} title={title} index={i + 1} onClick={() => onAction && onAction('agents')} />
          ))}
        </div>
      </section>

      {/* ── Related views ───────────────────────────────── */}
      <aside className="ui-preview__relations">
        <h3>Related views</h3>
        {(screen.relations || []).map((relation) => (
          <button key={relation} type="button" onClick={() => onRelation && onRelation(relation)}>{relation}</button>
        ))}
      </aside>
    </main>
  );
}

export default HubView;
