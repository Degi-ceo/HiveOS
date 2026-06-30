import React, { useEffect, useRef, useState, useCallback } from 'react';
import { useGateway } from '../hooks/useGateway';
import { useWebSocket } from '../hooks/useWebSocket';

export function ChatCenter({ token, sessionId, onApproval, voiceTranscript }) {
  const { post } = useGateway(token);
  const { messages } = useWebSocket(token);
  const [input, setInput] = useState('');
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState(false);
  const processedIndexRef = useRef(0);

  useEffect(() => {
    for (let i = processedIndexRef.current; i < messages.length; i++) {
      const m = messages[i];
      if (m.event_type === 'token' || m.type === 'token') {
        const text = m.text || m.content || '';
        setHistory((h) => {
          const next = h.slice();
          const lastMsg = next[next.length - 1];
          if (lastMsg && lastMsg.role === 'assistant') {
            next[next.length - 1] = { role: 'assistant', text: (lastMsg.text || '') + text };
          } else {
            next.push({ role: 'assistant', text });
          }
          return next;
        });
      } else if (m.event_type === 'tool_call' || m.type === 'tool_call') {
        setHistory((h) => [...h, { role: 'tool', name: m.tool_name, args: m.args }]);
      } else if (m.event_type === 'approval_request' || m.type === 'approval') {
        onApproval?.(m);
      }
    }
    processedIndexRef.current = messages.length;
  }, [messages, onApproval]);

  useEffect(() => {
    if (voiceTranscript) setInput(voiceTranscript);
  }, [voiceTranscript]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;
    setHistory((h) => [...h, { role: 'user', text }]);
    setInput('');
    setBusy(true);
    try {
      await post('/chat', { message: text, session_id: sessionId });
    } catch (e) {
      setHistory((h) => [...h, { role: 'error', text: String(e) }]);
    } finally {
      setBusy(false);
    }
  }, [input, busy, post, sessionId]);

  return (
    <div className="glass chat-center" data-testid="chat-center" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="chat-history" data-testid="chat-history" style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
        {history.length === 0 && <div className="text-dim" data-testid="chat-empty">start a conversation</div>}
        {history.map((m, i) => (
          <div key={i} data-testid="chat-msg" data-role={m.role} className={`chat-msg chat-msg--${m.role}`}>
            {m.role === 'user' && <span><b>you:</b> {m.text}</span>}
            {m.role === 'assistant' && <span><b>hive:</b> {m.text}</span>}
            {m.role === 'tool' && <span className="text-dim">[tool: {m.name}]</span>}
            {m.role === 'error' && <span style={{ color: 'var(--rose)' }}>[error: {m.text}]</span>}
          </div>
        ))}
      </div>
      <div className="chat-input-row" style={{ display: 'flex', gap: 8, padding: 12, borderTop: '1px solid var(--glass)' }}>
        <input
          data-testid="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
          placeholder="ask hive…"
          disabled={busy}
          style={{ flex: 1, padding: 8, background: 'transparent', color: 'var(--text)', border: '1px solid var(--glass)', borderRadius: 8 }}
        />
        <button
          data-testid="chat-send"
          onClick={send}
          disabled={busy || !input.trim()}
          className="btn-primary"
        >
          {busy ? '…' : 'send'}
        </button>
      </div>
    </div>
  );
}

export default ChatCenter;
