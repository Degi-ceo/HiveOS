import { useEffect, useRef, useState, useCallback } from 'react';

export function useWebSocket(token, path = '/ws/dashboard') {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('connecting');
  const [reconnectKey, setReconnectKey] = useState(0);
  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const reconnectTimerRef = useRef(null);

  useEffect(() => {
    // Auth contract: gateway `/ws/dashboard` reads the token as the FIRST TEXT
    // FRAME via `await websocket.receive_text()` — NOT from URL query string.
    // Sending the token in the URL would leak it to browser history, proxy
    // access logs, and Referer headers, and would never reach the server-side
    // validator (the server blocks on receive_text() forever).
    const fullUrl = path.includes('://') ? path : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;
    let cancelled = false;
    let ws;
    try {
      ws = new WebSocket(fullUrl);
    } catch {
      setStatus('error');
      return undefined;
    }
    wsRef.current = ws;
    ws.onopen = () => {
      if (cancelled) return;
      // Send token as first text frame (gateway validates then sends events).
      try { ws.send(token || ''); } catch { /* ignore */ }
      setStatus('open');
      retryRef.current = 0;
    };
    ws.onmessage = (e) => {
      if (cancelled) return;
      try {
        const parsed = JSON.parse(e.data);
        // Skip gateway auth-error reply (the connection will close right after).
        if (parsed?.type === 'error' && parsed?.data === 'unauthorized') {
          setStatus('error');
          return;
        }
        setMessages((m) => [...m.slice(-99), parsed]);
      } catch {
        /* ignore non-JSON */
      }
    };
    ws.onerror = () => {
      if (cancelled) return;
      setStatus('error');
    };
    ws.onclose = () => {
      if (cancelled) return;
      setStatus('closed');
      const delay = Math.min(30000, 1000 * 2 ** retryRef.current++);
      reconnectTimerRef.current = setTimeout(() => {
        if (cancelled) return;
        setReconnectKey((k) => k + 1);
      }, delay);
    };
    return () => {
      cancelled = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      try { ws.close(); } catch { /* ignore */ }
    };
  }, [token, path, reconnectKey]);

  const send = useCallback((data) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
    }
  }, []);

  return { messages, status, send };
}