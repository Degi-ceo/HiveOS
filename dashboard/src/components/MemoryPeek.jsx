import React, { useEffect, useState } from 'react';
import { useGateway } from '../hooks/useGateway';

export function MemoryPeek({ token, onClose }) {
  const { get } = useGateway(token);
  const [topics, setTopics] = useState([]);

  useEffect(() => {
    let mounted = true;
    get('/memory/topics')
      .then((d) => { if (mounted) setTopics(d.topics || []); })
      .catch(() => { if (mounted) setTopics([]); });
    return () => { mounted = false; };
  }, [get]);

  return (
    <div className="glass memory-peek" data-testid="memory-peek">
      <div className="memory-peek__head">
        <h3>memory</h3>
        {onClose && (
          <button onClick={onClose} aria-label="close" data-testid="memory-peek-close">×</button>
        )}
      </div>
      <ul>
        {topics.slice(0, 8).map((t) => (
          <li key={t.name} data-testid="memory-topic">
            <span>{t.name}</span>
            <span className="text-dim">{t.count}</span>
          </li>
        ))}
        {topics.length === 0 && (
          <li className="text-dim" data-testid="memory-empty">no topics</li>
        )}
      </ul>
    </div>
  );
}

export default MemoryPeek;