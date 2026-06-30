import React, { useEffect, useState } from 'react';
import { useGateway } from '../hooks/useGateway';
import { StatusOrb } from './StatusOrb';

const CHANNELS = ['telegram', 'slack', 'discord', 'email'];

export function SurfaceBar({ token }) {
  const { get } = useGateway(token);
  const [channels, setChannels] = useState({});

  useEffect(() => {
    let mounted = true;
    const tick = () => {
      get('/health/summary')
        .then((h) => { if (mounted) setChannels(h.channels || {}); })
        .catch(() => { if (mounted) setChannels({}); });
    };
    tick();
    const id = setInterval(tick, 30000);
    return () => { mounted = false; clearInterval(id); };
  }, [get]);

  return (
    <div className="surface-bar" data-testid="surface-bar">
      {CHANNELS.map((c) => {
        const active = !!channels[c];
        return (
          <span key={c} title={c} data-active={active} data-channel={c}>
            <StatusOrb state={active ? 'ok' : 'idle'} />
            {c}
          </span>
        );
      })}
    </div>
  );
}

export default SurfaceBar;