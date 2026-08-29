const STATE_TITLE = {
  idle: 'idle',
  working: 'working',
  ok: 'healthy',
  error: 'error',
  warn: 'warning',
};

export function StatusOrb({ state = 'idle', onPointerDown, onPointerUp, onPointerLeave }) {
  const cls = state === 'error' ? 'orb error' : state === 'warn' ? 'orb warn' : 'orb';
  return (
    <div
      className={cls}
      data-state={state}
      title={STATE_TITLE[state] || state}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerLeave}
    />
  );
}

export default StatusOrb;
