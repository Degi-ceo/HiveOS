import { useState, useRef, useCallback, useEffect } from 'react';

function pickSR() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function detectSupported() {
  return pickSR() !== null;
}

export function useVoice() {
  const supported = detectSupported();
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState(null);
  const recRef = useRef(null);

  const start = useCallback(() => {
    if (!supported) {
      setError('not supported');
      return;
    }
    setError(null);
    setTranscript('');
    const SR = pickSR();
    const rec = new SR();
    rec.continuous = false;
    rec.interimResults = true;
    rec.onresult = (e) => {
      const text = Array.from(e.results).map((r) => r[0].transcript).join('');
      setTranscript(text);
    };
    rec.onerror = (e) => {
      setError(e.error || 'unknown');
      setListening(false);
    };
    rec.onend = () => setListening(false);
    try {
      rec.start();
      recRef.current = rec;
      setListening(true);
    } catch (err) {
      setError(err?.message || 'failed to start');
      setListening(false);
    }
  }, [supported]);

  const stop = useCallback(() => {
    try { recRef.current?.stop(); } catch { /* ignore */ }
  }, []);

  useEffect(() => () => {
    try { recRef.current?.stop(); } catch { /* ignore */ }
  }, []);

  return { supported, listening, transcript, error, start, stop };
}