import React, { useEffect } from 'react';
import { useVoice } from '../hooks/useVoice';

export function VoiceToggle({ onTranscript }) {
  const { supported, listening, transcript, start, stop, error } = useVoice();
  useEffect(() => {
    if (transcript) onTranscript?.(transcript);
  }, [transcript, onTranscript]);
  if (!supported) {
    return <span className="text-dim" data-testid="voice-unsupported">mic —</span>;
  }
  return (
    <button
      data-testid="voice-toggle"
      data-listening={listening ? 'true' : 'false'}
      onClick={listening ? stop : start}
      className="voice-toggle"
      title={error || (listening ? 'stop' : 'speak')}
    >
      {listening ? '■' : '\u{1F399}'}
    </button>
  );
}

export default VoiceToggle;
