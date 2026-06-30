import React, { useState } from 'react';
import { SurfaceBar } from './components/SurfaceBar';
import { MemoryPeek } from './components/MemoryPeek';
import { SkillLauncher } from './components/SkillLauncher';
import { ActivityFeed } from './components/ActivityFeed';
import { SelfImprovementFeed } from './components/SelfImprovementFeed';
import { ChatCenter } from './components/ChatCenter';
import { VoiceToggle } from './components/VoiceToggle';
import { ApprovalModal } from './components/ApprovalModal';
import { StatusOrb } from './components/StatusOrb';

export function Centre({ token, sessionId }) {
  const [approval, setApproval] = useState(null);
  const [voiceText, setVoiceText] = useState('');
  return (
    <div className="centre" data-testid="centre">
      <div className="centre__top">
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <StatusOrb state="ok" /> HIVE
        </span>
        <SurfaceBar token={token} />
        <span style={{ display: 'flex', gap: 8 }}>
          <VoiceToggle onTranscript={setVoiceText} />
        </span>
      </div>
      <div className="centre__left glass" data-testid="centre-left">
        <SkillLauncher token={token} />
      </div>
      <div className="centre__center" data-testid="centre-center">
        <ChatCenter
          token={token}
          sessionId={sessionId}
          onApproval={setApproval}
          voiceTranscript={voiceText}
        />
      </div>
      <div className="centre__right" data-testid="centre-right">
        <MemoryPeek token={token} />
        <ActivityFeed token={token} />
        <SelfImprovementFeed token={token} />
      </div>
      <div className="centre__bottom">
        <ApprovalModal token={token} request={approval} onClose={() => setApproval(null)} />
      </div>
      <div className="scanline" />
    </div>
  );
}

export default Centre;
