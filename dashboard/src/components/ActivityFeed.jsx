import React from 'react';
import { useWebSocket } from '../hooks/useWebSocket';

const TOOL_EVENTS = new Set(['tool_call', 'a2a.call.started', 'a2a.call.completed', 'a2a.call.failed']);

export function ActivityFeed({ token }) {
  const { messages } = useWebSocket(token);
  const calls = messages
    .filter((m) => TOOL_EVENTS.has(m.event_type) || TOOL_EVENTS.has(m.type))
    .slice(-10)
    .reverse();
  return (
    <div className="glass activity-feed" data-testid="activity-feed">
      <h3>activity</h3>
      {calls.length === 0 && <div className="text-dim" data-testid="activity-empty">no activity yet</div>}
      <ul>
        {calls.map((c, i) => (
          <li key={i} className="text-dim" data-testid="activity-item">
            {c.event_type || c.type} {c.tool_name || c.agent_name || ''}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default ActivityFeed;
