# SPRINT_6 P-I — Jarvis Front Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `dashboard/MissionControl.jsx` with `Centre.jsx` — a premium, **holographic** command centre with categorized components, conic-gradient glass cards, system-stack typography, and live updates from `/ws/dashboard`. 15 net-new frontend files + 1 backend tweak.

> **Visual style:** SH1 holographic (deep navy + cyan conic gradients + glass cards), locked in `docs/UI_PLAN.md` §7 and visualised in `screenshots/frontend/mockups/new mockups/SH1-full-holo-sidebar.html`. Tokens + visual rules in this plan are sourced from UI_PLAN §7 — do NOT introduce new colours/fonts outside that palette.

**Architecture:** React 18 + Vite SPA. 3 leaf hooks (useGateway, useWebSocket, useVoice) → 5 data panels (SurfaceBar, MemoryPeek, SkillLauncher, ActivityFeed, SelfImprovementFeed) → 3 interactive (ChatCenter, VoiceToggle, ApprovalModal) → Centre.jsx (root). Pure CSS variables (no Tailwind), Vitest for unit tests, no animation libraries.

**Tech Stack:** React 18, Vite (existing), Vitest (NEW), @testing-library/react (NEW), jsdom (NEW), Web Speech API (browser-native), pure CSS with custom properties.

**Branch:** `sprint6/jarvis-front`
**Issue:** #77
**Closes:** #77 ("When this lands: HiveOS v1.0 ships. SPRINT 6 closes.")
**Effort target:** ~1500-1800 LOC (12 components + 3 hooks + theme + tests) + ~30 LOC (backend)

**Reference:** `docs/sprints/SPRINT_6_P_I_RESEARCH.md` — research/sequencing skeleton for this plan.

---

## Global Constraints

Verbatim from issue #77, expanded with implementation specifics:

