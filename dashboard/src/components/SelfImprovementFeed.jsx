import React, { useEffect, useState } from 'react';
import { useGateway } from '../hooks/useGateway';

export function SelfImprovementFeed({ token }) {
  const { get } = useGateway(token);
  const [verdicts, setVerdicts] = useState([]);

  useEffect(() => {
    let mounted = true;
    get('/learning/history')
      .then((d) => {
        if (!mounted) return;
        const list = (d.loops || d.history || d.entries || []).filter((e) => e.verdict).slice(-5).reverse();
        setVerdicts(list);
      })
      .catch(() => { if (mounted) setVerdicts([]); });
    return () => { mounted = false; };
  }, [get]);

  return (
    <div className="glass self-improve" data-testid="self-improve">
      <h3>self-improve</h3>
      {verdicts.length === 0 && <div className="text-dim" data-testid="self-improve-empty">no verdicts yet</div>}
      <ul>
        {verdicts.map((v, i) => (
          <li key={i} data-verdict={v.verdict} data-testid="self-improve-item">
            {v.id || v.summary} <span className="text-dim">{v.verdict}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default SelfImprovementFeed;
