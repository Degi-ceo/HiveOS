import { useState, useRef } from 'react';
import { useHaptic } from '../hooks/useHaptic';

// ── Icon SVGs (inline, 20×20 stroke-based, matching the cyan/glass aesthetic) ──

const Icon = ({ d, size = 20, ...props }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    {...props}
  >
    {d.split('|').map((path, i) => (
      <path key={i} d={path} />
    ))}
  </svg>
);

const icons = {
  home:      'M3 12l2-2m0 0l7-7 7 7|M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6',
  chat:      'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z',
  skills:    'M13 10V3L4 14h7v7l9-11h-7z',
  activity:  'M22 12h-4l-3 9L9 3l-3 9H2',
  voice:     'M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z|M19 10v2a7 7 0 01-14 0v-2|M12 19v3M8 22h8',
  memory:    'M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2v-4M9 21H5a2 2 0 01-2-2v-4m0-6h18v6H9z',
  kanban:    'M3 5h18v4H3zM3 13h18v4H3zM3 19h8v2H3z',
  agents:    'M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75',
  approvals: 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z',
  settings:  'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z|M15 12a3 3 0 11-6 0 3 3 0 016 0z',
  voiceRose: 'M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z|M19 10v2a7 7 0 01-14 0v-2M12 19v3M8 22h8',
};

// ── Nav item ──────────────────────────────────────────────────────────────────

function NavItem({ icon, label, active, onClick, showTooltip }) {
  const { vibrate } = useHaptic();

  const handleClick = () => {
    vibrate(10);
    onClick?.();
  };

  return (
    <div className={`nav-item${active ? ' nav-item--active' : ''}${showTooltip ? ' nav-item--tooltip' : ''}`}>
      <button
        type="button"
        className="nav-item__btn"
        onClick={handleClick}
        aria-label={label}
        aria-current={active ? 'page' : undefined}
      >
        <span className="nav-item__icon" aria-hidden="true">{icon}</span>
        <span className="nav-item__label">{label}</span>
      </button>
      {showTooltip && (
        <span className="nav-item__tooltip" role="tooltip">{label}</span>
      )}
    </div>
  );
}

// ── Nav group ─────────────────────────────────────────────────────────────────

function NavGroup({ title, items, activePath, onNavigate, showTooltips }) {
  return (
    <div className="nav-group">
      {title && <span className="nav-group__title">{title}</span>}
      <div className="nav-group__items">
        {items.map((item) => (
          <NavItem
            key={item.id}
            icon={item.icon}
            label={item.label}
            active={activePath === item.id}
            onClick={() => onNavigate(item.id)}
            showTooltip={showTooltips}
          />
        ))}
      </div>
    </div>
  );
}

// ── Main Nav component ─────────────────────────────────────────────────────────

const NAV_GROUPS = [
  {
    title: null,
    items: [
      { id: 'home',   label: 'Home',   icon: <Icon d={icons.home} /> },
      { id: 'chat',   label: 'Chat',   icon: <Icon d={icons.chat} /> },
      { id: 'skills', label: 'Skills', icon: <Icon d={icons.skills} /> },
    ],
  },
  {
    title: 'Live',
    items: [
      { id: 'activity', label: 'Activity', icon: <Icon d={icons.activity} /> },
      { id: 'voice',    label: 'Voice',    icon: <Icon d={icons.voice} /> },
    ],
  },
  {
    title: 'Workspace',
    items: [
      { id: 'memory', label: 'Memory', icon: <Icon d={icons.memory} /> },
      { id: 'kanban', label: 'Kanban', icon: <Icon d={icons.kanban} /> },
      { id: 'agents', label: 'Agents', icon: <Icon d={icons.agents} /> },
    ],
  },
  {
    title: 'System',
    items: [
      { id: 'approvals', label: 'Approvals', icon: <Icon d={icons.approvals} /> },
      { id: 'settings',  label: 'Settings',  icon: <Icon d={icons.settings} /> },
    ],
  },
];

export function Nav({ isOpen, onClose }) {
  const [activePath, setActivePath] = useState('home');
  const navRef = useRef(null);

  const handleNavigate = (id) => {
    setActivePath(id);
    // On mobile, close drawer after selection
    if (window.innerWidth < 768) onClose?.();
  };

  const handleBackdropClick = () => {
    onClose?.();
  };

  // Tooltips shown in desktop rail mode (768-1023px) or full sidebar (≥1024px)
  const isRail = typeof window !== 'undefined' && window.innerWidth >= 768 && window.innerWidth < 1024;

  return (
    <>
      {/* Mobile backdrop */}
      {!isRail && (
        <div
          className={`nav-backdrop${isOpen ? ' nav-backdrop--visible' : ''}`}
          onClick={handleBackdropClick}
          aria-hidden="true"
        />
      )}

      <nav
        ref={navRef}
        className={[
          'nav',
          isOpen ? 'nav--open' : '',
          isRail ? 'nav--rail' : '',
          window?.innerWidth < 768 ? 'nav--drawer' : '',
        ].filter(Boolean).join(' ')}
        aria-label="Main navigation"
        data-open={isOpen}
      >
        {/* Brand */}
        <div className="nav__brand">
          <span className="nav__wordmark">HIVE</span>
        </div>

        {/* Scrollable nav groups */}
        <div className="nav__groups">
          {NAV_GROUPS.map((group, i) => (
            <NavGroup
              key={i}
              title={group.title}
              items={group.items}
              activePath={activePath}
              onNavigate={handleNavigate}
              showTooltips={isRail}
            />
          ))}
        </div>

        {/* Voice button at bottom */}
        <div className="nav__footer">
          <button
            type="button"
            className="nav-voice-btn"
            aria-label="Voice"
            onClick={() => handleNavigate('voice')}
          >
            <span className="nav-voice-btn__icon">
              <Icon d={icons.voiceRose} size={22} />
            </span>
          </button>
        </div>
      </nav>
    </>
  );
}

export default Nav;