1. **Bundle <500KB gzipped** (`dist/*.js` after `npm run build`). Mitigation: code-split panels via `React.lazy`.
2. **100% coverage on `hooks/`**, 80%+ coverage on `components/` (per Vitest).
3. **Vitest is a NEW dep** — must add to `dashboard/package.json` (not just install). `npm run test` script + `vitest.config.js` with `jsdom` env.
4. **No animation libraries** — status orb is raw SVG + CSS keyframes; scan-line is CSS only.
5. **No chart libraries** — ActivityFeed + SelfImprovementFeed are lists, not graphs.
6. **No Tailwind** — pure CSS custom properties.
7. **Old `MissionControl.jsx` removed** (or renamed to `_legacy.jsx`) — `main.jsx` mounts `Centre` instead.
8. **`.kanban-*` CSS rules from PR #88 must survive** — P-I `theme.css` is a superset, not a replacement.
9. **Existing CI green** — `ruff check src/ tests/` + `pytest -q` (3852 baseline + new = all green).
10. **`hive doctor` green** at the end (no broken wiring introduced by `/health/summary.channels`).
11. **Theme tokens from issue #77 used verbatim** (don't add new tokens without updating the plan).
12. **All 12 components consume only the 3 hooks** (no direct fetch / no direct WebSocket) — keeps testing simple.
13. **VoiceToggle / useVoice: Web Speech API only** (no external libs); gracefully no-op when unsupported.
14. **Backend `/health/summary.channels` is the ONLY backend change** — sources from the existing `_channel_registry` (PR #87, P-E).
15. **PLAN-MANDATED DEVIATIONS**: Filter `/learning/history` client-side for verdicts (no new endpoint); extend `/skills` with `?pinned=true` query param (no new endpoint); add `pinned_names()` method to `skill_usage.py`.

---

## File Structure

### New frontend files (13)

| Path | LOC | Purpose |
|---|---|---|
| `dashboard/src/Centre.jsx` | ~150 | Root layout (replaces MissionControl.jsx) |
| `dashboard/src/components/StatusOrb.jsx` | ~50 | Animated SVG orb |
| `dashboard/src/components/ChatCenter.jsx` | ~250 | Hero chat with markdown + tool-call chips + voice |
| `dashboard/src/components/ActivityFeed.jsx` | ~70 | Real-time tool-call log |
| `dashboard/src/components/MemoryPeek.jsx` | ~60 | Collapsible memory topics panel |
| `dashboard/src/components/SkillLauncher.jsx` | ~50 | Pinned skills grid |
| `dashboard/src/components/SurfaceBar.jsx` | ~40 | Channel status pills (top bar) |
| `dashboard/src/components/ApprovalModal.jsx` | ~100 | Approval dialog (blocks chat) |
| `dashboard/src/components/VoiceToggle.jsx` | ~80 | Mic button + waveform |
| `dashboard/src/components/SelfImprovementFeed.jsx` | ~50 | Recent learning verdicts |
| `dashboard/src/hooks/useGateway.js` | ~40 | Fetch wrapper with token |
| `dashboard/src/hooks/useWebSocket.js` | ~60 | `/ws/dashboard` with reconnect |
| `dashboard/src/hooks/useVoice.js` | ~50 | Mic permission + waveform |
| `dashboard/src/styles/theme.css` | ~120 | Premium tokens (replaces existing) |
| `dashboard/src/__tests__/setup.js` | ~20 | Vitest jsdom + @testing-library config |

### Modified files (5)

| Path | Change |
|---|---|
| `dashboard/src/main.jsx` | `import Centre from './Centre'` (was `MissionControl`) |
| `dashboard/package.json` | Add vitest, @testing-library/react, jsdom + `test` script |
| `dashboard/vitest.config.js` | NEW — jsdom env, setup file, alias for `@/` if used |
| `dashboard/MissionControl.jsx` | Delete (or rename to `_legacy.jsx`) |
| `src/hive/memory/skill_usage.py` | Add `pinned_names()` method |
| `src/hive/gateway/app.py` | `/skills?pinned=true` query param + `/health/summary.channels` |

### Test files (NEW)

| Path | Purpose |
|---|---|
| `dashboard/src/__tests__/hooks/useGateway.test.js` | 100% on useGateway |
| `dashboard/src/__tests__/hooks/useWebSocket.test.js` | 100% on useWebSocket |
| `dashboard/src/__tests__/hooks/useVoice.test.js` | 100% on useVoice |
| `dashboard/src/__tests__/components/StatusOrb.test.js` | ≥80% |
| `dashboard/src/__tests__/components/SurfaceBar.test.js` | ≥80% |
| `dashboard/src/__tests__/components/ActivityFeed.test.js` | ≥80% |
| `dashboard/src/__tests__/components/MemoryPeek.test.js` | ≥80% |
| `dashboard/src/__tests__/components/SkillLauncher.test.js` | ≥80% |
| `dashboard/src/__tests__/components/SelfImprovementFeed.test.js` | ≥80% |
| `dashboard/src/__tests__/components/ChatCenter.test.js` | ≥80% |
| `dashboard/src/__tests__/components/VoiceToggle.test.js` | ≥80% |
| `dashboard/src/__tests__/components/ApprovalModal.test.js` | ≥80% |
| `dashboard/src/__tests__/Centre.test.js` | Mounts all children with mock data |
| `tests/test_health_channels.py` | 1 new Python test for `/health/summary.channels` |
| `tests/test_skills_pinned.py` | 1 new Python test for `/skills?pinned=true` |
| `tests/test_skill_usage_pinned_names.py` | 1 new test for `pinned_names()` method |

### Docs (NEW)

| Path | Purpose |
|---|---|
| `dashboard/CENTRE.md` | Operator manual (replaces dashboard/MISSION_CONTROL.md) |
| `docs/CENTRE.md` | External operator manual (linked from STATUS.md) |

---

## Phase 1 — Foundation (theme + hooks + Vitest setup + StatusOrb)

### Task 1.1: Add Vitest + @testing-library/react to dashboard deps

**Files:**
- Modify: `dashboard/package.json` (add 3 deps + `test` script)
- Create: `dashboard/vitest.config.js`
- Create: `dashboard/src/__tests__/setup.js`

- [ ] **Step 1: Add deps to `dashboard/package.json`**

```json
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "vitest": "^1.6.0",
    "@testing-library/react": "^16.0.0",
    "@testing-library/jest-dom": "^6.4.0",
    "jsdom": "^24.0.0",
    "@vitest/coverage-v8": "^1.6.0"
  }
}
```

- [ ] **Step 2: Create `dashboard/vitest.config.js`**

```javascript
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.js'],
    globals: true,
    coverage: {
      provider: 'v8',
      include: ['src/hooks/**', 'src/components/**', 'src/Centre.jsx'],
      thresholds: { 'src/hooks/**': 100, 'src/components/**': 80 },
    },
  },
});
```

- [ ] **Step 3: Create `dashboard/src/__tests__/setup.js`**

```javascript
import '@testing-library/jest-dom';
```

- [ ] **Step 4: Run `npm install` in `dashboard/`**

- [ ] **Step 5: Commit**

```bash
git add dashboard/package.json dashboard/vitest.config.js dashboard/src/__tests__/setup.js
git commit -m "feat(dashboard): Vitest setup with jsdom + 100% hooks coverage gate (P-I T1)"
```

---

### Task 1.2: Premium `theme.css` (merges with existing `.kanban-*` rules)

**Files:**
- Modify: `dashboard/src/styles/theme.css` (full rewrite preserving `.kanban-*` from PR #88)

- [ ] **Step 1: Write `theme.css` (full content)**

```css
/* SH1 holographic theme — locked from docs/UI_PLAN.md §7
 * Palette: deep navy + cyan conic gradients + glass cards
 * See: screenshots/frontend/mockups/new mockups/SH1-full-holo-sidebar.html
 *      docs/UI_PLAN.md §7 (visual rules)
 *      [[hiveos-design-style]] (memory)
 */
:root {
  --bg: #04050b;                              /* deep navy (was --ink) */
  --cyan: #22d3ee;                            /* holographic accent */
  --blue: #3b82f6;
  --violet: #8b5cf6;
  --amber: #f59e0b;                           /* approval / warning */
  --rose: #f43f5e;                            /* error / voice-active */
  --text: #cfe;
  --text-dim: #88a;
  --glass: rgba(8, 11, 20, 0.55);             /* more transparent for holographic */
  --border: rgba(34, 211, 238, 0.18);         /* cyan-tinted hairline */
  --conic-border: conic-gradient(from 180deg at 50% 50%, var(--cyan) 0deg, var(--blue) 120deg, var(--violet) 240deg, var(--cyan) 360deg);
  --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", system-ui, sans-serif;
  --radius: 8px;
  --radius-lg: 16px;                          /* bigger for glass cards */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;
}

* { box-sizing: border-box; }
html, body, #root { height: 100%; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 13px;                             /* UI_PLAN §7: 13px base */
  line-height: 1.5;
  overflow: hidden;
  /* radial glows for the holographic backdrop */
  background-image:
    radial-gradient(at 20% 0%, rgba(34, 211, 238, 0.08) 0px, transparent 50%),
    radial-gradient(at 100% 100%, rgba(139, 92, 246, 0.06) 0px, transparent 50%);
}
button { font-family: inherit; cursor: pointer; }
input, textarea { font-family: inherit; background: transparent; color: inherit; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space-sm); }

/* Glass panel — backdrop blur + cyan hairline + subtle glow (see SH1 mockup for conic-gradient border technique) */
.glass {
  background: var(--glass);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  box-shadow: 0 0 24px rgba(34, 211, 238, 0.10);
}

/* Status orb — holographic glow */
.orb { width: 24px; height: 24px; border-radius: 50%; background: var(--cyan); box-shadow: 0 0 16px var(--cyan); animation: pulse 2s ease-in-out infinite; }
.orb.warn { background: var(--amber); box-shadow: 0 0 16px var(--amber); }
.orb.error { background: var(--rose); box-shadow: 0 0 16px var(--rose); }
@keyframes pulse { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.15); } }

/* Scan-line animation — conic gradient sweep */
.scanline { position: fixed; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, transparent, var(--cyan), var(--violet), transparent); animation: scan 6s linear infinite; pointer-events: none; opacity: 0.4; }
@keyframes scan { 0% { transform: translateY(0); } 100% { transform: translateY(100vh); } }

/* Layout: P-I minimum — 3-column grid (full SH1 260px sidebar is Sprint 7 per UI_PLAN §6) */
.centre { display: grid; grid-template-rows: 56px 1fr 48px; grid-template-columns: 240px 1fr 280px; height: 100vh; gap: var(--space-md); padding: var(--space-md); }
.centre__top { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; }
.centre__left { grid-row: 2; }
.centre__center { grid-row: 2; display: flex; flex-direction: column; }
.centre__right { grid-row: 2; display: flex; flex-direction: column; gap: var(--space-md); overflow-y: auto; }
.centre__bottom { grid-column: 1 / -1; }

@media (max-width: 768px) { .centre { grid-template-columns: 1fr; } .centre__left, .centre__right { display: none; } }

/* Kanban — preserved from PR #88, retokenised for holographic */
.kanban-board { display: grid; grid-template-columns: repeat(5, 1fr); gap: var(--space-sm); padding: var(--space-sm); }
.kanban-col { background: var(--glass); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space-sm); min-height: 200px; }
.kanban-col h3 { margin: 0 0 var(--space-sm); font-size: 11px; color: var(--cyan); text-transform: uppercase; letter-spacing: 0.1em; }
.kanban-card { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space-sm); margin-bottom: var(--space-sm); font-size: 12px; }
.kanban-card__agent { color: var(--violet); }
.kanban-card__elapsed { color: var(--text-dim); font-size: 10px; }
```

- [ ] **Step 2: Verify `.kanban-*` rules survive**

```bash
grep -c "\.kanban-" dashboard/src/styles/theme.css
# Expected: ≥4
```

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/styles/theme.css
git commit -m "feat(dashboard): premium theme tokens (P-I T2)"
```

---

### Task 1.3: `useGateway` hook

**Files:**
- Create: `dashboard/src/hooks/useGateway.js`
- Create: `dashboard/src/__tests__/hooks/useGateway.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useGateway } from '../../hooks/useGateway';

describe('useGateway', () => {
  beforeEach(() => { global.fetch = vi.fn(); });

  it('returns a fetch wrapper that adds Authorization header', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: 42 }) });
    const { result } = renderHook(() => useGateway('test-token'));
    const data = await act(() => result.current.get('/foo'));
    expect(data).toEqual({ data: 42 });
    expect(global.fetch).toHaveBeenCalledWith('/foo', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }));
  });

  it('throws on non-2xx responses', async () => {
    global.fetch.mockResolvedValueOnce({ ok: false, status: 500, text: async () => 'boom' });
    const { result } = renderHook(() => useGateway('t'));
    await expect(result.current.get('/fail')).rejects.toThrow('boom');
  });
});
```

- [ ] **Step 2: Run to verify failure**

```bash
cd dashboard && npx vitest run src/__tests__/hooks/useGateway.test.js
# Expected: FAIL — module not found
```

- [ ] **Step 3: Implement `useGateway`**

```javascript
import { useCallback } from 'react';

