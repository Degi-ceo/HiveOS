import { useEffect, useRef, useState, useCallback } from 'react';

export function useWebSocket(token, path = '/ws/dashboard') {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('connecting');
  const [reconnectKey, setReconnectKey] = useState(0);
  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const reconnectTimerRef = useRef(null);

  useEffect(() => {
    const url = `${path}?token=${encodeURIComponent(token || '')}`;
    const fullUrl = url.includes('://') ? url : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${url}`;
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
      setStatus('open');
      retryRef.current = 0;
    };
    ws.onmessage = (e) => {
      if (cancelled) return;
      try {
        const parsed = JSON.parse(e.data);
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