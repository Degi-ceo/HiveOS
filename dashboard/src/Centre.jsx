import { useState } from 'react';
import { SurfaceBar } from './components/SurfaceBar';
import { MemoryPeek } from './components/MemoryPeek';
import { SkillLauncher } from './components/SkillLauncher';
import { ActivityFeed } from './components/ActivityFeed';
import { SelfImprovementFeed } from './components/SelfImprovementFeed';
import { ChatCenter } from './components/ChatCenter';
import { VoiceToggle } from './components/VoiceToggle';
import { ApprovalModal } from './components/ApprovalModal';
import { StatusOrb } from './components/StatusOrb';
import { Nav } from './components/Nav';
import { CmdPalette } from './components/CmdPalette';
import { useNavState } from './hooks/useNavState';
import { useLongPress } from './hooks/useLongPress';
import { useCommandPalette } from './hooks/useCommandPalette';
import { useKeyboardShortcut } from './hooks/useKeyboardShortcut';

const BOTTOM_TABS = [
  { id: 'chat',    label: 'Chat' },
  { id: 'skills',  label: 'Skills' },
  { id: 'memory',  label: 'Memory' },
  { id: 'activity',label: 'Activity' },
  { id: 'more',   label: 'More' },
];

function HamburgerIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M3 12h18M3 6h18M3 18h18" />
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="5" r="1" fill="currentColor" />
      <circle cx="12" cy="12" r="1" fill="currentColor" />
      <circle cx="12" cy="19" r="1" fill="currentColor" />
    </svg>
  );
}

export function Centre({ token, sessionId }) {
  const [approval, setApproval] = useState(null);
  const [voiceText, setVoiceText] = useState('');
  const [activeTab, setActiveTab] = useState('chat');
  const { isNavOpen, openNav, closeNav } = useNavState();

  // CmdPalette state
  const {
    isOpen: isPaletteOpen,
    open: openPalette,
    close: closePalette,
  } = useCommandPalette(token);

  // Desktop: ⌘K / Ctrl+K opens command palette
  useKeyboardShortcut('k', openPalette, { metaKey: true });
  useKeyboardShortcut('k', openPalette, { ctrlKey: true });

  // StatusOrb long-press triggers nav open on mobile (⌘K equivalent)
  const { onPointerDown, onPointerUp, onPointerLeave } = useLongPress(openNav, 450);

  const handleTabClick = (id) => {
    if (id === 'more') {
      openNav();
    } else {
      setActiveTab(id);
    }
  };

  return (
    <div className="centre" data-testid="centre">
      {/* ── Top bar ── */}
      <div className="centre__top">
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {/* StatusOrb: tap = show nav (mobile), long-press = ⌘K */}
          <button
            type="button"
            className="hamburger-btn"
            onClick={openNav}
            aria-label="Open navigation"
          >
            <HamburgerIcon />
          </button>
          <StatusOrb
            state="ok"
            onPointerDown={onPointerDown}
            onPointerUp={onPointerUp}
            onPointerLeave={onPointerLeave}
          />
          <span className="nav__wordmark-inline">HIVE</span>
        </span>
        <SurfaceBar token={token} />
        <span style={{ display: 'flex', gap: 8 }}>
          <VoiceToggle onTranscript={setVoiceText} />
        </span>
      </div>

      {/* ── Sidebar Nav (desktop: always-on; mobile: drawer overlay) ── */}
      <Nav isOpen={isNavOpen} onClose={closeNav} />

      {/* ── Command palette (⌘K overlay) ── */}
      <CmdPalette isOpen={isPaletteOpen} onClose={closePalette} token={token} />

      {/* ── Left panel (SkillLauncher — desktop only; hidden behind Nav on mobile) ── */}
      <div className="centre__left glass" data-testid="centre-left">
        <SkillLauncher token={token} />
      </div>

      {/* ── Centre column ── */}
      <div className="centre__center" data-testid="centre-center">
        <ChatCenter
          token={token}
          sessionId={sessionId}
          onApproval={setApproval}
          voiceTranscript={voiceText}
        />
      </div>

      {/* ── Right panel ── */}
      <div className="centre__right" data-testid="centre-right">
        <MemoryPeek token={token} />
        <ActivityFeed token={token} />
        <SelfImprovementFeed token={token} />
      </div>

      {/* ── Bottom approval strip ── */}
      <div className="centre__bottom">
        <ApprovalModal token={token} request={approval} onClose={() => setApproval(null)} />
      </div>

      {/* ── Mobile bottom tab bar ── */}
      <nav className="bottom-tab-bar" aria-label="Mobile navigation">
        {BOTTOM_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`bottom-tab-bar__tab${activeTab === tab.id ? ' bottom-tab-bar__tab--active' : ''}`}
            onClick={() => handleTabClick(tab.id)}
            aria-label={tab.label}
            aria-current={activeTab === tab.id ? 'page' : undefined}
          >
            {tab.id === 'more' ? (
              <MoreIcon />
            ) : (
              <span className="bottom-tab-bar__icon" aria-hidden="true">
                {tab.id === 'chat' && (
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                    <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
                  </svg>
                )}
                {tab.id === 'skills' && (
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                    <path d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                )}
                {tab.id === 'memory' && (
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                    <path d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2v-4M9 21H5a2 2 0 01-2-2v-4m0-6h18v6H9z" />
                  </svg>
                )}
                {tab.id === 'activity' && (
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
                  </svg>
                )}
              </span>
            )}
            <span className="bottom-tab-bar__label">{tab.label}</span>
          </button>
        ))}
      </nav>

      <div className="scanline" />
    </div>
  );
}

export default Centre;