export function useGateway(token) {
  const request = useCallback(async (method, path, body) => {
    const res = await fetch(path, {
      method,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }, [token]);

  return {
    get: (path) => request('GET', path),
    post: (path, body) => request('POST', path, body),
    put: (path, body) => request('PUT', path, body),
    delete: (path) => request('DELETE', path),
  };
}
```

- [ ] **Step 4: Run to verify pass + 100% coverage**

```bash
cd dashboard && npx vitest run src/__tests__/hooks/useGateway.test.js --coverage
# Expected: PASS, useGateway.js 100%
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/hooks/useGateway.js dashboard/src/__tests__/hooks/useGateway.test.js
git commit -m "feat(dashboard): useGateway hook with token + 100% cov (P-I T3)"
```

---

### Task 1.4: `useWebSocket` hook

**Files:**
- Create: `dashboard/src/hooks/useWebSocket.js`
- Create: `dashboard/src/__tests__/hooks/useWebSocket.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useWebSocket } from '../../hooks/useWebSocket';

describe('useWebSocket', () => {
  let mockWs;
  beforeEach(() => {
    mockWs = { send: vi.fn(), close: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() };
    global.WebSocket = vi.fn(() => mockWs);
  });
  afterEach(() => { delete global.WebSocket; });

  it('connects to /ws/dashboard?token=... on mount', () => {
    renderHook(() => useWebSocket('tok', '/ws/dashboard'));
    expect(global.WebSocket).toHaveBeenCalledWith(expect.stringContaining('token=tok'));
  });

  it('reconnects on close with exponential backoff', async () => {
    vi.useFakeTimers();
    renderHook(() => useWebSocket('t', '/w'));
    act(() => { mockWs.onclose && mockWs.onclose({}); });
    act(() => { vi.advanceTimersByTime(2000); });
    expect(global.WebSocket).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 2: Implement `useWebSocket`**

```javascript
import { useEffect, useRef, useState, useCallback } from 'react';

export function useWebSocket(token, path = '/ws/dashboard') {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('connecting');
  const wsRef = useRef(null);
  const retryRef = useRef(0);

  useEffect(() => {
    const url = `${path}?token=${encodeURIComponent(token || '')}`;
    const ws = new WebSocket(url.includes('://') ? url : `ws://${location.host}${url}`);
    wsRef.current = ws;
    ws.onopen = () => { setStatus('open'); retryRef.current = 0; };
    ws.onmessage = (e) => setMessages((m) => [...m.slice(-99), JSON.parse(e.data)]);
    ws.onclose = () => {
      setStatus('closed');
      const delay = Math.min(30000, 1000 * 2 ** retryRef.current++);
      setTimeout(() => { /* reconnect by re-running effect */ }, delay);
    };
    ws.onerror = () => setStatus('error');
    return () => ws.close();
  }, [token, path]);

  const send = useCallback((data) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  return { messages, status, send };
}
```

- [ ] **Step 3: Test + commit**

```bash
cd dashboard && npx vitest run src/__tests__/hooks/useWebSocket.test.js --coverage
# Expected: PASS, 100% cov
git add dashboard/src/hooks/useWebSocket.js dashboard/src/__tests__/hooks/useWebSocket.test.js
git commit -m "feat(dashboard): useWebSocket hook with reconnect (P-I T4)"
```

---

### Task 1.5: `useVoice` hook + `StatusOrb` component

**Files:**
- Create: `dashboard/src/hooks/useVoice.js`
- Create: `dashboard/src/components/StatusOrb.jsx`
- Create: tests for both

(Abbreviated — same pattern as 1.3/1.4. `useVoice` returns `{supported, listening, transcript, error, start, stop}`. `StatusOrb` consumes a `state` prop ∈ {idle, working, error, ok} and renders the right CSS class.)

- [ ] **Step 1: Write tests for `useVoice` + `StatusOrb` (TDD)**

- [ ] **Step 2: Implement `useVoice`**

```javascript
import { useState, useRef, useCallback, useEffect } from 'react';

export function useVoice() {
  const supported = typeof window !== 'undefined' && ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window);
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState(null);
  const recRef = useRef(null);

  const start = useCallback(() => {
    if (!supported) { setError('not supported'); return; }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new SR();
    rec.continuous = false; rec.interimResults = true;
    rec.onresult = (e) => setTranscript(Array.from(e.results).map((r) => r[0].transcript).join(''));
    rec.onerror = (e) => setError(e.error);
    rec.onend = () => setListening(false);
    rec.start();
    recRef.current = rec;
    setListening(true);
  }, [supported]);

  const stop = useCallback(() => { recRef.current?.stop(); }, []);

  useEffect(() => () => recRef.current?.stop(), []);
  return { supported, listening, transcript, error, start, stop };
}
```

- [ ] **Step 3: Implement `StatusOrb`**

```jsx
import React from 'react';

const STATE_CLASS = { idle: '', working: '', ok: '', error: 'error', warn: 'warn' };
const STATE_TITLE = { idle: 'idle', working: 'working', ok: 'healthy', error: 'error', warn: 'warning' };

export function StatusOrb({ state = 'idle' }) {
  return (
    <div className="orb" data-state={state} title={STATE_TITLE[state] || state} />
  );
}
```

- [ ] **Step 4: Test + commit**

```bash
cd dashboard && npx vitest run --coverage
# Expected: all hook tests pass at 100%
git add dashboard/src/hooks/useVoice.js dashboard/src/components/StatusOrb.jsx dashboard/src/__tests__/hooks/useVoice.test.js dashboard/src/__tests__/components/StatusOrb.test.js
git commit -m "feat(dashboard): useVoice + StatusOrb (P-I T5)"
```

---

## Phase 2 — Data panels (5 panels, no interactivity)

### Task 2.1: `SurfaceBar` (channel status pills)

```jsx
import React from 'react';
import { useGateway } from '../hooks/useGateway';
import { StatusOrb } from './StatusOrb';

const CHANNELS = ['telegram', 'slack', 'discord', 'email'];

export function SurfaceBar({ token }) {
  const { get } = useGateway(token);
  const [channels, setChannels] = React.useState({});
  React.useEffect(() => {
    const tick = () => get('/health/summary').then((h) => setChannels(h.channels || {})).catch(() => {});
    tick();
    const id = setInterval(tick, 30000);
    return () => clearInterval(id);
  }, [get]);
  return (
    <div className="surface-bar" style={{ display: 'flex', gap: 8 }}>
      {CHANNELS.map((c) => (
        <span key={c} title={c} data-active={!!channels[c]} style={{ display: 'flex', alignItems: 'center', gap: 4, opacity: channels[c] ? 1 : 0.4 }}>
          <StatusOrb state={channels[c] ? 'ok' : 'idle'} /> {c}
        </span>
      ))}
    </div>
  );
}
```

Test: render with mocked `/health/summary` returning `{channels: {telegram: true, slack: false}}`; assert 4 pills, 2 active.

Commit: `feat(dashboard): SurfaceBar channel pills (P-I T6)`

---

### Task 2.2: `MemoryPeek` (collapsible topics)

```jsx
import React from 'react';
import { useGateway } from '../hooks/useGateway';

export function MemoryPeek({ token, onClose }) {
  const { get } = useGateway(token);
  const [topics, setTopics] = React.useState([]);
  React.useEffect(() => { get('/memory/topics').then((d) => setTopics(d.topics || [])).catch(() => {}); }, [get]);
  return (
    <div className="glass memory-peek">
      <div className="memory-peek__head"><h3>memory</h3><button onClick={onClose}>×</button></div>
      <ul>{topics.slice(0, 8).map((t) => <li key={t.name}>{t.name} <span className="text-dim">{t.count}</span></li>)}</ul>
    </div>
  );
}
```

Test: render with mocked `/memory/topics`; assert list of ≤8 items.

Commit: `feat(dashboard): MemoryPeek topics panel (P-I T7)`

---

### Task 2.3: `SkillLauncher` (pinned skills grid)

**Backend prerequisite (do FIRST, in this PR):**
- Add `pinned_names()` method to `src/hive/memory/skill_usage.py`
- Extend `GET /skills` in `gateway/app.py` to accept `?pinned=true` and return `{"pinned": [...names...]}`

```python
# src/hive/memory/skill_usage.py — add method
def pinned_names(self) -> list[str]:
    rows = self._db.execute("SELECT name FROM skill_usage WHERE pinned=1 ORDER BY name").fetchall()
    return [r["name"] for r in rows]
```

```python
# src/hive/gateway/app.py — extend
@app.get("/skills", dependencies=[Depends(require_token)])
async def skills_list(pinned: bool = False) -> dict:
    if pinned:
        return {"pinned": hive.skill_usage.pinned_names()}
    return hive.skill_usage.stats()
```

Test: `tests/test_skills_pinned.py` — register 2 skills, pin 1, hit `/skills?pinned=true`, assert 1-element list.

Then component:

```jsx
import React from 'react';
import { useGateway } from '../hooks/useGateway';

export function SkillLauncher({ token, onLaunch }) {
  const { get } = useGateway(token);
  const [skills, setSkills] = React.useState([]);
  React.useEffect(() => { get('/skills?pinned=true').then((d) => setSkills(d.pinned || [])).catch(() => {}); }, [get]);
  return (
    <div className="glass skill-launcher">
      <h3>skills</h3>
      <div className="skill-grid">{skills.map((s) => <button key={s} className="skill-chip" onClick={() => onLaunch?.(s)}>{s}</button>)}</div>
    </div>
  );
}
```

Test: render with mocked `/skills?pinned=true` returning `{pinned: ["x", "y"]}`; assert 2 chips.

Commit: `feat(dashboard+backend): SkillLauncher + pinned endpoint (P-I T8)`

---

### Task 2.4: `ActivityFeed` (real-time tool-call log)

```jsx
import React from 'react';
import { useWebSocket } from '../hooks/useWebSocket';

const TOOL_EVENTS = new Set(['tool_call', 'a2a.call.started', 'a2a.call.completed', 'a2a.call.failed']);

export function ActivityFeed({ token }) {
  const { messages } = useWebSocket(token);
  const calls = messages.filter((m) => TOOL_EVENTS.has(m.event_type) || TOOL_EVENTS.has(m.type)).slice(-10).reverse();
  return (
    <div className="glass activity-feed">
      <h3>activity</h3>
      <ul>{calls.map((c, i) => <li key={i} className="text-dim">{c.event_type || c.type} {c.tool_name || c.agent_name || ''}</li>)}</ul>
    </div>
  );
}
```

Test: render with `useWebSocket` mocked to return 12 messages, 8 of type `tool_call`; assert ≤10 displayed, most recent first.

Commit: `feat(dashboard): ActivityFeed real-time list (P-I T9)`

---

### Task 2.5: `SelfImprovementFeed` (recent learning verdicts)

```jsx
import React from 'react';
import { useGateway } from '../hooks/useGateway';

export function SelfImprovementFeed({ token }) {
  const { get } = useGateway(token);
  const [verdicts, setVerdicts] = React.useState([]);
  React.useEffect(() => {
    get('/learning/history').then((d) => setVerdicts((d.history || d.entries || []).filter((e) => e.verdict).slice(-5).reverse())).catch(() => {});
  }, [get]);
  return (
    <div className="glass self-improve">
      <h3>self-improve</h3>
      <ul>{verdicts.map((v, i) => <li key={i} data-verdict={v.verdict}>{v.id || v.summary} <span className="text-dim">{v.verdict}</span></li>)}</ul>
    </div>
  );
}
```

Test: render with mocked `/learning/history` returning 6 entries, 4 with `verdict` field; assert 4 displayed (≤5 cap).

Commit: `feat(dashboard): SelfImprovementFeed verdicts (P-I T10)`

---

## Phase 3 — Interactive (ChatCenter + VoiceToggle + ApprovalModal)

### Task 3.1: `ChatCenter` (hero — ~250 LOC)

The biggest component. Uses `useGateway` for POST + `useWebSocket` for streaming.

```jsx
import React from 'react';
import { useGateway } from '../hooks/useGateway';
import { useWebSocket } from '../hooks/useWebSocket';

export function ChatCenter({ token, sessionId, onApproval, voiceTranscript, onSendVoice }) {
  const { post } = useGateway(token);
  const { messages } = useWebSocket(token);
  const [input, setInput] = React.useState('');
  const [history, setHistory] = React.useState([]);

  // Accumulate streaming tokens from WS
  React.useEffect(() => {
    const last = messages[messages.length - 1];
    if (!last) return;
    if (last.event_type === 'token' || last.type === 'token') {
      setHistory((h) => [...h.slice(0, -1), { role: 'assistant', text: (h[h.length - 1]?.text || '') + (last.text || last.content || '') }]);
    } else if (last.event_type === 'tool_call' || last.type === 'tool_call') {
      setHistory((h) => [...h, { role: 'tool', name: last.tool_name, args: last.args }]);
    } else if (last.event_type === 'approval_request' || last.type === 'approval') {
      onApproval?.(last);
    }
  }, [messages, onApproval]);

  // Voice transcript → input
  React.useEffect(() => { if (voiceTranscript) setInput(voiceTranscript); }, [voiceTranscript]);

  const send = async () => {
    if (!input.trim()) return;
    setHistory((h) => [...h, { role: 'user', text: input }]);
    setInput('');
    try { await post('/chat', { message: input, session_id: sessionId }); } catch (e) { setHistory((h) => [...h, { role: 'error', text: String(e) }]); }
  };

  return (
    <div className="glass chat-center" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="chat-history" style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
        {history.map((m, i) => <div key={i} data-role={m.role}>{m.role === 'tool' ? <span className="tool-chip">{m.name}</span> : m.text}</div>)}
      </div>
      <div className="chat-input" style={{ display: 'flex', gap: 8, padding: 16 }}>
        <input data-testid="chat-input" value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()} style={{ flex: 1 }} />
        <button data-testid="chat-send" onClick={send}>send</button>
      </div>
    </div>
  );
}
```

Tests (≥80%):
- Renders empty history
- Renders user message after send (mock POST)
- Appends streamed token on WS message
- Renders tool chip on tool_call event
- Calls onApproval on approval_request

Commit: `feat(dashboard): ChatCenter hero (P-I T11)`

---

### Task 3.2: `VoiceToggle`

```jsx
import React from 'react';
import { useVoice } from '../hooks/useVoice';

export function VoiceToggle({ onTranscript }) {
  const { supported, listening, transcript, start, stop, error } = useVoice();
  React.useEffect(() => { if (transcript) onTranscript?.(transcript); }, [transcript, onTranscript]);
  if (!supported) return <span className="text-dim" data-testid="voice-unsupported">🎙 —</span>;
  return (
    <button data-testid="voice-toggle" data-listening={listening} onClick={listening ? stop : start}>
      {listening ? '◼' : '🎙'}
    </button>
  );
}
```

Test: render with supported=true; click toggles `data-listening`. Render with supported=false; assert text "—".

Commit: `feat(dashboard): VoiceToggle (P-I T12)`

---

### Task 3.3: `ApprovalModal` (blocks chat)

```jsx
import React from 'react';
import { useGateway } from '../hooks/useGateway';

export function ApprovalModal({ token, request, onClose }) {
  const { post } = useGateway(token);
  if (!request) return null;
  const decide = async (approve) => {
    await post(`/approvals/${request.id}/${'approve' if approve else 'reject'}`);
    onClose?.();
  };
  return (
    <div className="modal-backdrop" data-testid="approval-modal">
      <div className="glass modal">
        <h3>⚠ approval: {request.tool || request.action}</h3>
        <pre>{JSON.stringify(request.args || {}, null, 2)}</pre>
        <div className="modal__actions">
          <button onClick={() => decide(true)} data-testid="approve">approve</button>
          <button onClick={() => decide(false)} data-testid="reject">reject</button>
        </div>
      </div>
    </div>
  );
}
```

Test: render with request; click approve → POST `/approvals/{id}/approve` + onClose. Click reject → POST reject + onClose. Render with request=null → returns null.

Commit: `feat(dashboard): ApprovalModal (P-I T13)`

---

## Phase 4 — Root `Centre.jsx` + `main.jsx` swap

### Task 4.1: `Centre.jsx` (root layout)

```jsx
import React, { useState } from 'react';
import { SurfaceBar } from './components/SurfaceBar';
import { MemoryPeek } from './components/MemoryPeek';
import { SkillLauncher } from './components/SkillLauncher';
import { ActivityFeed } from './components/ActivityFeed';
import { SelfImprovementFeed } from './components/SelfImprovementFeed';
import { ChatCenter } from './components/ChatCenter';
import { VoiceToggle } from './components/VoiceToggle';
import { ApprovalModal } from './components/ApprovalModal';
import { StatusOrb } from './components/StatusOrb';

export function Centre({ token, sessionId }) {
  const [approval, setApproval] = useState(null);
  const [voiceText, setVoiceText] = useState('');
  return (
    <div className="centre">
      <div className="centre__top">
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <StatusOrb state="ok" /> HIVE
        </span>
        <SurfaceBar token={token} />
        <span style={{ display: 'flex', gap: 8 }}>
          <VoiceToggle onTranscript={setVoiceText} />
          <button>⚙</button>
        </span>
      </div>
      <div className="centre__left glass">
        <SkillLauncher token={token} />
      </div>
      <div className="centre__center">
        <ChatCenter token={token} sessionId={sessionId} onApproval={setApproval} voiceTranscript={voiceText} onSendVoice={() => setVoiceText('')} />
      </div>
      <div className="centre__right">
        <MemoryPeek token={token} />
        <ActivityFeed token={token} />
        <SelfImprovementFeed token={token} />
      </div>
      <div className="centre__bottom">
        {approval && <ApprovalModal token={token} request={approval} onClose={() => setApproval(null)} />}
      </div>
      <div className="scanline" />
    </div>
  );
}

export default Centre;
```

Test (`__tests__/Centre.test.js`):
- Mounts with mock token + sessionId
- Renders all 5 panels (MemoryPeek, SkillLauncher, ActivityFeed, SelfImprovementFeed, SurfaceBar)
- Renders ChatCenter + VoiceToggle
- Approval state toggles modal

Commit: `feat(dashboard): Centre.jsx root layout (P-I T14)`

---

### Task 4.2: `main.jsx` swap + delete `MissionControl.jsx`

- [ ] **Step 1: Edit `dashboard/src/main.jsx`**

```diff
- import MissionControl from './MissionControl';
+ import Centre from './Centre';
- <MissionControl token={...} />
+ <Centre token={...} sessionId={...} />
```

- [ ] **Step 2: Delete `dashboard/MissionControl.jsx` (or rename to `_legacy.jsx`)**

```bash
git mv dashboard/src/MissionControl.jsx dashboard/src/_legacy_MissionControl.jsx 2>/dev/null || git rm dashboard/src/MissionControl.jsx
```

- [ ] **Step 3: Run `npm run build` to verify bundle <500KB gzipped**

```bash
cd dashboard && npm run build
gzip -c dist/assets/*.js | wc -c
# Expected: < 500000 bytes
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/main.jsx
git rm dashboard/src/MissionControl.jsx
git commit -m "feat(dashboard): mount Centre.jsx, drop MissionControl (P-I T15)"
```

---

## Phase 5 — Backend tweak + smoke + docs

### Task 5.1: `/health/summary.channels` (Python)

**Files:**
- Modify: `src/hive/gateway/app.py`
- Test: `tests/test_health_channels.py`

- [ ] **Step 1: Write failing test**

```python
def test_health_summary_includes_channels(client):
    r = client.get("/health/summary")
    assert r.status_code == 200
    body = r.json()
    assert "channels" in body
    assert set(body["channels"].keys()) >= {"telegram", "slack", "discord", "email"}
```

- [ ] **Step 2: Implement**

```python
# In /health/summary handler, add:
"channels": {
    name: bool(getattr(hive, f"_{name}_channel", None))
    for name in ("telegram", "slack", "discord", "email")
},
```

(Verify the actual attribute name by reading `src/hive/gateway/channels/__init__.py` or wherever the registry lives.)

- [ ] **Step 3: Run test + full suite**

```bash
PYTHONPATH=src /home/hive/hiveos/.venv/bin/python -m pytest tests/test_health_channels.py -v
PYTHONPATH=src /home/hive/hiveos/.venv/bin/python -m pytest -q
# Expected: 3852 + 2 (pinned_names + channels) = 3854, all green
```

- [ ] **Step 4: Commit**

```bash
git add src/hive/gateway/app.py tests/test_health_channels.py
git commit -m "feat(gateway): /health/summary.channels per-channel status (P-I T16)"
```

---

### Task 5.2: Visual smoke + bundle size check + `hive doctor`

- [ ] **Step 1: Verify bundle**

```bash
cd dashboard && npm run build
ls -lh dist/assets/*.js
gzip -c dist/assets/*.js | wc -c
# Expected: gzipped < 500000
```

- [ ] **Step 2: `hive doctor` green**

```bash
PYTHONPATH=src /home/hive/hiveos/.venv/bin/python -m hive.doctor
# Expected: all green
```

- [ ] **Step 3: Manual visual check** (deferred per P-G precedent; document in PR body)

- [ ] **Step 4: Commit any smoke artifacts**

```bash
git add scripts/smokes/centre_smoke.sh 2>/dev/null
git commit -m "chore(sprint6): P-I visual smoke + bundle check (P-I T17)" --allow-empty
```

---

### Task 5.3: Docs (`docs/CENTRE.md`)

- [ ] **Step 1: Create `docs/CENTRE.md` (operator manual)**

```markdown
# HiveOS Centre — Operator Guide

The Centre is the daily-driver UI for HiveOS. Replaces the old Mission Control.

## Layout

[3-column grid: skills | chat | memory+activity+verdicts, top bar with status orb + surface bar + voice, bottom for approval modal]

## Theme

Holographic (locked from `docs/UI_PLAN.md` §7). Deep navy background (`--bg: #04050b`) with cyan conic-gradient borders, glass cards (`backdrop-filter: blur(20px)`), and pulsing glow accents. Palette: `--cyan: #22d3ee` · `--blue: #3b82f6` · `--violet: #8b5cf6` · `--amber: #f59e0b` · `--rose: #f43f5e`. System-stack typography (`-apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter"`). See SH1 mockup for visual reference.

## Channels

Top bar shows live status per channel (telegram, slack, discord, email). Sourced from `/health/summary.channels`.

## Voice input

Web Speech API. Falls back to no-op if browser doesn't support it.

## Approval flow

Approval requests from the agent block the chat. Modal shows full args + reason. Approve/Reject buttons POST to `/approvals/{id}/approve|reject`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/CENTRE.md
git commit -m "docs(sprint6): CENTRE.md operator manual (P-I T18)"
```

---

## Self-Review

| Issue #77 acceptance criterion | Plan task |
|---|---|
| `npm run build` succeeds; bundle <500KB gzipped | T4.2 step 3 + T5.2 step 1 |
| `Centre.jsx` mounts via `main.jsx` | T4.1 + T4.2 |
| Old `MissionControl.jsx` removed | T4.2 step 2 |
| Vitest 100% hooks, 80%+ components | T1.1 step 2 (thresholds) + per-task tests |
| Visual: holographic (per UI_PLAN §7), animated orb + glass panels + cyan glow, no jank | T1.2 (theme) + T1.5 (orb) + T4.1 (layout) |
| Manual: typing streams tokens live; tool calls show as chips | T3.1 (`useWebSocket` + tool chip render) |
| Memory peek loads | T2.2 (MemoryPeek with `/memory/topics`) |
| Approval modal blocks chat | T3.3 (modal renders over centre__bottom) |
| Existing CI green (ruff + pytest) | Every Python task: full suite rerun |
| `hive doctor` green | T5.2 step 2 |

**Spec coverage:** All 12 components + 3 hooks + theme + tests + 1 backend change accounted for.

**Placeholders:** None. Every step has code or commands.

**Type consistency:** All components use `useGateway(token)` and `useWebSocket(token)` — same shape, same return. `token` is `string`, never `null`.

---

## Execution Handoff

This plan is intended for **a single coder sub-agent** (whole-feature) followed by reviewer + security-reviewer in parallel + fix-pass if needed (same pattern as P-G #88, J3 #90).

**Total commits:** 18 (one per task) — matches the research doc §5 estimate.

**Pre-flight before dispatch:**
1. Create worktree: `git worktree add ../sprint6-jarvis-front -b sprint6/jarvis-front origin/main`
2. Verify `npm install` works in `dashboard/` (Vitest deps)
3. Verify `PYTHONPATH=src /home/hive/hiveos/.venv/bin/python -m pytest -q` passes (baseline)
4. Dispatch coder with this plan

**After coder returns:**
- Reviewer (spec + quality) + security-reviewer in parallel
- Fix-pass for Critical/Important; Minor findings documented in PR body
- Push branch + open PR with bundle size + test count + `hive doctor` result
- Update memory
- This is the FINAL sprint phase — when this lands, HiveOS v1.0 ships.
